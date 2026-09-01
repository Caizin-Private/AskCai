"""
timesheet_mock.py — stand-in for the timesheet read API.

Serves the contract in artifacts/timesheet-ui-contract.yaml with synthetic data, so
static/timesheet-dashboard can be built and reviewed before Keka PSA is wired up.

`attendance` is synthesised here too, but its real source is the attendance tracker
rather than Keka — see timesheet_attendance.py.

Replace with a real timesheet_service.py that reads Keka:
  projects + logged time  -> GET /psa/project/resources, GET /psa/timeentries
  holidays                -> GET /time/holidayscalendar/{id}/holidays
  approved leave          -> GET /time/leaverequests
The response shape must not change — the UI is written against the contract, not
against this module.

Everything here is deterministic: the same employee and month always produce the same
timesheet, so a refresh never reshuffles the calendar.
"""

import calendar
import hashlib
import logging
import re
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

DAILY_CAP_HOURS = 8.0
WORKING_DAYS = ["mon", "tue", "wed", "thu", "fri"]
WEEK_STARTS_ON = "mon"
TIMEZONE = "Asia/Kolkata"

_WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Matches the contract's `month` pattern exactly — zero-padded, months 01-12.
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# The pool a mock employee can be assigned to. Who gets which is decided per
# employee in _projects_for(), the way Keka assigns real allocations.
_PROJECT_POOL = [
    {"id": "9f2c1e64-0f1a-4a7e-9a1b-2c3d4e5f6a7b", "name": "Cyber",     "code": "CYB", "is_billable": True},
    {"id": "1d7b8c92-3e4f-4a5b-8c6d-7e8f9a0b1c2d", "name": "Conduct",   "code": "CND", "is_billable": True},
    {"id": "4b8d3a11-77cc-42e9-b0d5-9e8f7a6b5c4d", "name": "Datasacan", "code": "DSC", "is_billable": True},
    {"id": "7c1a5b39-88de-4f01-a2b7-3d4e5f60718a", "name": "Internal",  "code": "INT", "is_billable": False},
]
_ROLES = ["Senior Engineer", "Engineer", "Tech Lead", "Analyst"]


def _projects_for(seed: str) -> list:
    """
    Which projects this employee is assigned to, with assignment detail.

    Two or three of the pool, chosen deterministically per employee — so two people
    genuinely see different projects, which is the point of the screen.

    Colour slots are assigned alphabetically over the employee's full assignment list,
    matching keka/timesheet_service.py, so a slot never moves between months.
    """
    n = 2 + _roll(seed, "projcount", 2)          # 2 or 3
    start = _roll(seed, "projstart", len(_PROJECT_POOL))
    picked = [_PROJECT_POOL[(start + i) % len(_PROJECT_POOL)] for i in range(n)]
    picked = sorted(picked, key=lambda p: p["name"].lower())

    out = []
    for i, p in enumerate(picked):
        pct = [100, 50, 60, 40, 25][_roll(seed, "pct" + p["id"], 5)]
        out.append({
            "id": p["id"],
            "name": p["name"],
            "code": p["code"],
            "is_billable": p["is_billable"],
            "color_slot": i + 1 if i < 3 else None,
            "allocation": {
                "from_date": "2026-04-01",
                "to_date": None if _roll(seed, "open" + p["id"], 3) else "2026-12-31",
                "percentage": pct,
                "billing_role": _ROLES[_roll(seed, "role" + p["id"], len(_ROLES))],
                "is_shadow": bool(_roll(seed, "shadow" + p["id"], 8) == 0),
            },
        })
    return out

# Stands in for the Keka holiday calendar.
HOLIDAYS = {
    "2026-01-26": "Republic Day",
    "2026-03-04": "Holi",
    "2026-04-03": "Good Friday",
    "2026-05-01": "Maharashtra Day",
    "2026-08-15": "Independence Day",
    "2026-08-28": "Raksha Bandhan",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Gandhi Jayanti",
    "2026-11-08": "Diwali",
    "2026-12-25": "Christmas",
}

# Stands in for approved Keka leave requests. portion 1.0 = full day, 0.5 = half day.
LEAVE = {
    "2026-07-06": {"name": "Casual Leave", "portion": 1.0, "half": None},
    "2026-08-12": {"name": "Sick Leave",   "portion": 0.5, "half": "First half"},
    "2026-08-17": {"name": "Casual Leave", "portion": 1.0, "half": None},
    "2026-08-18": {"name": "Casual Leave", "portion": 1.0, "half": None},
    "2026-09-21": {"name": "Earned Leave", "portion": 1.0, "half": None},
    "2026-09-22": {"name": "Earned Leave", "portion": 1.0, "half": None},
    "2026-09-23": {"name": "Earned Leave", "portion": 1.0, "half": None},
}

