import logging
import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

_DSN = {
    "host":     os.getenv("DB_HOST"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME"),
    "user":     os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "connect_timeout": 10,
}


@contextmanager
def _conn():
    conn = psycopg2.connect(**_DSN)
    try:
        yield conn
    finally:
        conn.close()


def query_attendance_by_date(date: str) -> list[dict]:
    """Return all attendance rows for a given date (YYYY-MM-DD)."""
    sql = """
        SELECT employee_id, status, employee_response, check_in_time
        FROM   attendance
        WHERE  date = %s
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (date,))
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_all_active_employees() -> list[dict]:
    """Return all active employees with their names."""
    sql = """
        SELECT employee_id, name
        FROM   employees
        WHERE  is_active = TRUE
        ORDER  BY name
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    return [dict(r) for r in rows]
