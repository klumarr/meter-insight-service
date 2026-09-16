"""
The single test in this project that spends money.

Everything else mocks the model. This one does not, because mocks cannot tell
you whether Anthropic actually accepts the tool schema we generate, or how long
a real call takes. It is skipped unless explicitly asked for:

    RUN_LIVE_LLM_TEST=1 pytest insights/tests/test_live_smoke.py -s

One call costs well under a penny.
"""

import os
import time

import pytest

from insights import analytics, llm, schemas
from insights.tests import factories

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("RUN_LIVE_LLM_TEST"),
        reason="live API test: set RUN_LIVE_LLM_TEST=1 to run it",
    ),
]


def test_a_real_call_returns_a_usable_response(capsys):
    facts = factories.facts_with_week_on_week()

    started = time.perf_counter()
    response = llm.generate_insights(facts)
    elapsed = time.perf_counter() - started

    assert response.source is schemas.InsightSource.MODEL
    assert 1 <= len(response.recommendations) <= schemas.MAX_RECOMMENDATIONS

    available = analytics.available_fact_keys(facts)
    for recommendation in response.recommendations:
        assert recommendation.based_on in available
        assert recommendation.title
        assert recommendation.rationale

    with capsys.disabled():
        print(f"\n  latency: {elapsed:.2f}s")
        for recommendation in response.recommendations:
            print(
                f"\n  {recommendation.title}"
                f"\n    {recommendation.rationale}"
                f"\n    based on {recommendation.based_on}"
                f", confidence {recommendation.confidence}"
                f", we calculated {recommendation.estimated_saving_kwh} kWh"
            )
