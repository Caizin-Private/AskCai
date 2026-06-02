"""
keka/holidays.py
Caizin public holiday list for 2026.

Update this file each year or replace with a live call to:
  GET /api/v1/time/holidayscalendar  →  get calendar ID
  GET /api/v1/time/holidayscalendar/{id}/holidays  →  get actual dates
(The per-calendar holiday endpoint is not in the current Postman collection.)
"""

from datetime import datetime, timedelta

# All dates in yyyy-MM-dd format
PUBLIC_HOLIDAYS_2026 = {
    "2026-01-01",   # New Year's Day
    "2026-01-26",   # Republic Day
    "2026-03-17",   # Holi (confirm exact date from Caizin HR)
    "2026-04-14",   # Ambedkar Jayanti / Baisakhi
    "2026-04-18",   # Good Friday
    "2026-05-01",   # Maharashtra Day / Labour Day
    "2026-08-15",   # Independence Day
    "2026-10-02",   # Gandhi Jayanti
    "2026-10-24",   # Dussehra (confirm exact date)
    "2026-11-11",   # Diwali (confirm exact date)
    "2026-11-12",   # Diwali holiday
    "2026-12-25",   # Christmas
}


def is_holiday(date_str: str) -> bool:
    """Return True if date_str (yyyy-MM-dd) is a declared public holiday."""
    return date_str in PUBLIC_HOLIDAYS_2026


def is_weekend(date_str: str) -> bool:
    """Return True if date_str (yyyy-MM-dd) falls on Saturday or Sunday."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.weekday() >= 5   # 5 = Saturday, 6 = Sunday


def is_working_day(date_str: str) -> bool:
    """Return True if the date is neither a weekend nor a public holiday."""
    return not is_weekend(date_str) and not is_holiday(date_str)


def count_working_days(from_str: str, to_str: str) -> int:
    """Count working days (Mon–Fri, non-holiday) between two dates inclusive."""
    start   = datetime.strptime(from_str, "%Y-%m-%d")
    end     = datetime.strptime(to_str,   "%Y-%m-%d")
    count   = 0
    current = start
    while current <= end:
        if is_working_day(current.strftime("%Y-%m-%d")):
            count += 1
        current += timedelta(days=1)
    return count


def validate_leave_dates(from_str: str, to_str: str) -> str | None:
    """
    Run all date validations.
    Returns an error message string if invalid, or None if dates are fine.
    """
    from datetime import date as _date

    try:
        from_dt = datetime.strptime(from_str, "%Y-%m-%d").date()
        to_dt   = datetime.strptime(to_str,   "%Y-%m-%d").date()
    except ValueError:
        return "Invalid date format. Please use YYYY-MM-DD."

    today = _date.today()

    if from_dt > to_dt:
        return "From date must be before or equal to To date."

    if from_dt < today:
        return "Cannot apply leave for a past date. Please select today or a future date."

    if is_weekend(from_str):
        day_name = datetime.strptime(from_str, "%Y-%m-%d").strftime("%A")
        return (
            f"**{from_dt.strftime('%d %b %Y')}** is a {day_name}. "
            "Leave cannot start on a weekend — please choose a working day."
        )

    if is_holiday(from_str):
        return (
            f"**{from_dt.strftime('%d %b %Y')}** is a public holiday. "
            "Leaves on public holidays are automatically granted — you don't need to apply."
        )

    working_days = count_working_days(from_str, to_str)
    if working_days == 0:
        return (
            "No working days found in the selected date range "
            "(all days are weekends or public holidays). Please adjust your dates."
        )

    return None   # all good
