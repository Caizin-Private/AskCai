"""
insync_db.py — read-only connector to the tracker RDS.

SOTs:
  DB schema   → Caizin_Attendance_Tracker/shared/db_client.py + models.py
  Status enum → Caizin_Attendance_Tracker/shared/constants.py
  Bucket logic→ Caizin_Attendance_Tracker/shared/teams_client.py (_status_bucket)

Required env vars: TRACKER_DB_HOST, TRACKER_DB_NAME, TRACKER_DB_USER, TRACKER_DB_PASSWORD
Optional: TRACKER_DB_PORT (default 5432)
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


# Mirrors _RESPONSE_LABEL from tracker's teams_client.py
_RESPONSE_LABELS = {
    "office":        "Office",
    "wfh":           "WFH",
    "leave":         "Leave",
    "client_site":   "Client Location",
    "floater_leave": "Floater Holiday",
}


def _bucket(status: str, employee_response: Optional[str]) -> str:
    """Mirrors _status_bucket() from tracker's teams_client.py."""
    if status == "present":
        if employee_response == "wfh":           return "WFH"
        if employee_response == "client_site":   return "Client Location"
        if employee_response == "leave":         return "Leave"
        if employee_response == "floater_leave": return "Floater Holiday"
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


def get_today_all_records() -> list:
    """
    All active employees' attendance for today from the tracker RDS.
    Returns: [{name, bucket, response_time}]
    """
    today = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        conn = _get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT e.name, a.status, a.employee_response, a.response_time
                  FROM attendance a
                  JOIN employees e ON a.employee_id = e.employee_id
                 WHERE a.date = %s AND e.is_active = 'active'
                 ORDER BY e.name
                """,
                (today,),
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


def get_employee_by_email(email: str) -> dict | None:
    """Look up active employee by email. Returns {employee_id, name} or None."""
    try:
        conn = _get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT employee_id, name FROM employees WHERE lower(email) = %s AND is_active = 'active'",
                (email.lower(),),
            )
            row = cur.fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.error("[InSyncDB] get_employee_by_email: %s", exc)
        return None


def record_attendance_response(employee_id: str, status_verb: str) -> bool:
    """
    Write the employee's attendance response to the tracker DB.
    status_verb: office | wfh | leave | client_site | floater_leave
    Mirrors the tracker Lambda's DB write (status stays card_sent; employee_response is set).
    """
    if status_verb not in {"office", "wfh", "leave", "client_site", "floater_leave"}:
        logger.warning("[InSyncDB] record_attendance_response: unknown verb %s", status_verb)
        return False

    today   = datetime.now(IST).strftime("%Y-%m-%d")
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE attendance
                   SET employee_response     = %s,
                       response_time         = %s,
                       response_update_count = COALESCE(response_update_count, 0) + 1
                 WHERE date = %s AND employee_id = %s
                """,
                (status_verb, now_utc, today, employee_id),
            )
            updated = cur.rowcount
        if updated == 0:
            logger.warning("[InSyncDB] record_attendance_response: no row for emp=%s date=%s", employee_id, today)
            return False
        logger.info("[InSyncDB] attendance recorded: emp=%s status=%s", employee_id, status_verb)
        return True
    except Exception as exc:
        logger.error("[InSyncDB] record_attendance_response: %s", exc)
        return False


def get_dashboard_data() -> dict:
    """
    Returns attendance data shaped for build_dashboard_card().
    {date, records: [{employee_id, status, employee_response, check_in_time}],
           employees: [{employee_id, name}]}
    """
    today = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        conn = _get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT a.employee_id, a.status, a.employee_response, a.check_in_time,
                       e.name
                  FROM attendance a
                  JOIN employees e ON a.employee_id = e.employee_id
                 WHERE a.date = %s AND e.is_active = 'active'
                 ORDER BY e.name
                """,
                (today,),
            )
            rows = cur.fetchall()
        records   = [dict(r) for r in rows]
        employees = [{"employee_id": r["employee_id"], "name": r["name"]} for r in records]
        return {"date": today, "records": records, "employees": employees}
    except Exception as exc:
        logger.error("[InSyncDB] get_dashboard_data: %s", exc)
        return {"date": today, "records": [], "employees": []}
