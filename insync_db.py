"""
insync_db.py — read-only connector to the tracker RDS.

SOTs:
  DB schema   → Caizin_Attendance_Tracker/shared/db_client.py + models.py
  Status enum → Caizin_Attendance_Tracker/shared/constants.py
  Bucket logic→ Caizin_Attendance_Tracker/shared/teams_client.py (_status_bucket)

Required env vars: TRACKER_DB_HOST, TRACKER_DB_NAME, TRACKER_DB_USER, TRACKER_DB_PASSWORD
Optional: TRACKER_DB_PORT (default 5432)
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3
import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

_REDACT_BUCKET = os.getenv("REDACT_LIST_BUCKET", "att-reports-126697143036")
_REDACT_KEY    = os.getenv("REDACT_LIST_KEY", "config/report_ignore_list.json")
_REDACT_TTL    = 3600  # refresh every hour

_redact_names: set[str] = set()
_redact_loaded_at: float = 0.0


def _load_redact_list() -> None:
    """Fetch the redact list from S3 and cache it. Silently no-ops on error."""
    global _redact_names, _redact_loaded_at
    try:
        s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION", "ap-south-1"))
        obj = s3.get_object(Bucket=_REDACT_BUCKET, Key=_REDACT_KEY)
        data = json.loads(obj["Body"].read())
        names = data["employee_ids"] if isinstance(data, dict) else data
        _redact_names = {str(n).strip() for n in names}
        _redact_loaded_at = time.monotonic()
        logger.info("[InSyncDB] redact list loaded: %d names", len(_redact_names))
    except Exception as exc:
        logger.warning("[InSyncDB] could not load redact list from S3: %s", exc)


def _get_redact_names() -> set[str]:
    """Return cached redact set, refreshing from S3 if the TTL has expired."""
    if time.monotonic() - _redact_loaded_at > _REDACT_TTL:
        _load_redact_list()
    return _redact_names

_TRACKER_CFG = {
    "host":            os.getenv("TRACKER_DB_HOST", "att-postgres.cbc000k2gx7d.ap-south-1.rds.amazonaws.com"),
    "port":            int(os.getenv("TRACKER_DB_PORT", "5432")),
    "dbname":          os.getenv("TRACKER_DB_NAME", "attendance"),
    "user":            os.getenv("TRACKER_DB_USER", ""),
    "password":        os.getenv("TRACKER_DB_PASSWORD", ""),
    "connect_timeout": 5,
    "options":         "-c statement_timeout=5000",
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


def _get_records_for_date(date: str) -> list:
    """Fetch attendance rows for a specific date. Returns [{name, bucket, response_time}]."""
    conn = _get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT e.employee_id, e.name, a.status, a.employee_response, a.response_time
              FROM attendance a
              JOIN employees e ON a.employee_id = e.employee_id
             WHERE a.date = %s AND e.is_active = 'active'
             ORDER BY e.name
            """,
            (date,),
        )
        rows = cur.fetchall()
    redacted = _get_redact_names()
    result = []
    for row in rows:
        if str(row["employee_id"]) in redacted:
            continue
        bkt = _bucket(row["status"], row["employee_response"])
        rt  = str(row["response_time"]) if row["response_time"] else None
        result.append({"name": row["name"], "bucket": bkt, "response_time": rt})
    return result


def get_today_all_records() -> list:
    """
    All active employees' attendance for today from the tracker RDS.
    Returns: [{name, bucket, response_time}]
    """
    today = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        return _get_records_for_date(today)
    except Exception as exc:
        logger.error("[InSyncDB] get_today_all_records: %s", exc)
        return []


def get_latest_records() -> tuple[list, str]:
    """
    Returns attendance records for today. Falls back to empty list if no data.
    Returns: (records, date_str)
    """
    today = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        rows = _get_records_for_date(today)
        return rows, today
    except Exception as exc:
        logger.error("[InSyncDB] get_latest_records: %s", exc)
        return [], today


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


