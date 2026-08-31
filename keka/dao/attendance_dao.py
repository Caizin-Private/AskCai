"""
keka/dao/attendance_dao.py
Raw Keka holiday-calendar access. No business logic, no mapping.

Endpoints — https://developers.keka.com/reference/
  GET /time/holidayscalendar                        every calendar in the org
  GET /time/holidayscalendar/{calendarId}/holidays  holidays in one calendar

The calendar an employee follows is `holidayCalendarId` on their /hris/employees
record. Requires the `Attendance` scope on the Keka API key.
"""

import logging

from keka.dao._http import cached, get_all

logger = logging.getLogger(__name__)


def fetch_holidays(calendar_id: str, year: int) -> list:
    """
    GET /time/holidayscalendar/{calendarId}/holidays?calendarYear=...

    Returns raw rows: {id, name, date, isFloater}
    Cached for a day — a holiday calendar changes once a year.
    """
    return cached(
        "holidays",
        f"{calendar_id}|{year}",
        lambda: get_all(
            f"/time/holidayscalendar/{calendar_id}/holidays",
            {"calendarYear": year},
            what=f"GET /time/holidayscalendar/{calendar_id}/holidays",
        ),
    )


def fetch_calendars() -> list:
    """
    GET /time/holidayscalendar

    Fallback for an employee whose record carries no holidayCalendarId.
    """
    return cached(
        "holidays",
        "__calendars__",
        lambda: get_all("/time/holidayscalendar", {}, what="GET /time/holidayscalendar"),
    )
