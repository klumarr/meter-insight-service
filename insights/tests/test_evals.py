"""
Evals: behavioural checks against the real model.

The rest of the suite mocks the model, which means it can prove the code
handles a bad reply correctly but can never tell you how often a bad reply
actually happens. These tests answer the other question. They call the live
API and assert properties of what comes back.

Two things make them different from the unit tests, and both are deliberate:

They cost money and take seconds, so they are skipped unless asked for:

    RUN_LLM_EVALS=1 pytest insights/tests/test_evals.py -v

And they can fail without anything being wrong with this repository. A model
update, or simply a different sample, can break an eval that passed yesterday.
That is the point: an eval failing is information about the model, and the
right response is usually a prompt change rather than a code change.

The duplicate-citation bug that reached production-shaped output was found by
reading live responses by hand. These exist so that the next one is found by a
test instead.
"""

import os
import re
import time
from decimal import Decimal

import pytest
from django.core.cache import cache

from insights import analytics, llm, schemas
from insights.analytics import FactKey
from insights.tests import factories

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LLM_EVALS") != "1",
        reason="Costs money and calls the real API. Set RUN_LLM_EVALS=1 to run.",
    ),
]

# Hours of the day, day counts and small ordinals appear in perfectly good
# sentences ("between 5pm and 8pm", "over 14 days") without being quoted from a
# supplied figure. Allowing them keeps the invented-number check focused on the
# quantities that would actually mislead someone.
INCIDENTAL_MAXIMUM = Decimal(24)

SCENARIOS = {
    "rising_usage": factories.facts_with_week_on_week,
    "mostly_estimated": factories.facts_mostly_estimated,
    "flat_usage": factories.facts_with_flat_usage,
    "short_history": factories.facts_without_week_on_week,
}


@pytest.fixture(scope="module")
def responses():
    """
    One live call per scenario, shared by every test in the file.

    Module scope because these cost money. Each scenario is asked once and the
    answer is examined from several angles, rather than paying per assertion.
    """
    results = {}
    for name, build_facts in SCENARIOS.items():
        facts = build_facts()
        cache.clear()
        started = time.monotonic()
        response = llm.generate_insights(facts)
        results[name] = (facts, response, time.monotonic() - started)
    return results


def numbers_in(text: str) -> list[Decimal]:
    """Every numeric token in a piece of prose, with thousands separators removed."""
    return [Decimal(match.replace(",", "")) for match in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]


def supplied_numbers(facts: analytics.ConsumptionFacts) -> set[Decimal]:
    """
    Every figure the model was given, plus the roundings it may legitimately use.

    Writing "over 56%" for 56.3, or "378 kWh" for 378.00, is good plain English
    rather than invention. Rounding a supplied figure is allowed; producing one
    that traces back to nothing is not.
    """
    allowed: set[Decimal] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
            return
        try:
            number = Decimal(str(value))
        except Exception:
            return
        allowed.update({number, round(number), round(number, 1), Decimal(int(number)), abs(number)})

    collect(llm.summarise_facts(facts))
    return allowed


def invented_numbers(response: schemas.InsightResponse, facts) -> list[Decimal]:
    """Numbers in the model's prose that trace back to no supplied figure."""
    allowed = supplied_numbers(facts)
    written = [
        number
        for recommendation in response.recommendations
        for number in numbers_in(f"{recommendation.title} {recommendation.rationale}")
    ]
    return [n for n in written if n not in allowed and n > INCIDENTAL_MAXIMUM]


