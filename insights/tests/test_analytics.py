"""
Tests for the arithmetic layer.

These are the tests that matter most. They are also free and instant, because
nothing here touches a network, a database or a language model.
"""

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from insights import analytics
from insights.analytics import ReadingQuality

LONDON = ZoneInfo("Europe/London")


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


class TestValidation:
    def test_empty_reading_set_is_rejected(self):
        with pytest.raises(analytics.NotEnoughReadingsError):
            analytics.compute_facts([])

    def test_naive_timestamps_are_rejected(self):
        naive = analytics.Reading(
            timestamp=dt.datetime(2026, 1, 1, 12, 0),
            value=Decimal("1"),
            quality=ReadingQuality.ACTUAL,
        )

        with pytest.raises(ValueError, match="timezone-aware"):
            analytics.compute_facts([naive])

    @pytest.mark.parametrize("width", [0, -1, 25])
    def test_peak_window_width_must_fit_in_a_day(self, width):
        with pytest.raises(ValueError, match="between 1 and 24"):
            analytics.compute_facts([reading(at(), "1")], peak_window_hours=width)


class TestTotalConsumption:
    def test_decimal_arithmetic_is_exact(self):
        """
        The reason the whole module uses Decimal.

        In binary floating point 0.1 + 0.2 is 0.30000000000000004, and that error
        compounds across thousands of readings into a total that does not match
        the customer's bill.
        """
        assert 0.1 + 0.2 != 0.3

        facts = analytics.compute_facts([reading(at(hour=0), "0.1"), reading(at(hour=1), "0.2")])

        assert facts.total_consumption_kwh == Decimal("0.3")

    def test_readings_are_summed_regardless_of_input_order(self):
        facts = analytics.compute_facts(
            [
                reading(at(hour=5), "3.5"),
                reading(at(hour=1), "1.25"),
                reading(at(hour=3), "2.25"),
            ]
        )

        assert facts.total_consumption_kwh == Decimal("7.00")
        assert facts.period_start == at(hour=1)
        assert facts.period_end == at(hour=5)
        assert facts.reading_count == 3

    def test_days_covered_counts_inclusive_days(self):
        facts = analytics.compute_facts(
            [reading(at(day=1, hour=0), "1"), reading(at(day=3, hour=23), "1")]
        )

        assert facts.days_covered == 3


class TestQualityBreakdown:
    def test_consumption_is_split_by_quality(self):
        facts = analytics.compute_facts(
            [
                reading(at(hour=0), "30", ReadingQuality.ACTUAL),
                reading(at(hour=1), "10", ReadingQuality.ESTIMATE),
                reading(at(hour=2), "10", ReadingQuality.ESTIMATE),
            ]
        )

        by_quality = {row.quality: row for row in facts.quality_breakdown}
        assert by_quality[ReadingQuality.ACTUAL].consumption_kwh == Decimal("30")
        assert by_quality[ReadingQuality.ACTUAL].reading_count == 1
        assert by_quality[ReadingQuality.ACTUAL].share_percent == Decimal("60.0")
        assert by_quality[ReadingQuality.ESTIMATE].consumption_kwh == Decimal("20")
        assert by_quality[ReadingQuality.ESTIMATE].reading_count == 2
        assert by_quality[ReadingQuality.ESTIMATE].share_percent == Decimal("40.0")

    def test_breakdown_order_does_not_depend_on_input_order(self):
        """Step 7 caches on a hash of these facts, so the ordering has to be fixed."""
        readings = [
            reading(at(hour=0), "1", ReadingQuality.ZEROED),
            reading(at(hour=1), "1", ReadingQuality.ESTIMATE),
            reading(at(hour=2), "1", ReadingQuality.ACTUAL),
            reading(at(hour=3), "1", ReadingQuality.CALCULATED),
        ]

        forwards = analytics.compute_facts(readings)
        backwards = analytics.compute_facts(list(reversed(readings)))

        assert [row.quality for row in forwards.quality_breakdown] == [
            ReadingQuality.ACTUAL,
            ReadingQuality.ESTIMATE,
            ReadingQuality.CALCULATED,
            ReadingQuality.ZEROED,
        ]
        assert forwards.quality_breakdown == backwards.quality_breakdown

    def test_qualities_that_are_absent_are_omitted(self):
        facts = analytics.compute_facts([reading(at(), "1", ReadingQuality.ACTUAL)])

        assert [row.quality for row in facts.quality_breakdown] == [ReadingQuality.ACTUAL]


