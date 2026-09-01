"""
keka/timesheet_service.py
Builds the timesheet month for the calendar UI, from Keka.

Contract:  artifacts/timesheet-ui-contract.yaml  (GET /api/timesheet/months/{month})
Upstreams: artifacts/keka-timesheet-apis.md

Nothing Keka-shaped escapes this module — no Keka ids in field names, no Keka
enums, no Keka error text. The UI is written against the contract, so this is the
only file that has to change if Keka does.

One month costs five upstream calls cold and, with the caches in keka/dao/_http.py
warm, usually one. That matters: Keka's limit is 50 requests/minute for the entire
tenant, shared with the leave flows.
"""

import calendar as _calendar
import logging
from datetime import date, datetime, timedelta

from keka import config
from keka.dao import attendance_dao, employee_dao, leave_dao, psa_dao
from keka.models import EmployeeNotFoundError, KekaServiceError

logger = logging.getLogger(__name__)

_WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_MONTH_NAME = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]

# Keka LeaveRequestStatus
_LEAVE_APPROVED = 1
# Keka SessionType: 0 first half, 1 second half
_FIRST_HALF, _SECOND_HALF = 0, 1

# How many projects get a distinct colour. The calendar's palette is validated for
# colour-vision deficiency at exactly three categorical hues; the rest render neutral.
_MAX_COLOR_SLOTS = 3


# ── helpers ───────────────────────────────────────────────────────────────────

def _r1(n: float) -> float:
    return round(float(n) + 1e-9, 1)


def _iso_date(value) -> str | None:
    """Keka sends date-times ('2026-08-27T00:00:00'). Keep the date part."""
    if not value:
        return None
    return str(value)[:10]


def _today() -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(config.timezone_name())).date()
    except Exception:
        # zoneinfo missing or a bad tz name: IST, which is what this org runs on.
        from datetime import timezone
        return datetime.now(timezone(timedelta(hours=5, minutes=30))).date()


# ── upstream reads, mapped to plain dicts keyed by date ───────────────────────

def _resolve_employee(email: str) -> dict:
    emp = employee_dao.find_by_email_indexed(email)
    if not emp:
        raise EmployeeNotFoundError(f"No Keka employee found for {email}")
    return emp


def _holiday_map(calendar_id: str, years: set) -> dict:
    """date -> {'name', 'is_floater'} for every holiday in the years touched."""
    if not calendar_id:
        logger.warning("[timesheet] employee has no holidayCalendarId — no holidays applied")
        return {}
    out = {}
    for year in sorted(years):
        for h in attendance_dao.fetch_holidays(calendar_id, year):
            iso = _iso_date(h.get("date"))
            if iso:
                out[iso] = {"name": h.get("name") or "Public holiday",
                            "is_floater": bool(h.get("isFloater"))}
    return out


def _leave_map(employee_id: str, from_date: str, to_date: str) -> dict:
    """
    date -> {'name', 'portion', 'half'} for APPROVED leave only.

    A Keka request spans fromDate..toDate with a session on each end:
      fromSession 0, toSession 1  a whole day (or whole span)
      fromSession 0, toSession 0  first half only
      fromSession 1, toSession 1  second half only
    Interior days of a multi-day span are always whole days; the sessions only
    qualify the first and last.
    """
    out = {}
    for req in leave_dao.fetch_leave_requests(employee_id, from_date, to_date):
        if req.get("status") != _LEAVE_APPROVED:
            continue

        start, end = _iso_date(req.get("fromDate")), _iso_date(req.get("toDate"))
        if not start:
            continue
        end = end or start

        sel = (req.get("selection") or [{}])[0]
        name = sel.get("leaveTypeName") or "Leave"

        f_sess = req.get("fromSession", _FIRST_HALF)
        t_sess = req.get("toSession", _SECOND_HALF)

        d = date.fromisoformat(start)
        last = date.fromisoformat(end)
        while d <= last:
            iso = d.isoformat()
            portion, half = 1.0, None
            if iso == start and f_sess == _SECOND_HALF:
                portion, half = 0.5, "Second half"
            if iso == end and t_sess == _FIRST_HALF:
                portion, half = 0.5, "First half"
            # A single-date request carrying both qualifiers is still one half day.
            out[iso] = {"name": name, "portion": portion, "half": half}
            d += timedelta(days=1)
    return out


