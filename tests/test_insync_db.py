"""
Tests for insync_db.py — get_work_status_by_email, get_work_status_by_name

The two new functions added for the work-location-query feature.
These tests will FAIL until those functions are added to insync_db.py.

Run:  cd Caizin-HR-Bot && pytest tests/test_insync_db.py -v

psycopg2, boto3, and AWS S3 are all mocked — no real DB or network calls.
"""

import os
import sys
import types
from unittest.mock import MagicMock, patch
import pytest

# ---------------------------------------------------------------------------
# Stub packages before insync_db is imported
# ---------------------------------------------------------------------------
os.environ.setdefault("TRACKER_DB_HOST",     "fake-host")
os.environ.setdefault("TRACKER_DB_NAME",     "fake-db")
os.environ.setdefault("TRACKER_DB_USER",     "fake-user")
os.environ.setdefault("TRACKER_DB_PASSWORD", "fake-pass")

for _pkg in ["psycopg2", "psycopg2.extras", "boto3", "dotenv"]:
    sys.modules.setdefault(_pkg, types.ModuleType(_pkg))

# psycopg2 needs RealDictCursor and a connect function
sys.modules["psycopg2"].connect = MagicMock()
sys.modules["psycopg2.extras"].RealDictCursor = MagicMock
sys.modules["boto3"].client = MagicMock(return_value=MagicMock())
sys.modules["dotenv"].load_dotenv = lambda: None

sys.modules.pop("insync_db", None)
import insync_db  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_cursor(rows):
    """Return a fake psycopg2 cursor whose fetchone/fetchall return `rows`."""
    cursor = MagicMock()
    cursor.__enter__ = lambda s: cursor
    cursor.__exit__ = MagicMock(return_value=False)
    if isinstance(rows, list):
        cursor.fetchall.return_value = rows
    else:
        cursor.fetchone.return_value = rows
    return cursor


def _make_conn(cursor):
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value = cursor
    return conn


def _row(employee_id="1", name="Priya Sharma", status="present", employee_response="wfh"):
    return {"employee_id": employee_id, "name": name,
            "status": status, "employee_response": employee_response}


# ===========================================================================
# get_work_status_by_email
# ===========================================================================

class TestGetWorkStatusByEmail:

    def test_returns_name_and_bucket_for_matching_row(self):
        row = _row(name="Priya Sharma", status="present", employee_response="wfh")
        cursor = _make_cursor(row)
        conn = _make_conn(cursor)
        insync_db._conn = conn
        insync_db._redact_names = set()
        insync_db._redact_loaded_at = float("inf")  # skip S3 refresh

        result = insync_db.get_work_status_by_email("priya@caizin.com")

        assert result is not None
        assert result["name"] == "Priya Sharma"
        assert result["bucket"] == "WFH"

    def test_returns_none_when_no_row_found(self):
        cursor = _make_cursor(None)
        conn = _make_conn(cursor)
        insync_db._conn = conn
        insync_db._redact_names = set()
        insync_db._redact_loaded_at = float("inf")

        result = insync_db.get_work_status_by_email("unknown@caizin.com")

        assert result is None

    def test_returns_none_for_redacted_employee(self):
        row = _row(employee_id="42", name="Hidden Person")
        cursor = _make_cursor(row)
        conn = _make_conn(cursor)
        insync_db._conn = conn
        insync_db._redact_names = {"42"}
        insync_db._redact_loaded_at = float("inf")

        result = insync_db.get_work_status_by_email("hidden@caizin.com")

        assert result is None

    def test_db_exception_returns_none(self):
        insync_db._conn = None
        with patch("insync_db._get_conn", side_effect=Exception("DB down")):
            result = insync_db.get_work_status_by_email("anyone@caizin.com")

        assert result is None

    @pytest.mark.parametrize("status,response,expected_bucket", [
        ("present",               "office",        "Office"),
        ("present",               "wfh",           "WFH"),
        ("present",               "client_site",   "Client Location"),
        ("present",               "leave",         "Leave"),
        ("present",               "floater_leave", "Floater Holiday"),
        ("pre_applied_wfh",       None,            "WFH"),
        ("pre_approved_leave",    None,            "Leave"),
        ("absent",                None,            "Absent"),
    ])
    def test_bucket_mapping(self, status, response, expected_bucket):
        row = _row(status=status, employee_response=response)
        cursor = _make_cursor(row)
        conn = _make_conn(cursor)
        insync_db._conn = conn
        insync_db._redact_names = set()
        insync_db._redact_loaded_at = float("inf")

        result = insync_db.get_work_status_by_email("any@caizin.com")

        assert result["bucket"] == expected_bucket


# ===========================================================================
# get_work_status_by_name
# ===========================================================================

class TestGetWorkStatusByName:

    def test_returns_list_of_matches(self):
        rows = [
            _row(employee_id="1", name="Priya Sharma", status="present", employee_response="office"),
            _row(employee_id="2", name="Priya Patel",  status="present", employee_response="wfh"),
        ]
        cursor = _make_cursor(rows)
        conn = _make_conn(cursor)
        insync_db._conn = conn
        insync_db._redact_names = set()
        insync_db._redact_loaded_at = float("inf")

        result = insync_db.get_work_status_by_name("priya")

        assert len(result) == 2
        names = [r["name"] for r in result]
        assert "Priya Sharma" in names
        assert "Priya Patel" in names

    def test_returns_empty_list_when_no_match(self):
        cursor = _make_cursor([])
        conn = _make_conn(cursor)
        insync_db._conn = conn
        insync_db._redact_names = set()
        insync_db._redact_loaded_at = float("inf")

        result = insync_db.get_work_status_by_name("zzznobody")

        assert result == []

    def test_excludes_redacted_employees(self):
        rows = [
            _row(employee_id="1", name="Visible Person"),
            _row(employee_id="99", name="Hidden Person"),
        ]
        cursor = _make_cursor(rows)
        conn = _make_conn(cursor)
        insync_db._conn = conn
        insync_db._redact_names = {"99"}
        insync_db._redact_loaded_at = float("inf")

        result = insync_db.get_work_status_by_name("person")

        assert len(result) == 1
        assert result[0]["name"] == "Visible Person"

    def test_db_exception_returns_empty_list(self):
        insync_db._conn = None
        with patch("insync_db._get_conn", side_effect=Exception("DB down")):
            result = insync_db.get_work_status_by_name("anyone")

        assert result == []

    def test_single_match_returns_list_of_one(self):
        rows = [_row(name="Rohan Lande", status="present", employee_response="office")]
        cursor = _make_cursor(rows)
        conn = _make_conn(cursor)
        insync_db._conn = conn
        insync_db._redact_names = set()
        insync_db._redact_loaded_at = float("inf")

        result = insync_db.get_work_status_by_name("rohan")

        assert len(result) == 1
        assert result[0]["name"] == "Rohan Lande"
        assert result[0]["bucket"] == "Office"