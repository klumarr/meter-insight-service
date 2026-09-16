"""
Tests for the model boundary.

The model is mocked throughout, so these are free, instant and deterministic.
That is not only a convenience: if this file needed a network, it could not
assert anything about timeouts or malformed replies, which is most of what
matters here.
"""

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import anthropic
import pytest

from insights import llm, schemas
from insights.tests import factories

VALID_REPLY = {
    "recommendations": [
        {
            "title": "Shift laundry to later in the evening",
            "rationale": "Most of your electricity is used between 17:00 and 20:00.",
            "based_on": "peak_window",
            "confidence": "high",
        }
    ]
}


def tool_use_message(payload: dict) -> SimpleNamespace:
    """A reply shaped like the SDK's, carrying the given tool arguments."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name=llm.TOOL_NAME, input=payload)]
    )


@pytest.fixture
def client():
    """Replace the Anthropic client, and hand the test the mock to inspect."""
    fake = mock.MagicMock()
    fake.messages.create.return_value = tool_use_message(VALID_REPLY)
    with mock.patch.object(llm, "_client", return_value=fake):
        yield fake


class TestPromptConstruction:
    def test_the_prompt_contains_the_computed_figures(self):
        facts = factories.facts_with_week_on_week()

        prompt = llm.build_prompt(facts)

        assert "739.20" in prompt
        assert "403.20" in prompt

    def test_the_prompt_never_contains_a_reading(self):
        """
        The context-window decision, asserted.

        The facts came from 672 readings. None of them is sent, because a year
        of half-hourly data would be slow, expensive and full of detail the
        model would only get lost in.
        """
        facts = factories.facts_with_week_on_week()

        prompt = llm.build_prompt(facts)

        assert facts.reading_count == 672
        assert "2026-01-01T00:30" not in prompt
        assert len(prompt) < 1500

    def test_an_absent_fact_is_omitted_rather_than_sent_as_null(self):
        """
        This omission is what makes the citation check meaningful.

        The model cannot cite a fact it was never shown, so a reply mentioning
        week_on_week here is provably invented rather than merely mistaken.
        """
        summary = llm.summarise_facts(factories.facts_without_week_on_week())

        assert "week_on_week" not in summary
        assert "peak_window" in summary

    def test_figures_are_sent_as_strings_to_survive_json(self):
        summary = llm.summarise_facts(factories.facts_with_week_on_week())

        assert summary["week_on_week"]["previous_week_kwh"] == "336.00"
        assert json.loads(json.dumps(summary))["total_consumption"]["total_kwh"] == "739.20"

    def test_the_system_prompt_forbids_arithmetic(self):
        assert "Never calculate" in llm.SYSTEM_PROMPT
        assert "Do not state a saving" in llm.SYSTEM_PROMPT


class TestTheCall:
    def test_a_valid_reply_becomes_an_insight_response(self, client):
        facts = factories.facts_with_week_on_week()

        response = llm.generate_insights(facts)

        assert response.source is schemas.InsightSource.MODEL
        assert response.recommendations[0].title == "Shift laundry to later in the evening"

    def test_the_saving_is_ours_even_though_the_wording_is_the_models(self, client):
        """The whole architecture in one assertion."""
        client.messages.create.return_value = tool_use_message(
            {"recommendations": [{**VALID_REPLY["recommendations"][0], "based_on": "week_on_week"}]}
        )
        facts = factories.facts_with_week_on_week()

        response = llm.generate_insights(facts)

        assert response.recommendations[0].estimated_saving_kwh == Decimal("67.20")

    def test_an_explicit_timeout_is_always_sent(self, client):
        """
        A request with no timeout can hang for as long as the far end likes,
        holding a worker open the whole time.
        """
        llm.generate_insights(factories.facts_with_week_on_week())

        assert client.messages.create.call_args.kwargs["timeout"] == 20

    def test_the_model_is_forced_to_use_the_tool(self, client):
        """
        "Please reply in JSON" is a request a model may decline. A forced tool
        call is not.
        """
        llm.generate_insights(factories.facts_with_week_on_week())

        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["tool_choice"] == {"type": "tool", "name": llm.TOOL_NAME}
        assert kwargs["tools"][0]["input_schema"]["properties"]["recommendations"]

    def test_output_length_is_capped(self, client):
        llm.generate_insights(factories.facts_with_week_on_week())

        assert client.messages.create.call_args.kwargs["max_tokens"] == 1024


class TestFailureModes:
    def test_a_timeout_is_reported_as_unavailable(self, client):
        client.messages.create.side_effect = anthropic.APITimeoutError(request=mock.MagicMock())

        with pytest.raises(llm.LLMUnavailableError, match="did not respond within 20"):
            llm.generate_insights(factories.facts_with_week_on_week())

    def test_an_api_error_is_reported_as_unavailable(self, client):
        client.messages.create.side_effect = anthropic.APIConnectionError(request=mock.MagicMock())

        with pytest.raises(llm.LLMUnavailableError):
            llm.generate_insights(factories.facts_with_week_on_week())

    def test_a_missing_api_key_fails_loudly(self, settings):
        """
        Better a clear error than a service that quietly serves templated text
        and looks like it is working.
        """
        settings.ANTHROPIC_API_KEY = ""

        with pytest.raises(llm.LLMUnavailableError, match="ANTHROPIC_API_KEY"):
            llm.generate_insights(factories.facts_with_week_on_week())

    def test_a_reply_breaking_the_schema_is_rejected(self, client):
        client.messages.create.return_value = tool_use_message(
            {"recommendations": [{"title": "No rationale given", "based_on": "peak_window"}]}
        )

        with pytest.raises(llm.InvalidModelOutputError):
            llm.generate_insights(factories.facts_with_week_on_week())

    def test_a_reply_citing_an_unavailable_fact_is_rejected(self, client):
        """
        Correctly shaped, and still untrustworthy.

        There was too little history to derive a week-on-week figure, so it was
        never in the prompt. Citing it is proof the justification was invented.
        """
        client.messages.create.return_value = tool_use_message(
            {"recommendations": [{**VALID_REPLY["recommendations"][0], "based_on": "week_on_week"}]}
        )

        with pytest.raises(llm.InvalidModelOutputError, match="week_on_week"):
            llm.generate_insights(factories.facts_without_week_on_week())

    def test_a_reply_that_never_calls_the_tool_is_rejected(self, client):
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Here are some ideas!")]
        )

        with pytest.raises(llm.InvalidModelOutputError, match="without calling"):
            llm.generate_insights(factories.facts_with_week_on_week())

    def test_the_two_failure_kinds_are_distinguishable(self):
        """
        Step 5 needs to tell them apart: a bad reply is worth one retry with
        feedback, an unreachable model is not.
        """
        assert issubclass(llm.LLMUnavailableError, llm.LLMError)
        assert issubclass(llm.InvalidModelOutputError, llm.LLMError)
        assert not issubclass(llm.InvalidModelOutputError, llm.LLMUnavailableError)


class TestTheClient:
    def test_the_sdk_does_not_retry_behind_our_back(self):
        """
        The SDK retries twice by default, which would triple the latency and
        cost of what looks like one call. The retry policy belongs in one
        visible place instead.
        """
        with mock.patch.object(anthropic, "Anthropic") as constructor:
            llm._client()

        assert constructor.call_args.kwargs["max_retries"] == 0