def _entry_map(employee_id: str, from_date: str, to_date: str, project_names: dict) -> dict:
    """date -> [ {'project_id', 'project_name', 'hours', 'comment'} ] from logged time."""
    out: dict = {}
    for row in psa_dao.fetch_time_entries(employee_id, from_date, to_date):
        iso = _iso_date(row.get("date"))
        pid = row.get("projectId")
        if not iso or not pid:
            continue
        hours = _r1((row.get("totalMinutes") or 0) / 60.0)   # Keka reports minutes
        if hours <= 0:
            continue
        comment = (row.get("comments") or "").strip() or None

        bucket = out.setdefault(iso, {})
        if pid in bucket:
            # Keka allows several entries per project per day; the calendar shows one
            # row per project, so fold them and join the comments.
            bucket[pid]["hours"] = _r1(bucket[pid]["hours"] + hours)
            if comment:
                bucket[pid]["comment"] = " · ".join(
                    [c for c in (bucket[pid]["comment"], comment) if c]
                )
        else:
            bucket[pid] = {
                "project_id": pid,
                "project_name": project_names.get(pid) or "Project",
                "hours": hours,
                "comment": comment,
            }
    return {iso: list(b.values()) for iso, b in out.items()}


def _project_catalogue() -> dict:
    """project id -> {'name', 'code'} for the whole tenant."""
    out = {}
    for p in psa_dao.fetch_projects():
        pid = p.get("id") or p.get("identifier")
        if pid:
            out[pid] = {
                "name": p.get("name") or "Project",
                "code": p.get("code"),
                "is_billable": p.get("isBillable"),
            }
    return out


def _assignment(project_id: str, employee_id: str) -> dict | None:
    """
    This employee's allocation row on one project, mapped to contract shape.

    /psa/projects/{id}/allocations returns every allocation on the project, so it is
    filtered to the caller here. Returns None when the employee is not on the list
    (they may have logged time before being de-allocated).
    """
    try:
        rows = psa_dao.fetch_project_allocations(project_id)
    except KekaServiceError as exc:
        # Assignment detail is enrichment, not the point of the screen. A project
        # whose allocations cannot be read still shows its name and its hours.
        logger.warning("[timesheet] allocations unreadable for project %s: %s", project_id, exc)
        return None

    for row in rows:
        emp = row.get("employee") or {}
        if emp.get("id") != employee_id:
            continue
        role = (row.get("billingRole") or {}).get("name")
        return {
            "from_date": _iso_date(row.get("startDate")),
            "to_date": _iso_date(row.get("endDate")),
            "percentage": row.get("allocationPercentage"),
            "billing_role": role,
            "is_shadow": bool(row.get("isShadow")),
        }
    return None


def _overlaps_month(assignment: dict | None, month_start: date, month_end: date) -> bool:
    """Whether an allocation covers any part of the displayed month."""
    if not assignment:
        return True          # unknown dates: assume current rather than hide it
    start = assignment.get("from_date")
    end = assignment.get("to_date")
    if start and date.fromisoformat(start) > month_end:
        return False
    if end and date.fromisoformat(end) < month_start:
        return False
    return True


