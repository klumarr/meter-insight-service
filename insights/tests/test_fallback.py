"""
Tests for the recommendations written without a model.

These assert the property that makes the fallback worth having: it always
produces a correct, well-formed answer from the facts alone, with no network
call and nothing to go wrong.
"""

from decimal import Decimal

import pytest

from insights import analytics, fallback, schemas
from insights.analytics import FactKey
from insights.tests import factories


class TestItAlwaysAnswers:
    def test_it_produces_recommendations_from_full_facts(self):
        response = fallback.build_fallback(factories.facts_with_week_on_week())

        assert 1 <= len(response.recommendations) <= schemas.MAX_RECOMMENDATIONS

    def test_it_still_answers_when_only_the_minimum_is_known(self):
        """
        A single day of readings supports no week-on-week comparison, but total
        consumption is always available, so there is always something to say.
        """
        response = fallback.build_fallback(factories.facts_without_week_on_week())

        assert response.recommendations

    def test_it_is_labelled_as_a_fallback(self):
        """
        The caller is told the model was not involved rather than being left to
        guess. Quietly serving templated text as though it were generated is
        how a silent outage lasts for weeks.
        """
        response = fallback.build_fallback(factories.facts_with_week_on_week())

        assert response.source is schemas.InsightSource.FALLBACK

    def test_it_is_deterministic(self):
        facts = factories.facts_with_week_on_week()

        first = fallback.build_fallback(facts)
        second = fallback.build_fallback(facts)

        assert first.model_dump() == second.model_dump()


class TestItObeysTheSameContract:
    def test_its_output_passes_the_schema_the_model_must_satisfy(self):
        """
        The fallback is built as a ModelInsightResponse, so it cannot produce
        wording the model would have been refused for. One contract, two
        writers.
        """
        response = fallback.build_fallback(factories.facts_with_week_on_week())

        schemas.ModelInsightResponse(
            recommendations=[
                schemas.ModelRecommendation(
                    title=recommendation.title,
                    rationale=recommendation.rationale,
                    based_on=recommendation.based_on,
                    confidence=recommendation.confidence,
                )
                for recommendation in response.recommendations
            ]
        )

    def test_every_recommendation_cites_an_available_fact(self):
        facts = factories.facts_without_week_on_week()

        response = fallback.build_fallback(facts)

        available = analytics.available_fact_keys(facts)
        assert all(r.based_on in available for r in response.recommendations)

    def test_savings_come_from_the_same_calculation_as_the_models(self):
        """
        The fallback does not compute its own savings. It goes through
        build_response like the model's output does, so the numbers a customer
        sees do not depend on which writer produced the sentence around them.
        """
        facts = factories.facts_with_week_on_week()

        response = fallback.build_fallback(facts)

        week = next(r for r in response.recommendations if r.based_on is FactKey.WEEK_ON_WEEK)
        assert week.estimated_saving_kwh == Decimal("67.20")


class TestItOnlyStatesProvenFigures:
    def test_the_week_on_week_wording_quotes_both_totals(self):
        """
        Both weeks are given so the customer can check the claim, rather than
        being handed a percentage to take on trust.
        """
        response = fallback.build_fallback(factories.facts_with_week_on_week())

        week = next(r for r in response.recommendations if r.based_on is FactKey.WEEK_ON_WEEK)
        assert "403.20" in week.rationale
        assert "336.00" in week.rationale

    def test_a_rise_and_a_fall_are_described_differently(self):
        risen = fallback.build_fallback(factories.facts_with_week_on_week())
        fallen = fallback.build_fallback(factories.facts_with_falling_usage())

        risen_week = next(r for r in risen.recommendations if r.based_on is FactKey.WEEK_ON_WEEK)
        fallen_week = next(r for r in fallen.recommendations if r.based_on is FactKey.WEEK_ON_WEEK)
        assert "rose" in risen_week.title
        assert "fell" in fallen_week.title
        assert "-" not in fallen_week.title

    def test_the_peak_window_wording_uses_the_customers_timezone(self):
        facts = factories.facts_without_week_on_week()

        response = fallback.build_fallback(facts)

        peak = next(r for r in response.recommendations if r.based_on is FactKey.PEAK_WINDOW)
        assert facts.timezone_name in peak.rationale

    def test_heavily_estimated_data_is_flagged_first(self):
        """
        Mirrors the eval: when most usage is inferred rather than measured,
        that undermines every other figure and the customer should hear it
        before advice built on those figures.
        """
        facts = factories.facts_mostly_estimated()

        response = fallback.build_fallback(facts)

        assert response.recommendations[0].based_on is FactKey.ESTIMATED_SHARE
        assert "estimated" in response.recommendations[0].rationale

    def test_well_measured_data_is_not_flagged(self):
        facts = factories.facts_with_week_on_week()

        response = fallback.build_fallback(facts)

        assert all(r.based_on is not FactKey.ESTIMATED_SHARE for r in response.recommendations)


@pytest.mark.parametrize(
    "facts_builder",
    [
        factories.facts_with_week_on_week,
        factories.facts_without_week_on_week,
        factories.facts_mostly_estimated,
        factories.facts_with_falling_usage,
    ],
)
def test_no_set_of_facts_produces_an_unusable_answer(facts_builder):
    response = fallback.build_fallback(facts_builder())

    assert response.recommendations
    assert all(r.title and r.rationale for r in response.recommendations)
