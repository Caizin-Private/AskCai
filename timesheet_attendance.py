"""
timesheet_attendance.py — the clock-in mark on the timesheet calendar.

Attendance is not a Keka read. It is the same tracker RDS row the People Pulse tab
already renders: insync_db.get_latest_records() reads one date for every employee,
insync_db.get_attendance_range() reads one employee across a month's grid.

Kept out of both month builders on purpose. keka/timesheet_service.py talks to Keka and
nothing else; timesheet_mock.py synthesises a month with no database at all. This module
is the one place a tracker bucket becomes the contract's `Day.attendance`.

Contract: artifacts/timesheet-ui-contract.yaml (DayAttendance)
"""

import logging

from insync_db import get_attendance_range

logger = logging.getLogger(__name__)

# Tracker bucket (insync_db._bucket) → contract status.
#
# 'Floater Holiday' folds into `leave` and carries its name in `detail`. A floater is a
# leave the employee opts into, so it reads as leave; adding a seventh status for it
# would put a colour in the calendar's legend that almost never appears.
_STATUS = {
    "Office":          "office",
    "WFH":             "wfh",
    "Client Location": "client",
    "Leave":           "leave",
    "Floater Holiday": "leave",
    "Absent":          "absent",
    "Pending":         "pending",
}
_DETAIL = {"Floater Holiday": "Floater holiday"}

# A day the mark cannot describe: nobody clocks in on a weekend or a public holiday.
_NO_ATTENDANCE_DAYS = ("weekend", "holiday")


def _for_day(day: dict, row: dict | None) -> dict | None:
    """
    One day's `attendance`, or None when there is nothing to mark.

    Spill-over days are marked too. They are drawn in the grid, and the tracker row for
    31 August is just as true when the employee is looking at September — leaving that
    Monday blank reads as "you did not come in" rather than "this belongs to August".
    The rest of an out-of-month day stays inert; the contract blanks its capacity,
    status, entries and annotation, and says nothing about this mark.
    """
    if day.get("day_type") in _NO_ATTENDANCE_DAYS:
        return None

    if row:
        status = _STATUS.get(row["bucket"])
        if status:
            return {
                "status":        status,
                "check_in_time": row.get("check_in") if status not in ("leave", "absent") else None,
                "detail":        _DETAIL.get(row["bucket"]),
            }
        logger.warning("[timesheet-attendance] unmapped tracker bucket %r", row["bucket"])

    # Approved leave is known from the leave record even with no tracker row — the two
    # systems are independent, and a leave day with a blank mark would look unrecorded.
    # A half day is not covered: the employee works its other half, so the tracker row
    # is the only thing that can say where from.
    if day.get("day_type") == "leave":
        return {"status": "leave", "check_in_time": None, "detail": None}

    return None


def attach(payload: dict, email: str) -> dict:
    """
    Fill `attendance` on every day of a month payload, in place, and return it.

    The month's own grid decides the span read, so this works for any window `days`
    covers. With no tracker row for a date the day is left unmarked — the UI renders
    that as "no attendance record", which is the truth.
    """
    days = payload.get("days") or []
    if not days:
        return payload

    rows = get_attendance_range(email, days[0]["date"], days[-1]["date"])
    for day in days:
        day["attendance"] = _for_day(day, rows.get(day["date"]))
    return payload