_COMMENTS = {
    "Cyber": [
        "Threat model review for the ingress gateway",
        "Pen-test findings triage",
        "SIEM rule tuning — false positive sweep",
        "Access review",
        "Vulnerability scan baseline",
        "Segmentation testing",
        "",
    ],
    "Conduct": [
        "Control evidence collection",
        "Quarterly attestation draft",
        "Risk register clean-up",
        "Control gap analysis",
        "Audit prep call",
        "",
    ],
    "Datasacan": [
        "PII discovery run on the staging warehouse",
        "Column classifier tuning",
        "Scanner throughput profiling",
        "Retention policy mapping",
        "",
    ],
    "Internal": [
        "Sprint planning",
        "Interview panel",
        "Onboarding docs",
        "",
    ],
}


def _today() -> date:
    return datetime.now(IST).date()


def _roll(seed: str, salt: str, mod: int) -> int:
    """Stable pseudo-random in [0, mod). Same inputs always give the same answer."""
    h = hashlib.sha256(f"{seed}|{salt}".encode()).hexdigest()
    return int(h[:8], 16) % mod


def _r1(n: float) -> float:
    return round(n + 1e-9, 1)


def _half(n: float) -> float:
    """Snap to the 0.5 increment the policy requires."""
    return round(round(n * 2) / 2, 1)


def _day_type(iso: str, weekday: str) -> str:
    if iso in HOLIDAYS:
        return "holiday"
    if weekday not in WORKING_DAYS:
        return "weekend"
    lv = LEAVE.get(iso)
    if lv:
        return "leave" if lv["portion"] >= 1.0 else "half_leave"
    return "working"


def _capacity(iso: str, day_type: str) -> float:
    if day_type == "working":
        return DAILY_CAP_HOURS
    if day_type == "half_leave":
        return _r1(DAILY_CAP_HOURS * (1.0 - LEAVE[iso]["portion"]))
    return 0.0


def _annotation(iso: str, day_type: str, weekday: str) -> dict | None:
    if day_type == "holiday":
        return {
            "label": HOLIDAYS[iso],
            "detail": None,
            "kind": "holiday",
            # A holiday landing on a weekend is labelled but accounts for nothing.
            "hours": 0.0 if weekday not in WORKING_DAYS else DAILY_CAP_HOURS,
        }
    if day_type in ("leave", "half_leave"):
        lv = LEAVE[iso]
        return {
            "label": lv["name"],
            "detail": lv["half"],
            "kind": "leave",
            "hours": _r1(DAILY_CAP_HOURS * lv["portion"]),
        }
    return None


def _entries(seed: str, iso: str, capacity: float, is_past: bool, is_today: bool,
             projects: list) -> list:
    """Synthesise a plausible day. Deterministic per (employee, date)."""
    if capacity <= 0 or (not is_past and not is_today):
        return []
    if not projects:
        return []

    # Thresholds are tuned so a typical month exercises every UI state — complete,
    # partial and missing all appear. Real distributions will be kinder than this.
    shape = _roll(seed, iso + ":shape", 100)
    if is_today:
        target = _half(capacity * 0.45)        # today is mid-flight
    elif shape < 16:
        return []                              # left blank -> renders as "hours missing"
    elif shape < 32:
        target = _half(capacity - (0.5 + _roll(seed, iso + ":gap", 5) * 0.5))
    else:
        target = capacity

    if target <= 0:
        return []

    split = 1 if _roll(seed, iso + ":split", 100) < 45 else 2
    picks, out = [], []
    for i in range(split):
        p = projects[_roll(seed, f"{iso}:proj{i}", len(projects))]
        if p["id"] not in picks:
            picks.append(p["id"])
            out.append(p)

    if len(out) == 1:
        hours = [target]
    else:
        first = _half(max(0.5, min(target - 0.5, (target / 2) + (_roll(seed, iso + ":skew", 5) - 2) * 0.5)))
        hours = [first, _half(target - first)]

    entries = []
    for p, h in zip(out, hours):
        if h <= 0:
            continue
        pool = _COMMENTS.get(p["name"], [""])
        comment = pool[_roll(seed, f'{iso}:c{p["name"]}', len(pool))]
        entries.append({
            "project_id": p["id"],
            "project_name": p["name"],
            "color_slot": p["color_slot"],
            "hours": h,
            "comment": comment or None,
        })
    return entries


# Where a mock employee worked from. Weighted toward the office, the way the real
# tracker data is.
_ATT_STATUSES = ["office", "office", "wfh", "office", "client", "wfh"]


