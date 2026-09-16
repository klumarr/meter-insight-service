"""
Builders for test data.

Kept in one place so that the analytics tests and the schema tests describe the
same scenarios in the same terms.
"""

import datetime as dt
from decimal import Decimal

from insights import analytics
from insights.analytics import ReadingQuality


def at(day: int = 1, hour: int = 0, minute: int = 0, month: int = 1) -> dt.datetime:
    return dt.datetime(2026, month, day, hour, minute, tzinfo=dt.UTC)


def reading(
    timestamp: dt.datetime,
    value: str,
    quality: ReadingQuality = ReadingQuality.ACTUAL,
) -> analytics.Reading:
    return analytics.Reading(timestamp=timestamp, value=Decimal(value), quality=quality)


def hourly_day(values: dict[int, str], day: int = 1, month: int = 1) -> list[analytics.Reading]:
    """One reading per hour of a single day, using the given hour/value mapping."""
    return [reading(at(day=day, hour=hour, month=month), values[hour]) for hour in range(24)]


def half_hourly(
    start: dt.datetime,
    *,
    days: int,
    value: str,
    quality: ReadingQuality = ReadingQuality.ACTUAL,
) -> list[analytics.Reading]:
    """Readings every 30 minutes, timestamped at the end of each interval."""
    return [
        reading(start + dt.timedelta(minutes=30 * (period + 1)), value, quality)
        for period in range(days * 48)
    ]


# Hours 17, 18 and 19 hold six of the day's forty-eight half-hourly slots, and
# are given 56.25% of its consumption. That is well clear of the 12.5% an even
# spread would put there, so analytics reports a peak rather than withholding
# one. The remaining 43.75% is divided evenly across the other forty-two slots.
PEAK_HOURS = frozenset({17, 18, 19})
_PEAK_SHARE = Decimal("0.5625")
_PEAK_SLOTS = 6
_OFF_PEAK_SLOTS = 42


def peaked_half_hourly(
    start: dt.datetime,
    *,
    days: int,
    daily_kwh: str,
    quality: ReadingQuality = ReadingQuality.ACTUAL,
) -> list[analytics.Reading]:
    """
    Half-hourly readings with a pronounced evening peak.

    The daily total is exactly `daily_kwh`, the same as the flat equivalent, so
    totals and week-on-week figures are unaffected by the shape. Only the
    distribution within the day changes, which is what a real household looks
    like and what gives analytics a peak worth reporting.
    """
    daily = Decimal(daily_kwh)
    # Quantised to the penny-equivalent so the readings carry the same scale a
    # real meter would send, rather than the long tail Decimal division leaves
    # behind. The chosen daily totals divide exactly, and the assertion below
    # keeps it that way if anyone adds one that does not.
    peak_value = (daily * _PEAK_SHARE / _PEAK_SLOTS).quantize(Decimal("0.01"))
    off_peak_value = (daily * (1 - _PEAK_SHARE) / _OFF_PEAK_SLOTS).quantize(Decimal("0.01"))
    assert peak_value * _PEAK_SLOTS + off_peak_value * _OFF_PEAK_SLOTS == daily, (
        f"daily_kwh={daily_kwh} does not divide exactly across the day"
    )

    readings = []
    for period in range(days * 48):
        timestamp = start + dt.timedelta(minutes=30 * (period + 1))
        value = peak_value if timestamp.hour in PEAK_HOURS else off_peak_value
        readings.append(analytics.Reading(timestamp=timestamp, value=value, quality=quality))
    return readings


def facts_with_week_on_week() -> analytics.ConsumptionFacts:
    """
    Fourteen days of history, so every fact including week-on-week is available.

    The earlier week totals 336.00 kWh and the later one 403.20 kWh, a rise of
    20.0% and a difference of exactly 67.20 kWh.
    """
    start = at(day=1)
    return analytics.compute_facts(
        [
            *peaked_half_hourly(start, days=7, daily_kwh="48.00"),
            *peaked_half_hourly(start + dt.timedelta(days=7), days=7, daily_kwh="57.60"),
        ]
    )


def facts_without_week_on_week() -> analytics.ConsumptionFacts:
    """Five days of history, which is too little to compare one week to another."""
    return analytics.compute_facts(peaked_half_hourly(at(day=1), days=5, daily_kwh="48.00"))


def facts_with_unchanged_usage() -> analytics.ConsumptionFacts:
    """
    Fourteen days at an identical daily total, so the change is exactly zero.

    There is enough history for the comparison and the comparison found nothing.
    That is a third outcome rather than a very small fall, and wording that only
    knows about rises and falls has to describe it as one of them.
    """
    return analytics.compute_facts(peaked_half_hourly(at(day=1), days=14, daily_kwh="48.00"))


def facts_with_falling_usage() -> analytics.ConsumptionFacts:
    """The mirror of facts_with_week_on_week: the later week is the lighter one."""
    start = at(day=1)
    return analytics.compute_facts(
        [
            *peaked_half_hourly(start, days=7, daily_kwh="57.60"),
            *peaked_half_hourly(start + dt.timedelta(days=7), days=7, daily_kwh="48.00"),
        ]
    )


def facts_with_flat_usage() -> analytics.ConsumptionFacts:
    """
    Seven days of perfectly even usage, so no window counts as a peak.

    Some three-hour window still holds more than the others, but only by the
    rounding of an even spread. This is the case where reporting a peak would
    be arithmetically true and completely misleading.
    """
    return analytics.compute_facts(half_hourly(at(day=1), days=7, value="1.00"))


def facts_mostly_estimated() -> analytics.ConsumptionFacts:
    """
    Seven days in which most of the consumption was never measured.

    The figures are arithmetically correct and epistemically weak at the same
    time, which is the case worth having advice about.
    """
    start = at(day=1)
    return analytics.compute_facts(
        [
            *peaked_half_hourly(start, days=5, daily_kwh="48.00", quality=ReadingQuality.ESTIMATE),
            *peaked_half_hourly(
                start + dt.timedelta(days=5),
                days=2,
                daily_kwh="48.00",
                quality=ReadingQuality.ACTUAL,
            ),
        ]
    )
