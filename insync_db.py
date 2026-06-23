"""
insync_db.py — read-only connector to the tracker RDS for the InSync home tab.

SOTs for this file:
  DB schema   → Caizin_Attendance_Tracker/shared/db_client.py + models.py
  Status enum → Caizin_Attendance_Tracker/shared/constants.py
  Bucket logic→ Caizin_Attendance_Tracker/shared/teams_client.py (_status_bucket, _RESPONSE_LABEL)
  Edit rules  → constants.py: CUTOFF_HOUR=15, MAX_RESPONSE_UPDATES=5, ALLOW_RESPONSE_UPDATES=false

Required env vars: TRACKER_DB_HOST, TRACKER_DB_NAME, TRACKER_DB_USER, TRACKER_DB_PASSWORD
Optional: TRACKER_DB_PORT (default 5432)
Optional: TRACKER_EDIT_CUTOFF_HOUR (default 15), TRACKER_MAX_EDITS (default 5)
Optional: TRACKER_ALLOW_EDITS (default false) — gates record_response()
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

_TRACKER_CFG = {
    "host":            os.getenv("TRACKER_DB_HOST", "att-postgres.cbc000k2gx7d.ap-south-1.rds.amazonaws.com"),
    "port":            int(os.getenv("TRACKER_DB_PORT", "5432")),
    "dbname":          os.getenv("TRACKER_DB_NAME", "attendance"),
    "user":            os.getenv("TRACKER_DB_USER", ""),
    "password":        os.getenv("TRACKER_DB_PASSWORD", ""),
    "connect_timeout": 5,
    "sslmode":         "prefer",
}

EDIT_CUTOFF_HOUR = int(os.getenv("TRACKER_EDIT_CUTOFF_HOUR", "15"))
MAX_EDITS        = int(os.getenv("TRACKER_MAX_EDITS", "5"))
ALLOW_EDITS      = os.getenv("TRACKER_ALLOW_EDITS", "false").lower() == "true"

_conn = None


def _get_conn():
    global _conn
    if _conn is not None and not _conn.closed:
        try:
            with _conn.cursor() as cur:
                cur.execute("SELECT 1")
            return _conn
        except Exception:
            pass
    _conn = psycopg2.connect(**_TRACKER_CFG)
    _conn.autocommit = True
    logger.info("[InSyncDB] new tracker DB connection established")
    return _conn


# ---------------------------------------------------------------------------
# Status bucketing — keep in sync with:
#   Caizin_Attendance_Tracker/shared/teams_client.py (_status_bucket, _RESPONSE_LABEL)
# ---------------------------------------------------------------------------

# Mirrors _RESPONSE_LABEL from tracker's teams_client.py
_RESPONSE_LABELS = {
    "office":        "Office",
    "wfh":           "WFH",
    "leave":         "Leave",
    "client_site":   "Client Location",   # tracker uses "Client Location", not "Client Site"
    "floater_leave": "Floater Holiday",
}


def _bucket(status: str, employee_response: Optional[str]) -> str:
    """Mirrors _status_bucket() from tracker's teams_client.py."""
    if status == "present":
        if employee_response == "wfh":         return "WFH"
        if employee_response == "client_site": return "Client Location"
        return "Office"
    if status == "pre_applied_wfh":
        return "WFH"
    if status in ("pre_approved_leave", "pre_approved_floater_leave"):
        return "Floater Holiday" if employee_response == "floater_leave" else "Leave"
    if status == "pre_applied_client_site":
        return "Client Location"
    if status == "absent":
        return "Absent"
    if employee_response:
        return _RESPONSE_LABELS.get(employee_response, employee_response)
    return "Pending"


# Maps DB status (with no employee_response / system-resolved) to UI sub-type + display bucket
_AUTO_MAP = {
    "present":                    ("auto_present",       "Office"),
    "pre_applied_wfh":            ("auto_wfh",           "WFH"),
    "pre_applied_client_site":    ("auto_client_site",   "Client Location"),
    "pre_approved_leave":         ("auto_leave",         "Leave"),
    "pre_approved_floater_leave": ("auto_floater_leave", "Floater Holiday"),
    "absent":                     ("auto_absent",        "Absent"),
}


def _to_ui_state(
    status: str,
    employee_response: Optional[str],
    response_time: Optional[str],
    response_update_count: int,
) -> dict:
    now_ist  = datetime.now(IST)
    can_edit = (
        ALLOW_EDITS
        and response_time is not None
        and now_ist.hour < EDIT_CUTOFF_HOUR
        and response_update_count < MAX_EDITS
    )

    # Employee responded via the Teams card
    if response_time:
        return {
            "state":    "responded",
            "bucket":   _bucket(status, employee_response),
            "sub":      None,
            "can_edit": can_edit,
        }

    # Card sent, waiting for response
    if status == "card_sent":
        return {"state": "pending", "bucket": "Pending", "sub": None, "can_edit": False}

    # System auto-resolved
    sub, bucket = _AUTO_MAP.get(status, (None, "Pending"))
    return {"state": "auto", "bucket": bucket, "sub": sub, "can_edit": False}


