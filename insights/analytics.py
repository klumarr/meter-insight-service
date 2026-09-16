"""
Consumption analytics.

This module is the arithmetic half of the service, and the half that has to be
correct. It depends on nothing but the standard library: no Django, no Pydantic,
and above all no language model. Every figure the service publishes is computed
here, and every one of them is covered by a unit test that costs nothing to run.

All arithmetic uses Decimal. Binary floating point cannot represent decimal
fractions exactly, and over thousands of readings those errors accumulate into a
total that does not match the customer's bill.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

# The width of the window reported as the customer's peak usage period. Three
# hours is wide enough to describe a habit ("your evening peak") rather than a
# single spike.
PEAK_WINDOW_HOURS = 3

# A week-on-week figure is only published when the earlier week is at least this
# well covered relative to the later one. See _week_on_week for why.
MINIMUM_COMPARABLE_COVERAGE = Decimal("0.9")

# How much busier than an even spread a window has to be before it counts as a
# peak. Three hours out of twenty-four hold 12.5% of the day no matter how the
# usage falls, so a "peak" at 12.5% is not a finding, and advice to shift load
# away from it rests on nothing. Requiring 1.3x an even spread means the window
# is at least somewhat concentrated before the service will act on it.
MEANINGFUL_PEAK_MULTIPLE = Decimal("1.3")

# The share of peak-window consumption a household can realistically move to a
# cheaper period. This is a stated assumption rather than a measured value, and
# it lives here, named and tested, precisely so that it can be found, argued
# with and changed. The alternative is a language model inventing a different
# figure on every call, which nobody can find, explain or adjust.
SHIFTABLE_PEAK_SHARE = Decimal("0.15")

# The share of total consumption that general efficiency measures tend to save.
# The same reasoning as SHIFTABLE_PEAK_SHARE applies.
GENERAL_EFFICIENCY_SHARE = Decimal("0.05")

_PERCENT_PRECISION = Decimal("0.1")
_KWH_PRECISION = Decimal("0.01")
_HOURS_IN_DAY = 24


class ReadingQuality(StrEnum):
    """
    How a reading came to exist.

    ACTUAL is a real read taken from the meter. ESTIMATE is inferred from
    historic usage, CALCULATED is derived from surrounding readings, and ZEROED
    is a substituted zero. Only ACTUAL is direct evidence of what was consumed,
    which is why the estimated share is reported to the customer at all.
    """

    ACTUAL = "ACTUAL"
    ESTIMATE = "ESTIMATE"
    CALCULATED = "CALCULATED"
    ZEROED = "ZEROED"


# Every recommendation the service returns has to point at the fact it rests on.
# That makes the advice checkable in seconds, and it makes a fabricated
# justification detectable: a citation of a fact that was never derived is proof
# the wording was invented rather than reasoned from the data.
#
# The docstring below is one line on purpose. It is published in the JSON schema
# sent to the model, which makes it prompt text rather than developer notes.
class FactKey(StrEnum):
    """The facts a recommendation may be attributed to."""

    TOTAL_CONSUMPTION = "total_consumption"
    ESTIMATED_SHARE = "estimated_share"
    PEAK_WINDOW = "peak_window"
    WEEK_ON_WEEK = "week_on_week"


class NotEnoughReadingsError(ValueError):
    """Raised when the reading set is too small to compute anything from."""


@dataclass(frozen=True, slots=True)
class Reading:
    """
    A single interval reading: the consumption recorded during one period.

    The timestamp marks the end of the interval the value covers, and must be
    timezone-aware.
    """

    timestamp: dt.datetime
    value: Decimal
    quality: ReadingQuality


@dataclass(frozen=True, slots=True)
class QualityBreakdown:
    """How much consumption came from readings of one particular quality."""

    quality: ReadingQuality
    consumption_kwh: Decimal
    reading_count: int
    share_percent: Decimal


@dataclass(frozen=True, slots=True)
class PeakWindow:
    """
    The hours of the day during which the most consumption occurs.

    Hours are given in the timezone the facts were computed in. end_hour is
    exclusive and may be lower than start_hour, because a window can wrap past
    midnight.

    A household whose usage is evenly spread has no peak, and is described by
    the absence of this object rather than by a window that technically holds
    the most.
    """

    start_hour: int
    end_hour: int
    consumption_kwh: Decimal
    share_percent: Decimal


@dataclass(frozen=True, slots=True)
class WeekOnWeekChange:
    """
    The change between the most recent seven days and the seven before them.

    The two totals are carried alongside the percentage so that the figure can
    be checked rather than taken on trust. They are grouped into one object
    because a percentage without its baseline is not verifiable, and this way
    the three values cannot become separated.
    """

    latest_week_kwh: Decimal
    previous_week_kwh: Decimal
    change_percent: Decimal


@dataclass(frozen=True, slots=True)
class ConsumptionFacts:
    """
    The complete set of figures derived from a reading set.

    This is the only thing the language model is ever shown. It is small by
    design: a handful of facts costs a few dozen tokens, where the readings they
    came from would run to thousands.
    """

    period_start: dt.datetime
    period_end: dt.datetime
    days_covered: int
    reading_count: int
    total_consumption_kwh: Decimal
    # Derived from the two figures above, and published anyway. An eval against
    # the live model caught it dividing one by the other to write "which works
    # out to 48 kWh per day". The urge is a good one -- a daily average means
    # more to a household than a fortnightly total -- so the answer is to
    # calculate it here, where it is exact and tested, rather than to forbid it
    # more loudly. A model that needs a figure it was not given will make one.
    average_daily_kwh: Decimal
    quality_breakdown: tuple[QualityBreakdown, ...]
    estimated_share_percent: Decimal
    # The complement of the line above, for the same reason. The model was
    # caught subtracting it from a hundred to write "100% accurate".
    measured_share_percent: Decimal
    peak_window: PeakWindow | None
    week_on_week: WeekOnWeekChange | None
    timezone_name: str


def compute_facts(
    readings: Sequence[Reading],
    *,
    timezone: dt.tzinfo = dt.UTC,
    peak_window_hours: int = PEAK_WINDOW_HOURS,
) -> ConsumptionFacts:
    """
    Reduce a set of interval readings to the facts the service reports.

    `timezone` controls which local hours the peak window is expressed in. A
    customer told their peak is "17:00 to 20:00" means their own clock, so
    bucketing in UTC would be wrong for anywhere that is not on it.
    """
    if not readings:
        raise NotEnoughReadingsError("at least one reading is required")

    if not 1 <= peak_window_hours <= _HOURS_IN_DAY:
        raise ValueError(f"peak_window_hours must be between 1 and 24, got {peak_window_hours}")

    naive_count = sum(1 for reading in readings if reading.timestamp.tzinfo is None)
    if naive_count:
        raise ValueError(
            "every reading timestamp must be timezone-aware, "
            f"but {naive_count} of {len(readings)} were naive"
        )

    ordered = sorted(readings, key=lambda reading: reading.timestamp)
    total = sum((reading.value for reading in ordered), Decimal(0))
    days = (ordered[-1].timestamp - ordered[0].timestamp).days + 1
    measured = _measured_consumption(ordered)

    return ConsumptionFacts(
        period_start=ordered[0].timestamp,
        period_end=ordered[-1].timestamp,
        days_covered=days,
        reading_count=len(ordered),
        total_consumption_kwh=total,
        average_daily_kwh=_round_kwh(total / days),
        quality_breakdown=_quality_breakdown(ordered, total=total),
        estimated_share_percent=_percentage(total - measured, total),
        measured_share_percent=_percentage(measured, total),
        peak_window=_peak_window(ordered, timezone=timezone, width=peak_window_hours, total=total),
        week_on_week=_week_on_week(ordered),
        timezone_name=getattr(timezone, "key", str(timezone)),
    )


def available_fact_keys(facts: ConsumptionFacts) -> frozenset[FactKey]:
    """
    The facts that were actually derived from a given reading set.

    Two of the four always exist. WEEK_ON_WEEK is withheld when there is too
    little history for the comparison to mean anything, and PEAK_WINDOW when
    usage is spread evenly enough that no window is worth calling a peak. In
    both cases the figure could be produced, and would be arithmetically
    correct, and would support advice that rests on nothing.

    A recommendation may only be attributed to a fact in this set.
    """
    keys = {
        FactKey.TOTAL_CONSUMPTION,
        FactKey.ESTIMATED_SHARE,
    }
    if facts.peak_window is not None:
        keys.add(FactKey.PEAK_WINDOW)
    if facts.week_on_week is not None:
        keys.add(FactKey.WEEK_ON_WEEK)
    return frozenset(keys)


def estimated_saving_kwh(fact_key: FactKey, facts: ConsumptionFacts) -> Decimal | None:
    """
    How much consumption a recommendation resting on `fact_key` could save.

    Returns None when no kWh figure would be honest, rather than inventing one.
    The same facts always produce the same number, which is the entire point:
    this is the calculation a language model is not permitted to make.
    """
    match fact_key:
        case FactKey.WEEK_ON_WEEK:
            if facts.week_on_week is None:
                return None
            # Not an assumption at all. Returning to the earlier week's usage
            # saves exactly the difference between the two weeks.
            increase = facts.week_on_week.latest_week_kwh - facts.week_on_week.previous_week_kwh
            return _round_kwh(increase) if increase > 0 else None

        case FactKey.PEAK_WINDOW:
            if facts.peak_window is None:
                return None
            return _round_kwh(facts.peak_window.consumption_kwh * SHIFTABLE_PEAK_SHARE)

        case FactKey.TOTAL_CONSUMPTION:
            return _round_kwh(facts.total_consumption_kwh * GENERAL_EFFICIENCY_SHARE)

        case FactKey.ESTIMATED_SHARE:
            # Replacing estimated readings with real ones corrects what the
            # customer is billed for, not what they consume. A kWh saving here
            # would be a fiction, so none is offered.
            return None


def _round_kwh(value: Decimal) -> Decimal:
    return value.quantize(_KWH_PRECISION, rounding=ROUND_HALF_UP)


def _percentage(part: Decimal, whole: Decimal) -> Decimal:
    if whole == 0:
        return Decimal("0.0")
    return (part / whole * 100).quantize(_PERCENT_PRECISION, rounding=ROUND_HALF_UP)


def _quality_breakdown(
    readings: Sequence[Reading], *, total: Decimal
) -> tuple[QualityBreakdown, ...]:
    consumption: dict[ReadingQuality, Decimal] = {}
    counts: dict[ReadingQuality, int] = {}

    for reading in readings:
        consumption[reading.quality] = consumption.get(reading.quality, Decimal(0)) + reading.value
        counts[reading.quality] = counts.get(reading.quality, 0) + 1

    # Iterating the enum rather than the dict keeps the order fixed regardless of
    # what order the readings arrived in. Step 7 caches on a hash of these facts,
    # and a hash is only useful if identical input produces identical output.
    return tuple(
        QualityBreakdown(
            quality=quality,
            consumption_kwh=consumption[quality],
            reading_count=counts[quality],
            share_percent=_percentage(consumption[quality], total),
        )
        for quality in ReadingQuality
        if quality in consumption
    )


def _measured_consumption(readings: Sequence[Reading]) -> Decimal:
    """
    The consumption that came from a real meter read.

    Only ACTUAL is direct evidence of what was used. ESTIMATE, CALCULATED and
    ZEROED are all inferences of one kind or another, so everything else counts
    towards the estimated share rather than this one.
    """
    return sum(
        (reading.value for reading in readings if reading.quality is ReadingQuality.ACTUAL),
        Decimal(0),
    )


def _peak_window(
    readings: Sequence[Reading], *, timezone: dt.tzinfo, width: int, total: Decimal
) -> PeakWindow | None:
    """
    Find the busiest window of the day, if there is one.

    Some window always holds more than the others, so "busiest" is not by itself
    evidence of a habit. Returns None when the winner is no more concentrated
    than chance would produce, because telling a customer to shift load away
    from a peak they do not have is advice with nothing behind it.
    """
    hourly = [Decimal(0)] * _HOURS_IN_DAY
    for reading in readings:
        hourly[reading.timestamp.astimezone(timezone).hour] += reading.value

    best_start = 0
    best_total = Decimal(0)
    for start in range(_HOURS_IN_DAY):
        # The modulo lets a window wrap past midnight. Overnight usage is a real
        # pattern, and a search that stopped at 23:00 would never find it.
        window_total = sum(
            (hourly[(start + offset) % _HOURS_IN_DAY] for offset in range(width)),
            Decimal(0),
        )
        # Strictly greater, so the earliest of several equal windows wins and the
        # result is deterministic.
        if window_total > best_total:
            best_total = window_total
            best_start = start

    share = _percentage(best_total, total)
    even_spread = _percentage(Decimal(width), Decimal(_HOURS_IN_DAY))
    if share < even_spread * MEANINGFUL_PEAK_MULTIPLE:
        return None

    return PeakWindow(
        start_hour=best_start,
        end_hour=(best_start + width) % _HOURS_IN_DAY,
        consumption_kwh=best_total,
        share_percent=share,
    )


def _week_on_week(readings: Sequence[Reading]) -> WeekOnWeekChange | None:
    """
    Compare the last seven days against the seven before them.

    Returns None rather than a number whenever the comparison would be
    misleading. A confident figure derived from four days of history is worse
    than no figure at all: the caller can omit what is absent, but cannot detect
    that a number it was given is meaningless.
    """
    latest = readings[-1].timestamp
    latest_week_start = latest - dt.timedelta(days=7)
    previous_week_start = latest - dt.timedelta(days=14)

    latest_week = [r for r in readings if latest_week_start < r.timestamp <= latest]
    previous_week = [r for r in readings if previous_week_start < r.timestamp <= latest_week_start]

    if not latest_week or not previous_week:
        return None

    # Comparing a full week against a partial one produces a large, confident and
    # entirely artificial change, so the earlier week has to be comparably covered.
    if len(previous_week) < len(latest_week) * MINIMUM_COMPARABLE_COVERAGE:
        return None

    latest_total = sum((reading.value for reading in latest_week), Decimal(0))
    previous_total = sum((reading.value for reading in previous_week), Decimal(0))

    # Percentage change from a zero baseline is undefined, not infinite.
    if previous_total == 0:
        return None

    return WeekOnWeekChange(
        latest_week_kwh=latest_total,
        previous_week_kwh=previous_total,
        change_percent=((latest_total - previous_total) / previous_total * 100).quantize(
            _PERCENT_PRECISION, rounding=ROUND_HALF_UP
        ),
    )
