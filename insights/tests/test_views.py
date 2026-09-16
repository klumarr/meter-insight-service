"""
Tests for the HTTP boundary.

Most of these mock insights.llm.generate_insights, because the view's job is
parsing, status codes and serialisation, and that is all they are checking.

The exception is TestItNeverFailsBecauseOfTheModel, which mocks the Anthropic
client instead and lets the real llm and fallback code run. That is the one
promise the service makes which cannot be demonstrated by testing any single
module on its own.
"""

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import anthropic
import pytest

from insights import fallback, llm, schemas
from insights.tests import factories

URL = "/api/insights"


def post(client, payload, **kwargs):
    return client.post(URL, data=json.dumps(payload), content_type="application/json", **kwargs)


def request_body(**overrides):
    """A well-formed request: two days of readings with a real evening peak."""
    readings = factories.peaked_half_hourly(factories.at(day=1), days=2, daily_kwh="48.00")
    body = {
        "readings": [
            {
                "timestamp": reading.timestamp.isoformat(),
                "value": str(reading.value),
                "quality": str(reading.quality),
            }
            for reading in readings
        ],
        "timezone": "Europe/London",
    }
    body.update(overrides)
    return body


@pytest.fixture
def model():
    """Stand in for the whole llm module, so the view is tested on its own."""
    with mock.patch.object(llm, "generate_insights") as generate:
        generate.side_effect = lambda facts: fallback.build_fallback(facts)
        yield generate


class TestTheHappyPath:
    def test_it_returns_recommendations(self, client, model):
        response = post(client, request_body())

        assert response.status_code == 200
        assert response.json()["recommendations"]

    def test_the_facts_are_returned_alongside_the_advice(self, client, model):
        """
        Advice that cannot be checked has negative value. Every recommendation
        names the fact it rests on, and that fact is in the same payload, so a
        reader can verify it without a second request.
        """
        payload = post(client, request_body()).json()

        cited = {rec["based_on"] for rec in payload["recommendations"]}
        assert cited
        assert set(payload["facts"]) >= {"total_consumption_kwh", "peak_window", "days_covered"}

    def test_figures_are_strings_rather_than_json_numbers(self, client, model):
        """
        Emitting 96.00 as a bare JSON number invites the caller's parser to read
        it as a float, undoing the exact arithmetic at the very last step.
        """
        payload = post(client, request_body()).json()

        assert payload["facts"]["total_consumption_kwh"] == "96.00"
        assert isinstance(payload["facts"]["estimated_share_percent"], str)

    def test_the_caller_is_told_who_wrote_the_answer(self, client, model):
        assert post(client, request_body()).json()["source"] == "fallback"

    def test_the_requested_timezone_decides_the_peak_hours(self, client, model):
        """
        A customer reads "18:00 to 21:00" as their own clock. These readings
        peak at 17:00-20:00 UTC, which in January is the same in London, so
        asking for Los Angeles is what proves the field is honoured.
        """
        london = post(client, request_body(timezone="Europe/London")).json()
        los_angeles = post(client, request_body(timezone="America/Los_Angeles")).json()

        assert london["facts"]["peak_window"]["start_hour"] == 17
        assert los_angeles["facts"]["peak_window"]["start_hour"] == 9
        assert los_angeles["facts"]["timezone_name"] == "America/Los_Angeles"