def _allocated_projects(employee_id: str, catalogue: dict,
                        month_start: date, month_end: date,
                        logged_project_ids: set) -> list:
    """
    The employee's projects for this month, each with its assignment detail and a
    stable colour slot.

    Colour slots are assigned from the employee's FULL allocation list, alphabetically,
    and only then filtered to the month. Assigning them after filtering would let a
    project dropping out of one month shift every other project's colour — the one
    thing `color_slot` exists to prevent.
    """
    ids, seen = [], set()
    for row in psa_dao.fetch_allocations(employee_id):
        pid = row.get("projectId")
        if pid and pid not in seen:
            seen.add(pid)
            ids.append(pid)
    # A project with hours this month belongs on screen even if the allocation ended.
    for pid in logged_project_ids:
        if pid not in seen:
            seen.add(pid)
            ids.append(pid)

    rows = [{
        "id": pid,
        "name": (catalogue.get(pid) or {}).get("name") or "Project",
        "code": (catalogue.get(pid) or {}).get("code"),
        "is_billable": (catalogue.get(pid) or {}).get("is_billable"),
        "assignment": _assignment(pid, employee_id),
    } for pid in ids]
    rows.sort(key=lambda r: ((r["name"] or "").lower(), r["id"] or ""))

    for i, r in enumerate(rows):
        r["color_slot"] = i + 1 if i < _MAX_COLOR_SLOTS else None

    keep = []
    for r in rows:
        if r["id"] in logged_project_ids or _overlaps_month(r["assignment"], month_start, month_end):
            a = r.pop("assignment")
            r["allocation"] = a
            r["has_hours_this_month"] = r["id"] in logged_project_ids
            keep.append(r)
        else:
            r.pop("assignment", None)
    return keep


# ── the month ─────────────────────────────────────────────────────────────────