class TestTheRulesThatMustAlwaysHold:
    """Properties that should hold for every scenario, every time."""

    @pytest.mark.parametrize("scenario", SCENARIOS)
    def test_the_model_answers_without_needing_the_fallback(self, responses, scenario):
        """
        A fallback here means the model could not satisfy its own contract even
        with a retry. That is a prompt problem, and this is where it surfaces.
        """
        _, response, _ = responses[scenario]

        assert response.source is schemas.InsightSource.MODEL

    @pytest.mark.parametrize("scenario", SCENARIOS)
    def test_no_number_is_invented(self, responses, scenario):
        """
        The rule the entire architecture exists to enforce.

        The model is told never to calculate, and every figure it may quote is
        in the prompt. A number that traces back to nothing means the
        instruction was ignored, and a plausible fabricated figure is the single
        most damaging thing this service could produce.
        """
        facts, response, _ = responses[scenario]

        assert invented_numbers(response, facts) == []

    @pytest.mark.parametrize("scenario", SCENARIOS)
    def test_every_citation_is_a_fact_that_exists(self, responses, scenario):
        facts, response, _ = responses[scenario]

        available = analytics.available_fact_keys(facts)
        assert all(r.based_on in available for r in response.recommendations)

    @pytest.mark.parametrize("scenario", SCENARIOS)
    def test_no_fact_is_cited_twice(self, responses, scenario):
        """
        The regression test for the bug that prompted this file.

        Two recommendations resting on one fact report the same saving twice,
        and a caller adding them up is told a window can yield twice what it
        holds.
        """
        _, response, _ = responses[scenario]

        cited = [r.based_on for r in response.recommendations]
        assert len(cited) == len(set(cited))

    @pytest.mark.parametrize("scenario", SCENARIOS)
    def test_it_never_promises_a_saving_in_words(self, responses, scenario):
        """
        Savings are calculated by us and returned in their own field. A model
        writing "this will save you 40 kWh" into the prose would be inventing a
        figure and putting it somewhere no validation can reach.
        """
        _, response, _ = responses[scenario]

        for recommendation in response.recommendations:
            prose = f"{recommendation.title} {recommendation.rationale}".lower()
            assert "save you" not in prose
            assert not re.search(r"sav\w+ (?:of |you )?[\d£$]", prose)


class TestScenarioSpecificBehaviour:
    def test_heavily_estimated_data_is_flagged_to_the_customer(self, responses):
        """
        When most usage was inferred rather than measured, every other figure in
        the response rests on that inference. A customer who is not told is
        being given false confidence.
        """
        facts, response, _ = responses["mostly_estimated"]

        assert facts.estimated_share_percent > 50
        prose = " ".join(f"{r.title} {r.rationale}" for r in response.recommendations).lower()
        assert response.recommendations[0].based_on is FactKey.ESTIMATED_SHARE or any(
            word in prose for word in ("estimate", "estimated", "meter read", "reading")
        )

    def test_a_rise_in_usage_is_mentioned(self, responses):
        facts, response, _ = responses["rising_usage"]

        assert facts.week_on_week is not None
        assert any(r.based_on is FactKey.WEEK_ON_WEEK for r in response.recommendations)

    def test_evenly_spread_usage_is_not_described_as_having_a_peak(self, responses):
        """
        The withheld fact, checked from the model's side. It was never shown a
        peak window, so it must not invent one to talk about.
        """
        facts, response, _ = responses["flat_usage"]

        assert facts.peak_window is None
        prose = " ".join(f"{r.title} {r.rationale}" for r in response.recommendations).lower()
        assert "peak" not in prose

    def test_it_asks_for_no_more_than_the_facts_can_support(self, responses):
        """Distinct citations mean two facts cannot support three recommendations."""
        facts, response, _ = responses["flat_usage"]

        assert len(analytics.available_fact_keys(facts)) == 2
        assert len(response.recommendations) <= 2

    def test_a_short_history_produces_no_week_on_week_comparison(self, responses):
        facts, response, _ = responses["short_history"]

        assert facts.week_on_week is None
        assert all(r.based_on is not FactKey.WEEK_ON_WEEK for r in response.recommendations)


def test_the_rules_hold_across_repeated_calls():
    """
    The same question asked three times.

    A single passing sample proves very little about a non-deterministic system.
    This is the difference between "the model got it right" and "the model gets
    it right", and it is the one eval that would have caught the duplicate
    citation bug on its own.
    """
    facts = factories.facts_with_week_on_week()
    failures = []

    for attempt in range(3):
        cache.clear()
        response = llm.generate_insights(facts)

        cited = [r.based_on for r in response.recommendations]
        if response.source is not schemas.InsightSource.MODEL:
            failures.append(f"attempt {attempt}: fell back to templated output")
        if len(cited) != len(set(cited)):
            failures.append(f"attempt {attempt}: cited {cited}")
        if invented := invented_numbers(response, facts):
            failures.append(f"attempt {attempt}: invented {invented}")

    assert not failures, "\n".join(failures)


def test_report_latency(responses, capsys):
    """Not an assertion so much as a record of what a call actually costs."""
    with capsys.disabled():
        print("\n\n  scenario           facts  recs  latency")
        for name, (facts, response, elapsed) in responses.items():
            facts_count = len(analytics.available_fact_keys(facts))
            print(
                f"  {name:<18} {facts_count:>5}  {len(response.recommendations):>4}"
                f"  {elapsed:>6.2f}s"
            )
