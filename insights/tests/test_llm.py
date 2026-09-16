"""
Tests for the model boundary.

The model is mocked throughout, so these are free, instant and deterministic.
That is not only a convenience: if this file needed a network it could not
assert anything about timeouts, malformed replies or repair attempts, which is
most of what matters here.
"""

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import anthropic
import pytest
from django.core.cache import cache

from insights import analytics, llm, schemas
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

SCHEMA_BREAKING_REPLY = {
    "recommendations": [{"title": "No rationale given", "based_on": "peak_window"}]
}

UNCITED_FACT_REPLY = {
    "recommendations": [{**VALID_REPLY["recommendations"][0], "based_on": "week_on_week"}]
}

# Two recommendations about the same evening peak. Each is individually
# well-formed and correctly cited; together they report one saving twice.
DUPLICATE_CITATION_REPLY = {
    "recommendations": [
        VALID_REPLY["recommendations"][0],
        {**VALID_REPLY["recommendations"][0], "title": "Look again at the evening"},
    ]
}


def tool_use_message(payload: dict, call_id: str = "toolu_test") -> SimpleNamespace:
    """A reply shaped like the SDK's, carrying the given tool arguments."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id=call_id, name=llm.TOOL_NAME, input=payload)]
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

    @pytest.mark.parametrize(
        "facts_builder",
        [
            factories.facts_with_week_on_week,
            factories.facts_without_week_on_week,
            factories.facts_with_flat_usage,
            factories.facts_mostly_estimated,
        ],
    )
    def test_the_prompt_offers_exactly_the_citable_facts(self, facts_builder):
        """
        The invariant the whole citation guardrail rests on.

        If the prompt could offer a fact the citation check later refuses, a
        well-behaved model would be punished for doing as it was told, and we
        would burn a retry and probably a fallback on our own inconsistency.
        If it withheld a fact the check would have accepted, we would be paying
        for advice we made impossible to give.
        """
        facts = facts_builder()

        offered = set(llm.summarise_facts(facts))

        assert offered == analytics.available_fact_keys(facts)

    def test_an_absent_peak_is_omitted_too(self):
        facts = factories.facts_with_flat_usage()

        assert facts.peak_window is None
        assert "peak_window" not in llm.build_prompt(facts)

    def test_it_asks_for_no_more_recommendations_than_there_are_facts(self):
        """
        Each recommendation has to cite a different fact, so a household with
        only two facts cannot support three. Asking for three anyway guarantees
        a duplicate, and costs a retry to discover what we knew before calling.
        """
        two_facts = factories.facts_with_flat_usage()
        four_facts = factories.facts_with_week_on_week()

        assert len(analytics.available_fact_keys(two_facts)) == 2
        assert "up to 2 recommendations" in llm.build_prompt(two_facts)
        assert "up to 3 recommendations" in llm.build_prompt(four_facts)

    def test_the_prompt_asks_for_distinct_facts(self):
        assert "each citing a different fact" in llm.build_prompt(
            factories.facts_with_week_on_week()
        )
        assert "Never cite the same fact twice" in llm.SYSTEM_PROMPT

    def test_figures_are_sent_as_strings_to_survive_json(self):
        summary = llm.summarise_facts(factories.facts_with_week_on_week())

        assert summary["week_on_week"]["previous_week_kwh"] == "336.00"
        assert json.loads(json.dumps(summary))["total_consumption"]["total_kwh"] == "739.20"

    def test_the_system_prompt_forbids_arithmetic(self):
        assert "Never calculate" in llm.SYSTEM_PROMPT
        assert "Never derive a new figure" in llm.SYSTEM_PROMPT
        assert "Do not state a saving" in llm.SYSTEM_PROMPT

    def test_figures_the_model_would_otherwise_work_out_are_supplied(self):
        """
        Both of these were added because an eval caught the model calculating
        them: dividing the total by the days, and subtracting the estimated
        share from a hundred. A model that needs a figure it was not given will
        make one, so the cure is to give it.
        """
        summary = llm.summarise_facts(factories.facts_mostly_estimated())

        assert summary["total_consumption"]["average_daily_kwh"] == "48.00"
        assert summary["estimated_share"]["percent_from_a_real_meter_read"] == "28.6"
        assert summary["estimated_share"]["percent_not_from_a_real_meter_read"] == "71.4"


class TestTheCall:
    def test_a_valid_reply_becomes_an_insight_response(self, client):
        response = llm.generate_from_model(factories.facts_with_week_on_week())

        assert response.source is schemas.InsightSource.MODEL
        assert response.recommendations[0].title == "Shift laundry to later in the evening"

    def test_the_saving_is_ours_even_though_the_wording_is_the_models(self, client):
        """The whole architecture in one assertion."""
        client.messages.create.return_value = tool_use_message(UNCITED_FACT_REPLY)

        response = llm.generate_from_model(factories.facts_with_week_on_week())

        assert response.recommendations[0].estimated_saving_kwh == Decimal("67.20")

    def test_an_explicit_timeout_is_always_sent(self, client):
        """
        A request with no timeout can hang for as long as the far end likes,
        holding a worker open the whole time.
        """
        llm.generate_from_model(factories.facts_with_week_on_week())

        assert client.messages.create.call_args.kwargs["timeout"] == 20

    def test_the_model_is_forced_to_use_the_tool(self, client):
        """
        "Please reply in JSON" is a request a model may decline. A forced tool
        call is not.
        """
        llm.generate_from_model(factories.facts_with_week_on_week())

        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["tool_choice"] == {"type": "tool", "name": llm.TOOL_NAME}
        assert kwargs["tools"][0]["input_schema"]["properties"]["recommendations"]

    def test_output_length_is_capped(self, client):
        llm.generate_from_model(factories.facts_with_week_on_week())

        assert client.messages.create.call_args.kwargs["max_tokens"] == 1024


class TestTheRepairAttempt:
    def test_a_rejected_reply_is_retried_once_and_can_succeed(self, client):
        client.messages.create.side_effect = [
            tool_use_message(SCHEMA_BREAKING_REPLY),
            tool_use_message(VALID_REPLY),
        ]

        response = llm.generate_from_model(factories.facts_with_week_on_week())

        assert response.recommendations[0].title == "Shift laundry to later in the evening"
        assert client.messages.create.call_count == 2

    def test_the_retry_shows_the_model_its_own_rejected_output(self, client):
        """
        Being told "that was wrong" repairs far less reliably than being shown
        what you said alongside the objection to it.
        """
        client.messages.create.side_effect = [
            tool_use_message(SCHEMA_BREAKING_REPLY),
            tool_use_message(VALID_REPLY),
        ]

        llm.generate_from_model(factories.facts_with_week_on_week())

        retry_messages = client.messages.create.call_args_list[1].kwargs["messages"]
        assistant_turn = retry_messages[1]
        objection = retry_messages[2]["content"][0]
        assert assistant_turn["role"] == "assistant"
        assert assistant_turn["content"][0]["input"] == SCHEMA_BREAKING_REPLY
        assert objection["type"] == "tool_result"
        assert objection["is_error"] is True
        assert "rationale" in objection["content"].lower()

    def test_an_invented_citation_is_explained_back_to_the_model(self, client):
        client.messages.create.side_effect = [
            tool_use_message(UNCITED_FACT_REPLY),
            tool_use_message(VALID_REPLY),
        ]

        llm.generate_from_model(factories.facts_without_week_on_week())

        objection = client.messages.create.call_args_list[1].kwargs["messages"][2]["content"][0]
        assert "week_on_week" in objection["content"]
        assert "peak_window" in objection["content"]

    def test_the_same_fact_cited_twice_is_retried(self, client):
        """
        The duplicate that double-counts a saving goes through the same repair
        path as an invented citation: one retry, with the objection attached.
        """
        client.messages.create.side_effect = [
            tool_use_message(DUPLICATE_CITATION_REPLY),
            tool_use_message(VALID_REPLY),
        ]

        response = llm.generate_from_model(factories.facts_with_week_on_week())

        assert client.messages.create.call_count == 2
        objection = client.messages.create.call_args_list[1].kwargs["messages"][2]["content"][0]
        assert "more than once" in objection["content"]
        assert len(response.recommendations) == 1

    def test_it_retries_only_once(self, client):
        """A second bad reply is a pattern, not a blip. Stop paying for it."""
        client.messages.create.side_effect = [
            tool_use_message(SCHEMA_BREAKING_REPLY),
            tool_use_message(SCHEMA_BREAKING_REPLY),
        ]

        with pytest.raises(llm.InvalidModelOutputError):
            llm.generate_from_model(factories.facts_with_week_on_week())

        assert client.messages.create.call_count == 2

    def test_an_unreachable_model_is_not_retried(self, client):
        """
        Retrying a timeout just spends a second timeout to learn the same
        thing. The fallback can answer immediately instead.
        """
        client.messages.create.side_effect = anthropic.APITimeoutError(request=mock.MagicMock())

        with pytest.raises(llm.LLMUnavailableError):
            llm.generate_from_model(factories.facts_with_week_on_week())

        assert client.messages.create.call_count == 1


class TestFailureModes:
    def test_a_timeout_is_reported_as_unavailable(self, client):
        client.messages.create.side_effect = anthropic.APITimeoutError(request=mock.MagicMock())

        with pytest.raises(llm.LLMUnavailableError, match="did not respond within 20"):
            llm.generate_from_model(factories.facts_with_week_on_week())

    def test_an_api_error_is_reported_as_unavailable(self, client):
        client.messages.create.side_effect = anthropic.APIConnectionError(request=mock.MagicMock())

        with pytest.raises(llm.LLMUnavailableError):
            llm.generate_from_model(factories.facts_with_week_on_week())

    def test_a_missing_api_key_fails_loudly(self, settings):
        """
        Better a clear error than a service that quietly serves templated text
        and looks like it is working.
        """
        settings.ANTHROPIC_API_KEY = ""

        with pytest.raises(llm.LLMUnavailableError, match="ANTHROPIC_API_KEY"):
            llm.generate_from_model(factories.facts_with_week_on_week())

    def test_a_reply_that_never_calls_the_tool_is_rejected(self, client):
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Here are some ideas!")]
        )

        with pytest.raises(llm.InvalidModelOutputError, match="without calling"):
            llm.generate_from_model(factories.facts_with_week_on_week())

    def test_the_two_failure_kinds_are_distinguishable(self):
        """
        The distinction the repair loop depends on: a bad reply is worth one
        retry with feedback, an unreachable model is not.
        """
        assert issubclass(llm.LLMUnavailableError, llm.LLMError)
        assert issubclass(llm.InvalidModelOutputError, llm.LLMError)
        assert not issubclass(llm.InvalidModelOutputError, llm.LLMUnavailableError)


class TestFallingBack:
    """
    The public entry point never raises.

    Whatever the model does, a caller gets a usable answer built from figures
    that were already proved correct.
    """

    def test_an_unreachable_model_falls_back(self, client):
        client.messages.create.side_effect = anthropic.APITimeoutError(request=mock.MagicMock())

        response = llm.generate_insights(factories.facts_with_week_on_week())

        assert response.source is schemas.InsightSource.FALLBACK
        assert response.recommendations

    def test_two_unusable_replies_fall_back(self, client):
        client.messages.create.side_effect = [
            tool_use_message(SCHEMA_BREAKING_REPLY),
            tool_use_message(UNCITED_FACT_REPLY),
        ]

        response = llm.generate_insights(factories.facts_without_week_on_week())

        assert response.source is schemas.InsightSource.FALLBACK

    def test_a_missing_api_key_falls_back(self, settings, client):
        settings.ANTHROPIC_API_KEY = ""

        response = llm.generate_insights(factories.facts_with_week_on_week())

        assert response.source is schemas.InsightSource.FALLBACK

    def test_falling_back_is_logged_as_a_warning(self, client, caplog):
        """
        A fallback firing on every request looks identical to a healthy service
        from the outside. That is the outage you hear about from a customer.
        """
        client.messages.create.side_effect = anthropic.APITimeoutError(request=mock.MagicMock())

        llm.generate_insights(factories.facts_with_week_on_week())

        assert any(record.levelname == "WARNING" for record in caplog.records)

    def test_a_working_model_is_not_labelled_as_a_fallback(self, client):
        response = llm.generate_insights(factories.facts_with_week_on_week())

        assert response.source is schemas.InsightSource.MODEL


class TestCaching:
    def test_identical_facts_reuse_the_first_answer(self, client):
        facts = factories.facts_with_week_on_week()

        first = llm.generate_from_model(facts)
        second = llm.generate_from_model(facts)

        assert client.messages.create.call_count == 1
        assert first.model_dump() == second.model_dump()

    def test_different_facts_are_asked_about_separately(self, client):
        llm.generate_from_model(factories.facts_with_week_on_week())
        llm.generate_from_model(factories.facts_mostly_estimated())

        assert client.messages.create.call_count == 2

    def test_the_numbers_are_recalculated_rather_than_cached(self, monkeypatch, client):
        """
        The division the cache is built around.

        Wording is expensive, slow and non-deterministic, so it is cached.
        Savings are instant and deterministic, so they are not. Correcting an
        assumption in analytics.py therefore takes effect on the very next
        request instead of being shadowed by an hour of cached arithmetic.
        """
        facts = factories.facts_with_week_on_week()
        first = llm.generate_from_model(facts)

        monkeypatch.setattr(analytics, "SHIFTABLE_PEAK_SHARE", Decimal("0.30"))
        second = llm.generate_from_model(facts)

        assert client.messages.create.call_count == 1
        assert first.recommendations[0].title == second.recommendations[0].title
        # 15% then 30% of the same 415.80 kWh peak window, from the same words.
        assert first.recommendations[0].estimated_saving_kwh == Decimal("62.37")
        assert second.recommendations[0].estimated_saving_kwh == Decimal("124.74")

    def test_a_fallback_is_never_cached(self, client):
        """
        A thirty-second outage should not pin templated text in front of every
        identical request for the next hour. A transient failure stays
        transient.
        """
        facts = factories.facts_with_week_on_week()
        client.messages.create.side_effect = anthropic.APITimeoutError(request=mock.MagicMock())
        assert llm.generate_insights(facts).source is schemas.InsightSource.FALLBACK

        client.messages.create.side_effect = None
        client.messages.create.return_value = tool_use_message(VALID_REPLY)

        assert llm.generate_insights(facts).source is schemas.InsightSource.MODEL

    def test_a_new_model_invalidates_the_cache(self, settings, client):
        facts = factories.facts_with_week_on_week()
        llm.generate_from_model(facts)

        settings.ANTHROPIC_MODEL = "claude-something-newer"
        llm.generate_from_model(facts)

        assert client.messages.create.call_count == 2

    def test_a_changed_system_prompt_invalidates_the_cache(self, monkeypatch, client):
        """
        Forgetting this is how a service keeps serving wording written under
        yesterday's instructions for an hour after they were changed.
        """
        facts = factories.facts_with_week_on_week()
        llm.generate_from_model(facts)

        monkeypatch.setattr(llm, "SYSTEM_PROMPT", llm.SYSTEM_PROMPT + "\n- Be brief.")
        llm.generate_from_model(facts)

        assert client.messages.create.call_count == 2

    def test_a_broken_cache_does_not_break_the_request(self, client):
        """
        A cache is an optimisation, and an optimisation that can take the
        endpoint down is a liability.
        """
        with mock.patch("insights.llm.cache") as broken:
            broken.get.side_effect = ConnectionError("cache is down")
            broken.set.side_effect = ConnectionError("cache is down")

            response = llm.generate_from_model(factories.facts_with_week_on_week())

        assert response.recommendations
        assert client.messages.create.call_count == 1

    def test_an_entry_from_an_older_schema_is_treated_as_a_miss(self, client):
        """
        Trusting it would put output we no longer consider valid in front of a
        customer. Discarding it costs one call.
        """
        facts = factories.facts_with_week_on_week()
        cache.set(llm._cache_key(llm.build_prompt(facts)), {"recommendations": [{"title": "x"}]})

        response = llm.generate_from_model(facts)

        assert client.messages.create.call_count == 1
        assert response.recommendations[0].title == "Shift laundry to later in the evening"

    def test_the_key_does_not_contain_the_prompt(self):
        """Backends limit key length and content; a prompt is long and unbounded."""
        key = llm._cache_key(llm.build_prompt(factories.facts_with_week_on_week()))

        assert "739.20" not in key
        assert len(key) < 100


class TestTheClient:
    def test_the_sdk_does_not_retry_behind_our_back(self):
        """
        The SDK retries twice by default, which would triple the latency and
        cost of what looks like one call, and would fight the single deliberate
        repair attempt above.
        """
        with mock.patch.object(anthropic, "Anthropic") as constructor:
            llm._client()

        assert constructor.call_args.kwargs["max_retries"] == 0