# ---------------------------------------------------------------------------
# Label → employee_response mapping for record_response()
# Includes both tracker button titles ("Home") and display labels ("WFH")
# ---------------------------------------------------------------------------
_LABEL_TO_RESPONSE = {
    "Office":          "office",
    "Home":            "wfh",           # tracker card button title
    "WFH":             "wfh",
    "Leave":           "leave",
    "Client Location": "client_site",   # tracker card button title + display label
    "Client Site":     "client_site",   # alias
    "Floater Leave":   "floater_leave", # tracker card button title
    "Floater Holiday": "floater_leave", # display label alias
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_today_status(email: str) -> dict:
    """
    Returns today's attendance state for the given email from the tracker RDS.

    Return shape:
        {
            state:             "no_record" | "pending" | "responded" | "auto" | "error"
            name:              str | None
            bucket:            str | None   — "Office" | "WFH" | "Client Location" |
                                              "Leave" | "Floater Holiday" | "Absent" | "Pending"
            sub:               str | None   — auto sub-type (auto_present | auto_wfh | ...)
            can_edit:          bool
            response_time:     str | None   — ISO timestamp
            employee_response: str | None
        }
    """
    today = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        conn = _get_conn()

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT employee_id, name FROM employees "
                "WHERE email = %s AND is_active = 'active'",
                (email.lower(),),
            )
            emp = cur.fetchone()

        if not emp:
            return {
                "state": "no_record", "name": None, "bucket": None,
                "sub": None, "can_edit": False,
                "response_time": None, "employee_response": None,
            }

        name        = emp["name"]
        employee_id = emp["employee_id"]

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT status, employee_response, response_time, response_update_count "
                "FROM attendance WHERE date = %s AND employee_id = %s",
                (today, employee_id),
            )
            rec = cur.fetchone()

        if not rec:
            return {
                "state": "no_record", "name": name, "bucket": None,
                "sub": None, "can_edit": False,
                "response_time": None, "employee_response": None,
            }

        status       = rec["status"]
        emp_resp     = rec["employee_response"]
        resp_time    = str(rec["response_time"]) if rec["response_time"] else None
        update_count = rec["response_update_count"] or 0

        ui = _to_ui_state(status, emp_resp, resp_time, update_count)
        return {
            "state":             ui["state"],
            "name":              name,
            "bucket":            ui["bucket"],
            "sub":               ui["sub"],
            "can_edit":          ui["can_edit"],
            "response_time":     resp_time,
            "employee_response": emp_resp,
        }

    except Exception as exc:
        logger.error("[InSyncDB] get_today_status(%s): %s", email, exc)
        return {
            "state": "error", "name": None, "bucket": None,
            "sub": None, "can_edit": False,
            "response_time": None, "employee_response": None,
        }


def get_today_all_records() -> list:
    """
    All active employees' attendance for today from the tracker RDS.
    Used by the Dashboard popup in the InSync tab.
    Returns: [{name, bucket, response_time}]
    """
    today = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        conn = _get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Use most recent date that has data, falling back to today if DB is current
            cur.execute("SELECT MAX(date) FROM attendance")
            latest = cur.fetchone()["max"]
            query_date = str(latest) if latest and str(latest) != today else today

            cur.execute(
                """
                SELECT e.name, a.status, a.employee_response, a.response_time
                  FROM attendance a
                  JOIN employees e ON a.employee_id = e.employee_id
                 WHERE a.date = %s AND e.is_active = 'active'
                 ORDER BY e.name
                """,
                (query_date,),
            )
            rows = cur.fetchall()

        result = []
        for row in rows:
            bkt = _bucket(row["status"], row["employee_response"])
            rt  = str(row["response_time"]) if row["response_time"] else None
            result.append({"name": row["name"], "bucket": bkt, "response_time": rt})
        return result

    except Exception as exc:
        logger.error("[InSyncDB] get_today_all_records: %s", exc)
        return []


def get_latest_attendance_date() -> str | None:
    """Returns the most recent date that has attendance records."""
    try:
        conn = _get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT MAX(date) AS date FROM attendance")
            row = cur.fetchone()
        return str(row["date"]) if row and row["date"] else None
    except Exception as exc:
        logger.error("[InSyncDB] get_latest_attendance_date: %s", exc)
        return None


def record_response(email: str, label: str) -> bool:
    """
    Write employee_response + response_time back to the tracker DB.
    Only executes when TRACKER_ALLOW_EDITS=true.
    Does NOT change the status column — that is the Lambda's responsibility.
    """
    if not ALLOW_EDITS:
        return False

    emp_response = _LABEL_TO_RESPONSE.get(label)
    if not emp_response:
        logger.warning("[InSyncDB] record_response: unknown label %r", label)
        return False

    today = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        conn = _get_conn()

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT employee_id FROM employees WHERE email = %s AND is_active = 'active'",
                (email.lower(),),
            )
            emp = cur.fetchone()

        if not emp:
            return False

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE attendance
                   SET employee_response     = %s,
                       response_time         = NOW(),
                       response_update_count = response_update_count + 1
                 WHERE date = %s AND employee_id = %s
                """,
                (emp_response, today, emp["employee_id"]),
            )
        logger.info("[InSyncDB] record_response(%s, %s) OK", email, label)
        return True

    except Exception as exc:
        logger.error("[InSyncDB] record_response(%s, %s): %s", email, label, exc)
        return False