class TestEstimatedShare:
    def test_is_zero_when_every_reading_is_actual(self):
        facts = analytics.compute_facts([reading(at(hour=0), "5"), reading(at(hour=1), "5")])

        assert facts.estimated_share_percent == Decimal("0.0")

    def test_counts_everything_that_is_not_a_real_meter_read(self):
        facts = analytics.compute_facts(
            [
                reading(at(hour=0), "40", ReadingQuality.ACTUAL),
                reading(at(hour=1), "30", ReadingQuality.ESTIMATE),
                reading(at(hour=2), "30", ReadingQuality.CALCULATED),
            ]
        )

        assert facts.estimated_share_percent == Decimal("60.0")

    def test_zero_consumption_does_not_divide_by_zero(self):
        facts = analytics.compute_facts(
            [
                reading(at(hour=0), "0", ReadingQuality.ZEROED),
                reading(at(hour=1), "0", ReadingQuality.ZEROED),
            ]
        )

        assert facts.total_consumption_kwh == Decimal("0")
        assert facts.estimated_share_percent == Decimal("0.0")


class TestPeakWindow:
    def test_finds_the_highest_consuming_block_of_hours(self):
        values = dict.fromkeys(range(24), "1")
        values.update({17: "10", 18: "10", 19: "10"})

        facts = analytics.compute_facts(hourly_day(values))

        assert facts.peak_window.start_hour == 17
        assert facts.peak_window.end_hour == 20
        assert facts.peak_window.consumption_kwh == Decimal("30")
        # 30 of 51 kWh total.
        assert facts.peak_window.share_percent == Decimal("58.8")

    def test_window_can_wrap_past_midnight(self):
        values = dict.fromkeys(range(24), "1")
        values.update({22: "10", 23: "10", 0: "10"})

        facts = analytics.compute_facts(hourly_day(values))

        assert facts.peak_window.start_hour == 22
        assert facts.peak_window.end_hour == 1
        assert facts.peak_window.consumption_kwh == Decimal("30")

    def test_ties_resolve_to_the_earliest_window(self):
        facts = analytics.compute_facts(hourly_day(dict.fromkeys(range(24), "1")))

        assert facts.peak_window.start_hour == 0
        assert facts.peak_window.end_hour == 3

    def test_hours_are_expressed_in_the_requested_timezone(self):
        """
        A customer reads "18:00 to 21:00" as their own clock, not as UTC.

        These readings peak at 17:00-20:00 UTC, which during British Summer Time
        is 18:00-21:00 in London.
        """
        values = dict.fromkeys(range(24), "1")
        values.update({17: "10", 18: "10", 19: "10"})
        readings = hourly_day(values, month=7)

        utc_facts = analytics.compute_facts(readings)
        london_facts = analytics.compute_facts(readings, timezone=LONDON)

        assert utc_facts.peak_window.start_hour == 17
        assert london_facts.peak_window.start_hour == 18
        assert london_facts.peak_window.end_hour == 21
        assert london_facts.timezone_name == "Europe/London"

    def test_window_width_is_configurable(self):
        values = dict.fromkeys(range(24), "1")
        values.update({17: "10", 18: "10", 19: "10"})

        facts = analytics.compute_facts(hourly_day(values), peak_window_hours=1)

        assert facts.peak_window.start_hour == 17
        assert facts.peak_window.end_hour == 18
        assert facts.peak_window.consumption_kwh == Decimal("10")


class TestWeekOnWeek:
    def test_reports_an_increase(self):
        start = at(day=1)
        readings = [
            *half_hourly(start, days=7, value="1.00"),
            *half_hourly(start + dt.timedelta(days=7), days=7, value="1.20"),
        ]

        change = analytics.compute_facts(readings).week_on_week

        assert change is not None
        assert change.previous_week_kwh == Decimal("336.00")
        assert change.latest_week_kwh == Decimal("403.20")
        assert change.change_percent == Decimal("20.0")

    def test_reports_a_decrease(self):
        start = at(day=1)
        readings = [
            *half_hourly(start, days=7, value="2.00"),
            *half_hourly(start + dt.timedelta(days=7), days=7, value="1.50"),
        ]

        change = analytics.compute_facts(readings).week_on_week

        assert change is not None
        assert change.change_percent == Decimal("-25.0")

    def test_is_withheld_when_there_is_no_earlier_week(self):
        facts = analytics.compute_facts(half_hourly(at(day=1), days=5, value="1.00"))

        assert facts.week_on_week is None

    def test_is_withheld_when_the_earlier_week_is_only_partly_covered(self):
        """
        Ten days of history means the earlier week is three days long.

        Comparing a full week against a partial one yields a large, confident and
        entirely artificial change, so no figure is published at all.
        """
        facts = analytics.compute_facts(half_hourly(at(day=1), days=10, value="1.00"))

        assert facts.week_on_week is None

    def test_is_withheld_when_the_earlier_week_consumed_nothing(self):
        """Percentage change from a zero baseline is undefined, not infinite."""
        start = at(day=1)
        readings = [
            *half_hourly(start, days=7, value="0.00", quality=ReadingQuality.ZEROED),
            *half_hourly(start + dt.timedelta(days=7), days=7, value="1.00"),
        ]

        facts = analytics.compute_facts(readings)

        assert facts.total_consumption_kwh == Decimal("336.00")
        assert facts.week_on_week is None