class TestRejectingBadRequests:
    """
    A client error is not a model error, and the response says which.

    The detail names the offending field so the caller can fix their payload
    rather than guess at it.
    """

    def test_a_body_that_is_not_json_is_refused(self, client):
        response = client.post(URL, data="not json at all", content_type="application/json")

        assert response.status_code == 400
        assert "not valid JSON" in response.json()["error"]

    def test_a_naive_timestamp_is_refused(self, client):
        body = request_body()
        body["readings"][0]["timestamp"] = "2026-01-01T00:30:00"

        response = post(client, body)

        assert response.status_code == 400
        assert response.json()["detail"][0]["loc"] == ["readings", 0, "timestamp"]

    def test_a_negative_reading_is_refused(self, client):
        body = request_body()
        body["readings"][0]["value"] = "-1.00"

        response = post(client, body)

        assert response.status_code == 400
        assert response.json()["detail"][0]["loc"] == ["readings", 0, "value"]

    def test_an_unknown_quality_is_refused(self, client):
        body = request_body()
        body["readings"][0]["quality"] = "GUESSED"

        assert post(client, body).status_code == 400

    def test_an_empty_reading_set_is_refused(self, client):
        assert post(client, request_body(readings=[])).status_code == 400

    def test_an_unknown_timezone_is_refused(self, client):
        response = post(client, request_body(timezone="Mars/Olympus_Mons"))

        assert response.status_code == 400
        assert response.json()["detail"][0]["loc"] == ["timezone"]

    def test_a_misspelled_field_is_refused_rather_than_ignored(self, client):
        """
        A client that sends `timezome` should hear about it immediately, not
        wonder why the value had no effect.
        """
        response = post(client, request_body(timezome="Europe/London"))

        assert response.status_code == 400
        assert response.json()["detail"][0]["loc"] == ["timezome"]

    def test_the_error_does_not_echo_the_request_back(self, client):
        """
        `loc` says which field was wrong, which is all the caller needs. Echoing
        the input would return up to a year of half-hourly readings in an error
        body for a single mistyped key at the root of the document.
        """
        response = post(client, request_body(timezome="Europe/London"))

        body = response.content.decode()
        assert "ESTIMATE" not in body and "ACTUAL" not in body
        assert len(body) < 1000

    def test_a_get_is_not_allowed(self, client):
        response = client.get(URL)

        assert response.status_code == 405
        assert response.headers["Allow"] == "POST"


class TestItNeverFailsBecauseOfTheModel:
    """
    The promise the whole service is built around, exercised end to end.

    These patch the Anthropic client rather than the llm module, so the real
    retry logic, the real validation and the real fallback all run.
    """

    @pytest.fixture
    def anthropic_client(self):
        fake = mock.MagicMock()
        with mock.patch.object(llm, "_client", return_value=fake):
            yield fake

    def test_a_timeout_still_returns_advice(self, client, anthropic_client):
        anthropic_client.messages.create.side_effect = anthropic.APITimeoutError(
            request=mock.MagicMock()
        )

        response = post(client, request_body())

        assert response.status_code == 200
        assert response.json()["source"] == "fallback"
        assert response.json()["recommendations"]

    def test_an_outage_still_returns_advice(self, client, anthropic_client):
        anthropic_client.messages.create.side_effect = anthropic.APIConnectionError(
            request=mock.MagicMock()
        )

        assert post(client, request_body()).status_code == 200

    def test_a_missing_api_key_still_returns_advice(self, client, settings):
        settings.ANTHROPIC_API_KEY = ""

        response = post(client, request_body())

        assert response.status_code == 200
        assert response.json()["source"] == "fallback"

    def test_nonsense_from_the_model_still_returns_advice(self, client, anthropic_client):
        """Two unusable replies in a row: the retry is spent, so the fallback answers."""
        anthropic_client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="I'd rather not.")]
        )

        response = post(client, request_body())

        assert response.status_code == 200
        assert response.json()["source"] == "fallback"
        assert anthropic_client.messages.create.call_count == 2

    def test_a_working_model_is_used_and_labelled(self, client, anthropic_client):
        # SimpleNamespace rather than MagicMock: `name` is a constructor
        # argument on Mock, so name="..." sets the mock's own repr instead of
        # the attribute the code reads, and the block is silently ignored.
        anthropic_client.messages.create.return_value = SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id="toolu_test",
                    name=llm.TOOL_NAME,
                    input={
                        "recommendations": [
                            {
                                "title": "Shift laundry to later in the evening",
                                "rationale": "Most of your electricity is used in the evening.",
                                "based_on": "peak_window",
                                "confidence": "high",
                            }
                        ]
                    },
                )
            ]
        )

        payload = post(client, request_body()).json()

        assert payload["source"] == "model"
        assert payload["recommendations"][0]["title"] == "Shift laundry to later in the evening"
        # The wording is the model's. The number is ours.
        assert payload["recommendations"][0]["estimated_saving_kwh"] == "8.10"


def test_the_saving_is_computed_rather_than_quoted(client, model):
    """
    A last check that nothing in the HTTP layer let a model-supplied number
    through: 15% of the peak window's 54.00 kWh, calculated by analytics.
    """
    payload = post(client, request_body()).json()

    peak = next(r for r in payload["recommendations"] if r["based_on"] == "peak_window")
    assert Decimal(peak["estimated_saving_kwh"]) == Decimal("8.10")
    assert schemas.InsightResponse.model_validate(payload)