def _attendance(seed: str, iso: str, day_type: str, today_iso: str) -> dict | None:
    """
    Stand-in for the tracker's attendance row, deterministic per (employee, date).

    Real clock-ins come from timesheet_attendance.py; this only has to produce the same
    shape so the calendar's mark can be reviewed with no database.
    """
    if day_type in ("weekend", "holiday"):
        return None
    if day_type == "leave":
        return {"status": "leave", "check_in_time": None, "detail": None}
    if iso > today_iso:
        return None                          # nobody has clocked in for a future day
    if iso == today_iso:
        return {"status": "pending", "check_in_time": None, "detail": None}
    if _roll(seed, iso + ":absent", 22) == 0:
        return {"status": "absent", "check_in_time": None, "detail": None}
    at = 9 * 60 + 5 + _roll(seed, iso + ":checkin", 70)      # 09:05 – 10:14
    return {
        "status": _ATT_STATUSES[_roll(seed, iso + ":att", len(_ATT_STATUSES))],
        "check_in_time": f"{at // 60:02d}:{at % 60:02d}",
        "detail": None,
    }


def _status(capacity: float, logged: float, iso: str, today_iso: str) -> str:
    if capacity <= 0:
        return "not_applicable"
    if logged >= capacity - 1e-9:
        return "complete"
    if logged > 0:
        return "partial"
    return "missing" if iso < today_iso else "empty"


def build_month(month: str, employee_email: str = "", employee_name: str = "") -> dict:
    """
    Build one contract-shaped month payload.

    `month` is 'YYYY-MM'. Raises ValueError on anything else — the caller maps that
    to 400 invalid_month.
    """
    if not _MONTH_RE.match(month or ""):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    year, mon = int(month[:4]), int(month[5:7])
    first_of_month = date(year, mon, 1)

    seed = (employee_email or "anonymous").strip().lower()
    projects = _projects_for(seed)
    by_id = {p["id"]: p for p in projects}
    today = _today()
    today_iso = today.isoformat()

    # 42 cells: six weeks, starting on the Monday of the week containing the 1st.
    lead = (first_of_month.weekday() - _WD.index(WEEK_STARTS_ON)) % 7
    grid_start = first_of_month - timedelta(days=lead)
    dim = calendar.monthrange(year, mon)[1]

    days = []
    per_project: dict[str, float] = {}
    capacity_total = logged_total = 0.0
    working_days = days_expected = days_logged = days_missing = 0
    holiday_hours = leave_hours = 0.0

    for i in range(42):
        d = grid_start + timedelta(days=i)
        iso = d.isoformat()
        weekday = _WD[d.weekday()]
        in_month = (d.year, d.month) == (year, mon)

        day_type = _day_type(iso, weekday)
        capacity = _capacity(iso, day_type)
        annotation = _annotation(iso, day_type, weekday)
        entries = _entries(seed, iso, capacity, iso < today_iso, iso == today_iso, projects)
        logged = _half(sum(e["hours"] for e in entries))
        attendance = _attendance(seed, iso, day_type, today_iso)

        if in_month:
            status = _status(capacity, logged, iso, today_iso)
        else:
            # A day outside this month is inert: it belongs to another month's totals.
            capacity, entries, annotation, logged, status = 0.0, [], None, 0.0, "not_applicable"
            attendance = None

        if in_month:
            if weekday in WORKING_DAYS:
                working_days += 1
                capacity_total += DAILY_CAP_HOURS
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
                per_project[e["project_id"]] = _half(per_project.get(e["project_id"], 0.0) + e["hours"])

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
            "attendance": attendance,
        })

    by_project = [
        {
            "project_id": pid,
            "name": by_id[pid]["name"],
            "color_slot": by_id[pid]["color_slot"],
            "kind": "project",
            "hours": hours,
        }
        for pid, hours in sorted(per_project.items(), key=lambda kv: -kv[1])
    ]
    if leave_hours:
        by_project.append({"project_id": None, "name": "Approved leave",
                           "color_slot": None, "kind": "leave", "hours": _half(leave_hours)})
    if holiday_hours:
        by_project.append({"project_id": None, "name": "Public holiday",
                           "color_slot": None, "kind": "holiday", "hours": _half(holiday_hours)})

    return {
        "month": f"{year:04d}-{mon:02d}",
        "label": f"{calendar.month_name[mon]} {year}",
        "today": today_iso,
        "employee": {
            "id": hashlib.sha256(seed.encode()).hexdigest()[:32],
            "name": employee_name or (seed.split("@")[0].replace(".", " ").title() if "@" in seed else "Employee"),
        },
        "policy": {
            "daily_cap_hours": DAILY_CAP_HOURS,
            "working_days": WORKING_DAYS,
            "week_starts_on": WEEK_STARTS_ON,
            "timezone": TIMEZONE,
        },
        "totals": {
            "capacity_hours": _half(capacity_total),
            "logged_hours": _half(logged_total),
            "working_days": working_days,
            "days_expected": days_expected,
            "days_logged": days_logged,
            "days_missing": days_missing,
        },
        "projects": [dict(p, has_hours_this_month=p["id"] in per_project) for p in projects],
        "by_project": by_project,
        "days": days,
        "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
    }
