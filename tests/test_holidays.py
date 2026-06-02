"""
tests/test_holidays.py
Unit tests for keka/holidays.py — pure logic, zero mocks needed.

Verified 2099 weekday map (python datetime confirmed):
  2099-01-03 = Saturday  (weekend)
  2099-01-04 = Sunday    (weekend)
  2099-01-05 = Monday    (weekday)
  2099-01-06 = Tuesday   (weekday)
  2099-01-07 = Wednesday (weekday)
  2099-01-09 = Friday    (weekday)
  2099-01-10 = Saturday  (weekend)
  2099-01-11 = Sunday    (weekend)
  2099-01-12 = Monday    (weekday)

Public holiday used for holiday tests:
  2026-10-02 = Friday    (Gandhi Jayanti — in PUBLIC_HOLIDAYS_2026, future date)
"""

import pytest
from keka.holidays import (
    is_weekend,
    is_holiday,
    is_working_day,
    count_working_days,
    validate_leave_dates,
    PUBLIC_HOLIDAYS_2026,
)


# ---------------------------------------------------------------------------
# is_weekend
# ---------------------------------------------------------------------------

class TestIsWeekend:
    def test_saturday_is_weekend(self):
        assert is_weekend("2099-01-03") is True       # Saturday

    def test_sunday_is_weekend(self):
        assert is_weekend("2099-01-04") is True       # Sunday

    def test_monday_is_not_weekend(self):
        assert is_weekend("2099-01-05") is False      # Monday

    def test_wednesday_is_not_weekend(self):
        assert is_weekend("2099-01-07") is False      # Wednesday

    def test_friday_is_not_weekend(self):
        assert is_weekend("2099-01-09") is False      # Friday


# ---------------------------------------------------------------------------
# is_holiday
# ---------------------------------------------------------------------------

class TestIsHoliday:
    def test_republic_day_is_holiday(self):
        assert is_holiday("2026-01-26") is True

    def test_independence_day_is_holiday(self):
        assert is_holiday("2026-08-15") is True

    def test_christmas_is_holiday(self):
        assert is_holiday("2026-12-25") is True

    def test_gandhi_jayanti_is_holiday(self):
        assert is_holiday("2026-10-02") is True

    def test_regular_weekday_is_not_holiday(self):
        assert is_holiday("2026-02-02") is False

    def test_future_year_not_in_2026_list(self):
        # Our holiday list only covers 2026 — 2099 dates are never in it
        assert is_holiday("2099-01-26") is False

    def test_all_declared_holidays_are_recognised(self):
        for h in PUBLIC_HOLIDAYS_2026:
            assert is_holiday(h) is True, f"{h} should be a holiday"


# ---------------------------------------------------------------------------
# is_working_day
# ---------------------------------------------------------------------------

class TestIsWorkingDay:
    def test_weekday_non_holiday_is_working(self):
        assert is_working_day("2099-01-05") is True   # Monday

    def test_saturday_is_not_working(self):
        assert is_working_day("2099-01-03") is False  # Saturday

    def test_sunday_is_not_working(self):
        assert is_working_day("2099-01-04") is False  # Sunday

    def test_public_holiday_is_not_working(self):
        assert is_working_day("2026-10-02") is False  # Gandhi Jayanti (Friday)


# ---------------------------------------------------------------------------
# count_working_days
# ---------------------------------------------------------------------------

class TestCountWorkingDays:
    def test_full_week_mon_to_fri(self):
        # 2099-01-05 Mon → 2099-01-09 Fri = 5 working days
        assert count_working_days("2099-01-05", "2099-01-09") == 5

    def test_single_working_day(self):
        assert count_working_days("2099-01-05", "2099-01-05") == 1

    def test_weekend_only_range_returns_zero(self):
        # 2099-01-03 Sat → 2099-01-04 Sun = 0 working days
        assert count_working_days("2099-01-03", "2099-01-04") == 0

    def test_range_spanning_weekend(self):
        # 2099-01-09 Fri → 2099-01-12 Mon = 2 working days (Fri + Mon)
        assert count_working_days("2099-01-09", "2099-01-12") == 2

    def test_public_holiday_excluded(self):
        # 2026-10-02 is Gandhi Jayanti (Friday), so Mon–Fri range = 4 working days
        assert count_working_days("2026-09-28", "2026-10-02") == 4

    def test_two_week_range(self):
        # Mon 05 → Fri 09 (5) + Sat 10, Sun 11 + Mon 12 → Fri 16 (5) = 10
        assert count_working_days("2099-01-05", "2099-01-16") == 10


# ---------------------------------------------------------------------------
# validate_leave_dates
# ---------------------------------------------------------------------------

class TestValidateLeaveDates:

    # ── Past date ────────────────────────────────────────────────────────────
    def test_past_from_date_returns_error(self):
        error = validate_leave_dates("2020-01-06", "2020-01-08")
        assert error is not None
        assert "past" in error.lower()

    # ── Reversed range ───────────────────────────────────────────────────────
    def test_to_before_from_returns_error(self):
        error = validate_leave_dates("2099-01-10", "2099-01-05")
        assert error is not None
        assert "before" in error.lower()

    # ── Weekend start ────────────────────────────────────────────────────────
    def test_saturday_start_returns_error(self):
        # 2099-01-03 = Saturday
        error = validate_leave_dates("2099-01-03", "2099-01-05")
        assert error is not None
        assert "Saturday" in error or "weekend" in error.lower()

    def test_sunday_start_returns_error(self):
        # 2099-01-04 = Sunday
        error = validate_leave_dates("2099-01-04", "2099-01-05")
        assert error is not None
        assert "Sunday" in error or "weekend" in error.lower()

    # ── Public holiday start ─────────────────────────────────────────────────
    def test_public_holiday_start_returns_error(self):
        # 2026-10-02 = Gandhi Jayanti (Friday, future date)
        error = validate_leave_dates("2026-10-02", "2026-10-05")
        assert error is not None
        assert "holiday" in error.lower()

    # ── All-weekend range ────────────────────────────────────────────────────
    def test_all_weekend_range_returns_error(self):
        # 2099-01-03 Sat → 2099-01-04 Sun — weekend check fires on from_date
        error = validate_leave_dates("2099-01-03", "2099-01-04")
        assert error is not None

    # ── Valid range ───────────────────────────────────────────────────────────
    def test_valid_weekday_range_returns_none(self):
        # 2099-01-05 Mon → 2099-01-07 Wed (all weekdays, no holidays)
        error = validate_leave_dates("2099-01-05", "2099-01-07")
        assert error is None

    def test_valid_single_day_returns_none(self):
        error = validate_leave_dates("2099-01-07", "2099-01-07")  # Wednesday
        assert error is None

    # ── Invalid date string ───────────────────────────────────────────────────
    def test_bad_date_format_returns_error(self):
        error = validate_leave_dates("07-01-2099", "09-01-2099")
        assert error is not None
        assert "invalid" in error.lower()

    def test_same_day_valid(self):
        error = validate_leave_dates("2099-03-10", "2099-03-10")  # Tuesday
        assert error is None
