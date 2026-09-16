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


def facts_with_week_on_week() -> analytics.ConsumptionFacts:
    """
    Fourteen days of history, so every fact including week-on-week is available.

    The earlier week totals 336.00 kWh and the later one 403.20 kWh, a rise of
    20.0% and a difference of exactly 67.20 kWh.
    """
    start = at(day=1)
    return analytics.compute_facts(
        [
            *half_hourly(start, days=7, value="1.00"),
            *half_hourly(start + dt.timedelta(days=7), days=7, value="1.20"),
        ]
    )


def facts_without_week_on_week() -> analytics.ConsumptionFacts:
    """Five days of history, which is too little to compare one week to another."""
    return analytics.compute_facts(half_hourly(at(day=1), days=5, value="1.00"))
