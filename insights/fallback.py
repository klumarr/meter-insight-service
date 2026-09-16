"""
Recommendations written without a language model.

This is what the service returns when the model is unreachable, too slow, or
cannot produce an acceptable reply. It is plainer than the model's wording, and
it is completely correct: every sentence is assembled from figures
insights.analytics already proved.

The important property is that this module has no dependency on insights.llm.
Delete that file and the service still answers every request. An endpoint that
returns 500 because a third party had a bad afternoon is not a working endpoint.

The output goes through exactly the same schema and the same saving calculation
as the model's, so a fallback response cannot be shaped differently from a
model-written one. Only the `source` field distinguishes them, and it is
returned to the caller rather than hidden.
"""

from decimal import Decimal

from insights import analytics, schemas
from insights.analytics import FactKey

# Below this, the estimated share is unremarkable and the advice would be noise.
# Above it, the customer's figures rest on inference rather than measurement,
# which is worth saying out loud before anything else.
ESTIMATED_SHARE_WORTH_MENTIONING = Decimal("20.0")


def build_fallback(facts: analytics.ConsumptionFacts) -> schemas.InsightResponse:
    """
    Assemble up to three recommendations from the facts alone.

    At least one is always produced, because total consumption is always
    available, so this function cannot fail to answer.
    """
    candidates = [
        _estimated_share_recommendation(facts),
        _week_on_week_recommendation(facts),
        _peak_window_recommendation(facts),
        _total_consumption_recommendation(facts),
    ]
    chosen = [candidate for candidate in candidates if candidate is not None]

    return schemas.build_response(
        schemas.ModelInsightResponse(recommendations=chosen[: schemas.MAX_RECOMMENDATIONS]),
        facts=facts,
        source=schemas.InsightSource.FALLBACK,
    )


def _estimated_share_recommendation(
    facts: analytics.ConsumptionFacts,
) -> schemas.ModelRecommendation | None:
    """
    Placed first on purpose.

    If most of the consumption was inferred rather than measured, that
    undermines every other figure in the response, so the customer should hear
    it before advice built on those figures.
    """
    if facts.estimated_share_percent < ESTIMATED_SHARE_WORTH_MENTIONING:
        return None

    return schemas.ModelRecommendation(
        title=f"Check your meter readings: {facts.estimated_share_percent}% are estimated",
        rationale=(
            f"{facts.estimated_share_percent}% of your usage came from estimated or "
            "calculated values rather than a real meter read. Submitting a reading, or "
            "having a smart meter fitted, makes your bills reflect what you actually use."
        ),
        based_on=FactKey.ESTIMATED_SHARE,
        confidence=schemas.Confidence.HIGH,
    )


def _week_on_week_recommendation(
    facts: analytics.ConsumptionFacts,
) -> schemas.ModelRecommendation | None:
    change = facts.week_on_week
    if change is None:
        return None

    if change.change_percent > 0:
        title = f"Your electricity use rose {change.change_percent}% on the previous week"
        closing = "Returning to the earlier level is the single easiest saving available."
    else:
        title = f"Your electricity use fell {abs(change.change_percent)}% on the previous week"
        closing = "Whatever changed is working, and is worth keeping up."

    return schemas.ModelRecommendation(
        title=title,
        rationale=(
            f"You used {change.latest_week_kwh} kWh over the last seven days, against "
            f"{change.previous_week_kwh} kWh the week before. {closing}"
        ),
        based_on=FactKey.WEEK_ON_WEEK,
        confidence=schemas.Confidence.HIGH,
    )


def _peak_window_recommendation(
    facts: analytics.ConsumptionFacts,
) -> schemas.ModelRecommendation | None:
    window = facts.peak_window
    start = f"{window.start_hour:02d}:00"
    end = f"{window.end_hour:02d}:00"

    return schemas.ModelRecommendation(
        title=f"Move heavy appliance use away from {start} to {end}",
        rationale=(
            f"{window.share_percent}% of your electricity is used between {start} and "
            f"{end} ({facts.timezone_name}). Running the washing machine or dishwasher "
            "outside that window spreads your demand more evenly."
        ),
        based_on=FactKey.PEAK_WINDOW,
        confidence=schemas.Confidence.MEDIUM,
    )


def _total_consumption_recommendation(
    facts: analytics.ConsumptionFacts,
) -> schemas.ModelRecommendation:
    """
    The one recommendation that is always available.

    It is generic, and its confidence says so, but it guarantees the service
    always has something to return.
    """
    return schemas.ModelRecommendation(
        title="Review your overall electricity use",
        rationale=(
            f"You used {facts.total_consumption_kwh} kWh over {facts.days_covered} days. "
            "Small changes such as lowering the thermostat by a degree and switching off "
            "standby power add up across a whole home."
        ),
        based_on=FactKey.TOTAL_CONSUMPTION,
        confidence=schemas.Confidence.LOW,
    )