def build_month(month: str, employee_email: str, employee_name: str = "") -> dict:
    """
    Build one contract-shaped month for an employee.

    `month` is 'YYYY-MM'; ValueError on anything else (the API maps that to 400).
    Raises EmployeeNotFoundError if the email has no Keka record, KekaServiceError
    (or KekaRateLimited) if an upstream read fails.
    """
    import re
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month or ""):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")

    year, mon = int(month[:4]), int(month[5:7])
    cap = config.daily_cap_hours()
    work_days = set(config.working_days())
    starts_on = config.week_starts_on()
    floaters_closed = config.floaters_are_closed()

    emp = _resolve_employee(employee_email)
    emp_id = emp.get("id")
    display = (employee_name or emp.get("displayName")
               or " ".join(x for x in [emp.get("firstName"), emp.get("lastName")] if x)
               or (employee_email or "").split("@")[0])

    # The 42-cell grid, so the UI does no calendar arithmetic.
    first_of_month = date(year, mon, 1)
    lead = (first_of_month.weekday() - _WD.index(starts_on)) % 7
    grid_start = first_of_month - timedelta(days=lead)
    grid_end = grid_start + timedelta(days=41)
    dim = _calendar.monthrange(year, mon)[1]

    # Read the grid's own span, not the calendar month, so spill-over days would
    # still be correct if we ever chose to report them. Well inside Keka's 90-day cap.
    span_from, span_to = grid_start.isoformat(), grid_end.isoformat()

    catalogue = _project_catalogue()
    holidays = _holiday_map(emp.get("holidayCalendarId"), {grid_start.year, grid_end.year})
    leave = _leave_map(emp_id, span_from, span_to)
    entries_by_date = _entry_map(emp_id, span_from, span_to, {k: v["name"] for k, v in catalogue.items()})

    # Projects are resolved after the entries so a project with hours this month is
    # kept even when its allocation has since ended.
    month_start = first_of_month
    month_end = date(year, mon, dim)
    logged_pids = {
        e["project_id"]
        for iso, rows in entries_by_date.items()
        if month_start.isoformat() <= iso <= month_end.isoformat()
        for e in rows
    }
    projects = _allocated_projects(emp_id, catalogue, month_start, month_end, logged_pids)
    slot_of = {p["id"]: p["color_slot"] for p in projects}

    today_iso = _today().isoformat()

    days = []
    per_project: dict = {}
    capacity_total = logged_total = 0.0
    working_days_count = days_expected = days_logged = days_missing = 0
    holiday_hours = leave_hours = 0.0

    for i in range(42):
        d = grid_start + timedelta(days=i)
        iso = d.isoformat()
        weekday = _WD[d.weekday()]
        in_month = (d.year, d.month) == (year, mon)
        is_working_weekday = weekday in work_days

        hol = holidays.get(iso)
        # A floater is optional leave, not a closure, so it does not blank the day
        # unless the org says otherwise.
        hol_closes = bool(hol) and (floaters_closed or not hol["is_floater"])
        lv = leave.get(iso)

        if hol_closes:
            day_type = "holiday"
        elif not is_working_weekday:
            day_type = "weekend"
        elif lv:
            day_type = "leave" if lv["portion"] >= 1.0 else "half_leave"
        else:
            day_type = "working"

        if day_type == "working":
            capacity = cap
        elif day_type == "half_leave":
            capacity = _r1(cap * (1.0 - lv["portion"]))
        else:
            capacity = 0.0

        raw_entries = entries_by_date.get(iso, [])
        entries = [{
            "project_id": e["project_id"],
            "project_name": e["project_name"],
            "color_slot": slot_of.get(e["project_id"]),
            "hours": e["hours"],
            "comment": e["comment"],
        } for e in raw_entries]
        logged = _r1(sum(e["hours"] for e in entries))

        annotation = None
        if hol:
            annotation = {
                "label": hol["name"],
                "detail": "Floater" if hol["is_floater"] else None,
                "kind": "holiday",
                "hours": cap if (hol_closes and is_working_weekday) else 0.0,
            }
        elif lv:
            annotation = {
                "label": lv["name"],
                "detail": lv["half"],
                "kind": "leave",
                "hours": _r1(cap * lv["portion"]),
            }

        if capacity <= 0:
            status = "not_applicable"
        elif logged >= capacity - 1e-9:
            status = "complete"
        elif logged > 0:
            status = "partial"
        else:
            status = "missing" if iso < today_iso else "empty"

        if not in_month:
            # Outside this month a day is inert; it belongs to another month's totals.
            capacity, entries, annotation, logged, status = 0.0, [], None, 0.0, "not_applicable"

        if in_month:
            if is_working_weekday:
                working_days_count += 1
                capacity_total += cap
            if capacity > 0:
                days_expected += 1
                if logged > 0:
                    days_logged += 1
                if status == "missing":
                    days_missing += 1
            logged_total += logged
            if annotation:
                logged_total += annotation["hours"]
                if annotation["kind"] == "holiday":
                    holiday_hours += annotation["hours"]
                else:
                    leave_hours += annotation["hours"]
            for e in entries:
                per_project[e["project_id"]] = _r1(per_project.get(e["project_id"], 0.0) + e["hours"])

        days.append({
            "date": iso,
            "weekday": weekday,
            "day_of_month": d.day,
            "in_month": in_month,
            "is_today": iso == today_iso,
            "day_type": day_type,
            "capacity_hours": capacity,
            "logged_hours": logged,
            "status": status,
            "entries": entries,
            "annotation": annotation,
            # Filled by timesheet_attendance.attach(): clock-ins come from the
            # attendance tracker, not from Keka.
            "attendance": None,
        })

    by_project = [{
        "project_id": pid,
        "name": (catalogue.get(pid) or {}).get("name") or "Project",
        "color_slot": slot_of.get(pid),
        "kind": "project",
        "hours": hours,
    } for pid, hours in sorted(per_project.items(), key=lambda kv: -kv[1])]

    if leave_hours:
        by_project.append({"project_id": None, "name": "Approved leave",
                           "color_slot": None, "kind": "leave", "hours": _r1(leave_hours)})
    if holiday_hours:
        by_project.append({"project_id": None, "name": "Public holiday",
                           "color_slot": None, "kind": "holiday", "hours": _r1(holiday_hours)})

    return {
        "month": f"{year:04d}-{mon:02d}",
        "label": f"{_MONTH_NAME[mon]} {year}",
        "today": today_iso,
        "employee": {"id": emp_id, "name": display},
        "policy": {
            "daily_cap_hours": cap,
            "working_days": config.working_days(),
            "week_starts_on": starts_on,
            "timezone": config.timezone_name(),
        },
        "totals": {
            "capacity_hours": _r1(capacity_total),
            "logged_hours": _r1(logged_total),
            "working_days": working_days_count,
            "days_expected": days_expected,
            "days_logged": days_logged,
            "days_missing": days_missing,
        },
        "projects": projects,
        "by_project": by_project,
        "days": days,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
