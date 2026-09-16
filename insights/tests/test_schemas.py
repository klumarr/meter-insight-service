"""
Tests for the contracts.

The point of these is negative: they prove that output which does not meet the
contract is *rejected*, rather than quietly reaching a caller. A language model
will eventually return something malformed, and the value of the schema is
entirely in what it refuses.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from insights import analytics, schemas
from insights.analytics import FactKey
from insights.tests import factories


def model_reply(**overrides) -> dict:
    """A well-formed reply from the model, with individual fields overridable."""
    recommendation = {
        "title": "Shift laundry to later in the evening",
        "rationale": "Most of your electricity is used between 17:00 and 20:00.",
        "based_on": "peak_window",
        "confidence": "high",
    }
    recommendation.update(overrides)
    return {"recommendations": [recommendation]}


def request_payload(**overrides) -> dict:
    payload = {
        "readings": [
            {"timestamp": "2026-01-01T00:30:00Z", "value": "0.42", "quality": "ACTUAL"},
            {"timestamp": "2026-01-01T01:00:00Z", "value": "0.38", "quality": "ESTIMATE"},
        ],
    }
    payload.update(overrides)
    return payload


class TestModelContract:
    def test_a_well_formed_reply_is_accepted(self):
        response = schemas.ModelInsightResponse.model_validate(model_reply())

        assert response.recommendations[0].based_on is FactKey.PEAK_WINDOW
        assert response.recommendations[0].confidence is schemas.Confidence.HIGH

    def test_an_unknown_field_is_rejected(self):
        """
        The model is not permitted to add fields.

        An invented `estimated_saving_kwh` is exactly the failure this project
        exists to prevent, so it must be an error rather than something ignored.
        """
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            schemas.ModelInsightResponse.model_validate(model_reply(estimated_saving_kwh="12.00"))

    def test_an_overlong_title_is_rejected(self):
        with pytest.raises(ValidationError, match="at most 80 characters"):
            schemas.ModelInsightResponse.model_validate(model_reply(title="x" * 81))

    def test_an_overlong_rationale_is_rejected(self):
        with pytest.raises(ValidationError, match="at most 300 characters"):
            schemas.ModelInsightResponse.model_validate(model_reply(rationale="x" * 301))

    def test_an_empty_title_is_rejected(self):
        with pytest.raises(ValidationError):
            schemas.ModelInsightResponse.model_validate(model_reply(title=""))

    def test_a_missing_field_is_rejected(self):
        reply = model_reply()
        del reply["recommendations"][0]["based_on"]

        with pytest.raises(ValidationError, match="based_on"):
            schemas.ModelInsightResponse.model_validate(reply)

    def test_an_invented_confidence_level_is_rejected(self):
        with pytest.raises(ValidationError):
            schemas.ModelInsightResponse.model_validate(model_reply(confidence="very high"))

    def test_an_unknown_fact_name_is_rejected(self):
        with pytest.raises(ValidationError):
            schemas.ModelInsightResponse.model_validate(model_reply(based_on="the_weather"))

    def test_an_empty_list_of_recommendations_is_rejected(self):
        with pytest.raises(ValidationError, match="at least 1 item"):
            schemas.ModelInsightResponse.model_validate({"recommendations": []})

    def test_more_than_three_recommendations_are_rejected(self):
        reply = {"recommendations": model_reply()["recommendations"] * 4}

        with pytest.raises(ValidationError, match="at most 3 items"):
            schemas.ModelInsightResponse.model_validate(reply)

    def test_the_published_schema_carries_no_developer_prose(self):
        """
        Docstrings on these classes are prompt text, not notes to a colleague.

        They are sent to the model on every call, so they cost tokens and they
        instruct. Internal reasoning belongs in comments, which are not
        published.
        """
        schema = schemas.ModelInsightResponse.model_json_schema()
        descriptions = _all_descriptions(schema)

        assert descriptions
        assert all(len(description) < 120 for description in descriptions)

    def test_the_published_schema_offers_no_numeric_field(self):
        """The model is given no place to put a number, by construction."""
        properties = schemas.ModelInsightResponse.model_json_schema()["$defs"][
            "ModelRecommendation"
        ]["properties"]

        assert set(properties) == {"title", "rationale", "based_on", "confidence"}


class TestCitationChecking:
    def test_a_recommendation_citing_an_available_fact_is_accepted(self):
        facts = factories.facts_with_week_on_week()
        response = schemas.ModelInsightResponse.model_validate(model_reply(based_on="week_on_week"))

        schemas.check_citations(response, facts)

    def test_a_recommendation_citing_an_absent_fact_is_rejected(self):
        """
        The guardrail the static schema cannot provide.

        `week_on_week` is a valid fact *name*, so the schema accepts it. Whether
        the fact exists depends on the readings, and here there was too little
        history to derive one. A model citing it has invented its reasoning.
        """
        facts = factories.facts_without_week_on_week()
        response = schemas.ModelInsightResponse.model_validate(model_reply(based_on="week_on_week"))

        with pytest.raises(schemas.UncitedFactError):
            schemas.check_citations(response, facts)

    def test_the_rejection_message_is_usable_as_retry_feedback(self):
        """
        The message is handed back to the model on the retry, so it has to say
        both what was wrong and what the model should have chosen instead.
        """
        facts = factories.facts_without_week_on_week()
        response = schemas.ModelInsightResponse.model_validate(model_reply(based_on="week_on_week"))

        with pytest.raises(schemas.UncitedFactError) as error:
            schemas.check_citations(response, facts)

        message = str(error.value)
        assert "week_on_week" in message
        assert "peak_window" in message
        assert "total_consumption" in message

    def test_the_same_fact_cited_twice_is_rejected(self):
        """
        Found by running the service rather than by testing it.

        The model returned two recommendations about the evening peak. Both
        cited peak_window, so both carried the saving derived from it, and a
        caller adding them up would be told a 378 kWh window could yield 113
        kWh of savings when it can only yield 57.

        Every individual figure was correct. The set of them was not, which is
        why neither the schema nor the invented-fact check caught it.
        """
        facts = factories.facts_with_week_on_week()
        response = schemas.ModelInsightResponse.model_validate(
            {
                "recommendations": [
                    model_reply()["recommendations"][0],
                    model_reply(title="Look again at the evening")["recommendations"][0],
                ]
            }
        )

        with pytest.raises(schemas.DuplicateCitationError, match="more than once"):
            schemas.check_citations(response, facts)

    def test_distinct_citations_are_accepted(self):
        facts = factories.facts_with_week_on_week()
        response = schemas.ModelInsightResponse.model_validate(
            {
                "recommendations": [
                    model_reply(based_on="peak_window")["recommendations"][0],
                    model_reply(based_on="week_on_week")["recommendations"][0],
                    model_reply(based_on="total_consumption")["recommendations"][0],
                ]
            }
        )

        schemas.check_citations(response, facts)

    def test_both_faults_are_reported_at_once(self):
        """
        There is only one retry, and it should not be spent learning about half
        of what was wrong.
        """
        facts = factories.facts_without_week_on_week()
        response = schemas.ModelInsightResponse.model_validate(
            {
                "recommendations": [
                    model_reply(based_on="week_on_week")["recommendations"][0],
                    model_reply(based_on="peak_window")["recommendations"][0],
                    model_reply(based_on="peak_window", title="Again")["recommendations"][0],
                ]
            }
        )

        with pytest.raises(schemas.UncitedFactError) as error:
            schemas.check_citations(response, facts)

        message = str(error.value)
        assert "were not provided" in message
        assert "more than once" in message

    def test_both_faults_share_a_base_class(self):
        """insights.llm catches the base, so a new fault type cannot slip past it."""
        assert issubclass(schemas.UncitedFactError, schemas.CitationError)
        assert issubclass(schemas.DuplicateCitationError, schemas.CitationError)


class TestBuildResponse:
    def test_the_saving_is_calculated_by_us_not_supplied_by_the_model(self):
        facts = factories.facts_with_week_on_week()
        response = schemas.ModelInsightResponse.model_validate(model_reply(based_on="week_on_week"))

        built = schemas.build_response(response, facts=facts)

        # 403.20 kWh this week against 336.00 last week.
        assert built.recommendations[0].estimated_saving_kwh == Decimal("67.20")

    def test_a_recommendation_with_no_honest_saving_reports_none(self):
        facts = factories.facts_with_week_on_week()
        response = schemas.ModelInsightResponse.model_validate(
            model_reply(based_on="estimated_share")
        )

        built = schemas.build_response(response, facts=facts)

        assert built.recommendations[0].estimated_saving_kwh is None

    def test_the_models_wording_is_preserved_exactly(self):
        facts = factories.facts_with_week_on_week()
        response = schemas.ModelInsightResponse.model_validate(model_reply())

        built = schemas.build_response(response, facts=facts)

        assert built.recommendations[0].title == "Shift laundry to later in the evening"

    def test_the_source_is_recorded_on_the_response(self):
        """A fallback answer must never be mistaken for a model-written one."""
        facts = factories.facts_with_week_on_week()
        response = schemas.ModelInsightResponse.model_validate(model_reply())

        assert schemas.build_response(response, facts=facts).source is schemas.InsightSource.MODEL
        assert (
            schemas.build_response(
                response, facts=facts, source=schemas.InsightSource.FALLBACK
            ).source
            is schemas.InsightSource.FALLBACK
        )

    def test_the_facts_travel_with_the_recommendations(self):
        """So that a reader can check the advice without a second request."""
        facts = factories.facts_with_week_on_week()
        response = schemas.ModelInsightResponse.model_validate(model_reply())

        built = schemas.build_response(response, facts=facts)

        assert built.facts.total_consumption_kwh == facts.total_consumption_kwh

    def test_decimals_are_serialised_as_strings(self):
        """
        JSON has no decimal type.

        Emitting 67.20 as a bare JSON number invites the client's parser to read
        it as a float, which would undo the exact arithmetic at the very last
        step. A string survives the round trip intact.
        """
        facts = factories.facts_with_week_on_week()
        response = schemas.ModelInsightResponse.model_validate(model_reply(based_on="week_on_week"))

        payload = schemas.build_response(response, facts=facts).model_dump(mode="json")

        assert payload["recommendations"][0]["estimated_saving_kwh"] == "67.20"
        assert payload["facts"]["total_consumption_kwh"] == "739.20"


class TestInsightRequest:
    def test_a_well_formed_request_is_accepted(self):
        request = schemas.InsightRequest.model_validate(request_payload())

        assert len(request.readings) == 2

    def test_readings_convert_to_the_analytics_type(self):
        request = schemas.InsightRequest.model_validate(request_payload())

        readings = request.to_readings()

        assert readings[0].value == Decimal("0.42")
        assert readings[0].quality is analytics.ReadingQuality.ACTUAL
        assert readings[0].timestamp.tzinfo is not None
        assert isinstance(readings[0], analytics.Reading)

    def test_a_timestamp_without_an_offset_is_rejected(self):
        """The analytics layer refuses naive timestamps, so the door does too."""
        payload = request_payload()
        payload["readings"][0]["timestamp"] = "2026-01-01T00:30:00"

        with pytest.raises(ValidationError, match="timezone"):
            schemas.InsightRequest.model_validate(payload)

    def test_a_negative_reading_is_rejected(self):
        payload = request_payload()
        payload["readings"][0]["value"] = "-1.0"

        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            schemas.InsightRequest.model_validate(payload)

    def test_an_unknown_quality_is_rejected(self):
        payload = request_payload()
        payload["readings"][0]["quality"] = "PROBABLY_RIGHT"

        with pytest.raises(ValidationError):
            schemas.InsightRequest.model_validate(payload)

    def test_no_readings_is_rejected(self):
        with pytest.raises(ValidationError, match="at least 1 item"):
            schemas.InsightRequest.model_validate({"readings": []})

    def test_an_unreasonable_number_of_readings_is_rejected(self):
        """
        An unbounded list is a way to exhaust the server's memory.

        The facts are computed across the whole set at once, so the cap is a
        real limit rather than a formality.
        """
        one_reading = request_payload()["readings"][0]
        payload = {"readings": [one_reading] * (schemas.MAX_READINGS + 1)}

        with pytest.raises(ValidationError, match="at most"):
            schemas.InsightRequest.model_validate(payload)

    def test_an_unknown_timezone_is_rejected(self):
        with pytest.raises(ValidationError, match="unknown timezone"):
            schemas.InsightRequest.model_validate(request_payload(timezone="Middle/Earth"))

    def test_the_timezone_defaults_to_utc(self):
        request = schemas.InsightRequest.model_validate(request_payload())

        assert request.timezone == "UTC"
        assert request.zone() is not None

    def test_an_unknown_field_is_rejected(self):
        """
        A misspelled field should be an error, not a silently ignored value.

        A client sending `timezone` as `timeZone` would otherwise get UTC
        results and no indication of why.
        """
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            schemas.InsightRequest.model_validate(request_payload(timeZone="Europe/London"))


def _all_descriptions(schema: object) -> list[str]:
    found = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "description" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_all_descriptions(value))
    elif isinstance(schema, list):
        for item in schema:
            found.extend(_all_descriptions(item))
    return found
