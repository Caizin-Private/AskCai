"""
shared/teams_client.py
Teams Adaptive Card builders and Teams Bot client helpers.
"""

from datetime import datetime, timezone, timedelta
from typing import List

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Status display helpers (reused across card builders)
# ---------------------------------------------------------------------------

_RESPONSE_LABEL = {
    "office":         "Office",
    "wfh":            "WFH",
    "leave":          "Leave",
    "floater_leave":  "Floater Leave",
    "client_site":    "Client Site",
}

_RESPONSE_COLOR = {
    "Office":        "Good",
    "WFH":           "Accent",
    "Leave":         "Warning",
    "Floater Leave": "Warning",
    "Client Site":   "Good",
    "Absent":        "Attention",
    "Pending":       "Default",
}

_STATUS_ORDER = ["Office", "WFH", "Client Site", "Leave", "Floater Leave", "Absent", "Pending"]


def _status_bucket(db_status: str, employee_response: str | None) -> str:
    """Map DB status + employee_response → human-readable bucket label."""
    if db_status == "present":
        if employee_response == "wfh":
            return "WFH"
        if employee_response == "client_site":
            return "Client Site"
        return "Office"
    if db_status == "pre_applied_wfh":
        return "WFH" if employee_response == "wfh" else "Office"
    if db_status in ("pre_approved_leave", "pre_approved_floater_leave"):
        return "Floater Leave" if employee_response == "floater_leave" else "Leave"
    if db_status == "pre_applied_client_site":
        return "Client Site"
    if db_status == "absent":
        return "Absent"
    if db_status == "card_sent" and not employee_response:
        return "Pending"
    # fallback: use response label if available
    if employee_response:
        return _RESPONSE_LABEL.get(employee_response, employee_response.replace("_", " ").title())
    return "Pending"


def _utc_to_ist_time(utc_iso: str | None) -> str:
    """Convert UTC ISO string → IST HH:MM, or '—' if missing."""
    if not utc_iso:
        return "—"
    try:
        dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
        ist_dt = dt.astimezone(IST)
        return ist_dt.strftime("%H:%M")
    except Exception:
        return "—"


# ---------------------------------------------------------------------------
# Dashboard card
# ---------------------------------------------------------------------------

def build_dashboard_card(records: list, employees: list, date: str) -> dict:
    """
    Build Adaptive Card JSON for the attendance dashboard popup.

    Args:
        records:   List of AttendanceRecord (or dicts with same fields)
        employees: List of Employee (or dicts with same fields)
        date:      "YYYY-MM-DD"
    """
    # Helper accessors that work for both objects and dicts
    def _get(obj, *keys):
        for k in keys:
            if isinstance(obj, dict):
                obj = obj.get(k)
            else:
                obj = getattr(obj, k, None)
            if obj is None:
                return None
        return obj

    # Step 1 — name lookup
    emp_map = {}
    for emp in employees:
        eid = _get(emp, "employee_id")
        name = _get(emp, "name") or "Unknown"
        if eid:
            emp_map[eid] = name

    # Step 2 & 3 — build rows with status bucket + check-in
    rows = []
    for rec in records:
        emp_id   = _get(rec, "employee_id")
        db_status = _get(rec, "status") or ""
        emp_resp  = _get(rec, "employee_response")
        check_in  = _get(rec, "check_in_time")

        bucket    = _status_bucket(db_status, emp_resp)
        rows.append({
            "name":     emp_map.get(emp_id, "Unknown"),
            "status":   bucket,
            "check_in": _utc_to_ist_time(check_in),
        })

    # Step 4 — sort rows
    order = {s: i for i, s in enumerate(_STATUS_ORDER)}
    rows.sort(key=lambda r: order.get(r["status"], 99))

    # Counts per bucket
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    # Format date for display: "2026-06-18" → "18 Jun 2026"
    try:
        display_date = datetime.strptime(date, "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        display_date = date

    # ---------------------------------------------------------------------------
    # Build Adaptive Card
    # ---------------------------------------------------------------------------
    body = []

    # Title
    body.append({
        "type": "TextBlock",
        "text": f"Attendance — {display_date}",
        "weight": "Bolder",
        "size": "Medium",
    })

    # Summary counts row
    summary_columns = [
        ("✅ Office",   counts.get("Office", 0),        "Good"),
        ("🏠 WFH",      counts.get("WFH", 0),           "Accent"),
        ("🏥 Leave",    counts.get("Leave", 0) + counts.get("Floater Leave", 0), "Warning"),
        ("💻 Client",   counts.get("Client Site", 0),   "Good"),
        ("❌ Absent",   counts.get("Absent", 0),         "Attention"),
        ("⏳ Pending",  counts.get("Pending", 0),        "Default"),
    ]
    body.append({
        "type": "ColumnSet",
        "columns": [
            {
                "type": "Column",
                "width": "stretch",
                "items": [
                    {"type": "TextBlock", "text": label,      "isSubtle": True},
                    {"type": "TextBlock", "text": str(count), "color": color, "weight": "Bolder"},
                ],
            }
            for label, count, color in summary_columns
        ],
    })

    # Separator
    body.append({"type": "Separator"})

    # Header row
    body.append({
        "type": "ColumnSet",
        "columns": [
            {"type": "Column", "width": 2, "items": [{"type": "TextBlock", "text": "Name",     "weight": "Bolder", "isSubtle": True}]},
            {"type": "Column", "width": 2, "items": [{"type": "TextBlock", "text": "Status",   "weight": "Bolder", "isSubtle": True}]},
            {"type": "Column", "width": 1, "items": [{"type": "TextBlock", "text": "Check-in", "weight": "Bolder", "isSubtle": True}]},
        ],
    })

    # Data rows
    for row in rows:
        color = _RESPONSE_COLOR.get(row["status"], "Default")
        body.append({
            "type": "ColumnSet",
            "columns": [
                {"type": "Column", "width": 2, "items": [{"type": "TextBlock", "text": row["name"],     "wrap": True}]},
                {"type": "Column", "width": 2, "items": [{"type": "TextBlock", "text": row["status"],   "color": color}]},
                {"type": "Column", "width": 1, "items": [{"type": "TextBlock", "text": row["check_in"]}]},
            ],
        })

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }
