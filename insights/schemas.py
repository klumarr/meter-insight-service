"""
The contracts at the service's three boundaries.

There are three, and keeping them apart matters:

1.  What a client may send us (InsightRequest).
2.  What the language model must return (ModelInsightResponse). This one is
    handed to the model as a JSON schema *and* used to validate its reply, so a
    single definition both instructs and polices it.
3.  What we return to the client (InsightResponse), which is the model's
    wording with our own arithmetic attached.

The model's contract is deliberately the smallest of the three. It contains no
numbers at all: the only quantitative thing the model is asked for is the name
of the fact each recommendation rests on, and every figure is calculated from
that by insights.analytics.
"""

from collections import Counter
from collections.abc import Iterable
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from insights import analytics
from insights.analytics import FactKey, ReadingQuality

# A year of half-hourly readings. The facts are computed from the whole set at
# once, so an unbounded list would let a single request exhaust memory.
MAX_READINGS = 35_136

MAX_RECOMMENDATIONS = 3


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class InsightSource(StrEnum):
    """
    Where the wording came from.

    This is returned to the caller rather than hidden, so that a fallback
    response is never mistaken for a model-written one. Degrading silently is
    worse than degrading visibly: a caller can react to what it can see.
    """

    MODEL = "model"
    FALLBACK = "fallback"


class ReadingPayload(BaseModel):
    """One reading as it arrives over HTTP."""

    model_config = ConfigDict(extra="forbid")

    # AwareDatetime rather than datetime: a timestamp without an offset is
    # ambiguous, and the analytics layer refuses to work with one.
    timestamp: AwareDatetime
    value: Annotated[Decimal, Field(ge=0)]
    quality: ReadingQuality


class InsightRequest(BaseModel):
    """
    The request body for POST /api/insights.

    extra="forbid" means an unrecognised field is an error rather than something
    quietly ignored. A client that misspells a field name should hear about it
    immediately, not wonder why its value had no effect.
    """

    model_config = ConfigDict(extra="forbid")

    readings: Annotated[list[ReadingPayload], Field(min_length=1, max_length=MAX_READINGS)]
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _must_be_a_known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown timezone {value!r}") from error
        return value

    def to_readings(self) -> list[analytics.Reading]:
        return [
            analytics.Reading(
                timestamp=payload.timestamp, value=payload.value, quality=payload.quality
            )
            for payload in self.readings
        ]

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


# The two classes below are different from every other class in this file,
# because their docstrings and field descriptions are published in the JSON
# schema that is sent to the model. That text is prompt, not developer notes,
# and it costs tokens on every call, so it is written for the model and kept
# short. Anything a developer needs to know is in comments like this one.
#
# What matters about ModelRecommendation is what it does *not* contain: no
# saving figure, and no number of any kind. The model's only quantitative
# responsibility is to name the fact it reasoned from, and every figure follows
# from that name in insights.analytics.
class ModelRecommendation(BaseModel):
    """One energy-saving recommendation."""

    model_config = ConfigDict(extra="forbid")

    title: Annotated[
        str,
        Field(min_length=1, max_length=80, description="A short, actionable headline."),
    ]
    rationale: Annotated[
        str,
        Field(
            min_length=1,
            max_length=300,
            description=(
                "One or two plain-English sentences explaining the advice, "
                "referring only to the figures supplied."
            ),
        ),
    ]
    based_on: Annotated[
        FactKey,
        Field(description="The supplied fact this recommendation is derived from."),
    ]
    confidence: Annotated[
        Confidence,
        Field(description="How well this advice is supported by the figures supplied."),
    ]


# This class is the instruction and the validator at once: its JSON schema is
# sent with the request to tell the model what shape to produce, and the reply
# is parsed back through the same class before any of it is trusted. One
# definition, so the two can never drift apart.
class ModelInsightResponse(BaseModel):
    """Between one and three recommendations, most valuable first."""

    model_config = ConfigDict(extra="forbid")

    recommendations: Annotated[
        list[ModelRecommendation], Field(min_length=1, max_length=MAX_RECOMMENDATIONS)
    ]


class Recommendation(BaseModel):
    """A recommendation as returned to the caller: the model's words, our number."""

    title: str
    rationale: str
    based_on: FactKey
    confidence: Confidence
    estimated_saving_kwh: Decimal | None


class InsightResponse(BaseModel):
    """
    The service's reply.

    The facts are returned alongside the recommendations on purpose. Every
    recommendation names the fact it rests on, and that fact is right there in
    the same payload, so a reader can check the advice against the arithmetic
    without a second request. Advice that cannot be checked has negative value.
    """

    source: InsightSource
    facts: analytics.ConsumptionFacts
    recommendations: list[Recommendation]


class CitationError(ValueError):
    """Base class for the ways a set of citations can be unusable."""


class UncitedFactError(CitationError):
    """Raised when the model attributes a recommendation to a fact it was never given."""


class DuplicateCitationError(CitationError):
    """Raised when two recommendations rest on the same fact."""


def check_citations(response: ModelInsightResponse, facts: analytics.ConsumptionFacts) -> None:
    """
    Reject a set of citations that cannot be trusted, for either of two reasons.

    A fact that was never supplied means the model invented its justification,
    which makes the whole reply untrustworthy rather than just the one
    recommendation. The static schema cannot catch this: it can only enforce
    that `based_on` is one of the four known names, whereas whether a given
    fact *exists* depends on the readings.

    The same fact cited twice is subtler, and every individual figure stays
    correct while the set of them stops being. Each recommendation carries the
    saving derived from the fact it names, so two resting on the same fact
    report that one saving twice, and a caller adding them up is told a window
    can yield twice what it holds.

    Both problems are reported together, because there is only one retry and it
    should not be spent learning about half of what was wrong. The messages are
    written to be fed straight back to the model.
    """
    available = analytics.available_fact_keys(facts)
    cited = [rec.based_on for rec in response.recommendations]

    problems = []
    invented = set(cited) - available
    if invented:
        problems.append(
            f"recommendations cite facts that were not provided: {_names(invented)}. "
            f"Only these facts are available: {_names(available)}."
        )

    duplicated = {key for key, count in Counter(cited).items() if count > 1}
    if duplicated:
        problems.append(
            f"the same fact is cited more than once: {_names(duplicated)}. Each "
            "recommendation must be attributed to a different fact, because two "
            "resting on the same fact report the same saving twice."
        )

    if not problems:
        return

    # Invention is the graver fault, so it names the error when both occur.
    error_type = UncitedFactError if invented else DuplicateCitationError
    raise error_type(" ".join(problems))


def build_response(
    model_response: ModelInsightResponse,
    *,
    facts: analytics.ConsumptionFacts,
    source: InsightSource = InsightSource.MODEL,
) -> InsightResponse:
    """Attach our computed saving to each of the model's recommendations."""
    return InsightResponse(
        source=source,
        facts=facts,
        recommendations=[
            Recommendation(
                title=rec.title,
                rationale=rec.rationale,
                based_on=rec.based_on,
                confidence=rec.confidence,
                estimated_saving_kwh=analytics.estimated_saving_kwh(rec.based_on, facts),
            )
            for rec in model_response.recommendations
        ],
    )


def _names(keys: Iterable[FactKey]) -> str:
    return ", ".join(sorted(str(key) for key in keys))
