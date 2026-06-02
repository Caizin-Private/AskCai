"""
tests/test_leave.py
Unit tests for keka/leave.py — all Keka API calls are mocked.
No real HTTP requests are made; no Keka data is touched.

Patching strategy:
  keka.leave.get_employee_id   — employee email → UUID lookup
  keka.leave.keka_get          — all GET calls inside handlers
  keka.leave.keka_post         — all POST calls inside handlers
  keka.leave.keka_delete       — all DELETE calls inside handlers
  keka.leave.validate_leave_dates  — date validation (tested separately)
  keka.leave.count_working_days    — day counter (tested separately)
"""

import pytest
from unittest.mock import patch, MagicMock, call

from keka.leave import (
    handle_get_leave_balance,
    handle_apply_leave,
    handle_get_leave_requests,
    handle_cancel_leave,
)
from tests.conftest import (
    MOCK_EMPLOYEE_ID,
    MOCK_EMPLOYEE_EMAIL,
    MOCK_LEAVE_TYPES,
    MOCK_LEAVE_BALANCE_RESPONSE,
    MOCK_LEAVE_REQUESTS_RESPONSE,
    MOCK_LEAVE_REQUEST_PENDING,
    MOCK_CREATE_LEAVE_SUCCESS,
    MOCK_CREATE_LEAVE_FAILURE,
    MOCK_CANCEL_SUCCESS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_employee(emp_id=MOCK_EMPLOYEE_ID):
    return patch("keka.leave.get_employee_id", return_value=emp_id)

def _patch_get(*side_effects):
    return patch("keka.leave.keka_get", side_effect=list(side_effects))

def _patch_post(return_value):
    return patch("keka.leave.keka_post", return_value=return_value)

def _patch_delete(return_value):
    return patch("keka.leave.keka_delete", return_value=return_value)


# ===========================================================================
# handle_get_leave_balance
# ===========================================================================

class TestHandleGetLeaveBalance:

    def test_returns_formatted_balance(self):
        with _patch_employee(), _patch_get(MOCK_LEAVE_BALANCE_RESPONSE):
            result = handle_get_leave_balance({}, MOCK_EMPLOYEE_EMAIL)

        assert "Sick Leave" in result
        assert "Casual Leave" in result
        assert "10" in result    # available balance
        assert "2" in result     # consumed

    def test_employee_not_found_returns_error(self):
        with patch("keka.leave.get_employee_id", side_effect=ValueError("not found")):
            result = handle_get_leave_balance({}, "nobody@caizin.com")

        assert "Error" in result or "error" in result.lower()

    def test_no_balance_entries_returns_helpful_message(self):
        no_balance = {
            "data": [
                {
                    "employeeIdentifier": MOCK_EMPLOYEE_ID,
                    "leaveBalance": [],
                }
            ],
            "nextPage": None,
        }
        with _patch_employee(), _patch_get(no_balance):
            result = handle_get_leave_balance({}, MOCK_EMPLOYEE_EMAIL)

        assert "No leave balance" in result or "contact HR" in result.lower()

    def test_employee_record_not_in_response_checks_next_pages(self):
        """If employee not on page 1, should fetch page 2."""
        page_1 = {
            "data": [
                {"employeeIdentifier": "other-id", "leaveBalance": []}
            ],
            "nextPage": "https://caizin.keka.com/api/v1/time/leavebalance?pageNumber=2",
        }
        page_2 = MOCK_LEAVE_BALANCE_RESPONSE

        with _patch_employee(), _patch_get(page_1, page_2):
            result = handle_get_leave_balance({}, MOCK_EMPLOYEE_EMAIL)

        assert "Sick Leave" in result

    def test_employee_not_in_any_page_returns_error(self):
        no_match = {
            "data": [
                {"employeeIdentifier": "stranger-id", "leaveBalance": []}
            ],
            "nextPage": None,
        }
        with _patch_employee(), _patch_get(no_match):
            result = handle_get_leave_balance({}, MOCK_EMPLOYEE_EMAIL)

        assert "couldn't find" in result.lower() or "contact HR" in result.lower()


# ===========================================================================
# handle_apply_leave
# ===========================================================================

class TestHandleApplyLeave:

    # ── Successful application ───────────────────────────────────────────────

    def test_successful_casual_leave_application(self):
        args = {
            "leave_type_name": "Casual Leave",
            "from_date":       "2099-03-10",
            "to_date":         "2099-03-12",
            "reason":          "Personal work",
        }
        with (
            patch("keka.leave.validate_leave_dates", return_value=None),
            patch("keka.leave.count_working_days",   return_value=3),
            _patch_employee(),
            _patch_get(MOCK_LEAVE_TYPES),
            _patch_post(MOCK_CREATE_LEAVE_SUCCESS),
        ):
            result = handle_apply_leave(args, MOCK_EMPLOYEE_EMAIL)

        assert "successfully" in result.lower()
        assert "Casual Leave" in result
        assert "3" in result    # working days

    def test_sick_leave_application(self):
        args = {
            "leave_type_name": "Sick Leave",
            "from_date":       "2099-04-07",
            "to_date":         "2099-04-07",
            "reason":          "Fever",
        }
        with (
            patch("keka.leave.validate_leave_dates", return_value=None),
            patch("keka.leave.count_working_days",   return_value=1),
            _patch_employee(),
            _patch_get(MOCK_LEAVE_TYPES),
            _patch_post(MOCK_CREATE_LEAVE_SUCCESS),
        ):
            result = handle_apply_leave(args, MOCK_EMPLOYEE_EMAIL)

        assert "successfully" in result.lower()
        assert "Sick Leave" in result

    # ── Date validation failures ─────────────────────────────────────────────

    def test_blocks_leave_on_weekend(self):
        args = {
            "leave_type_name": "Casual Leave",
            "from_date":       "2099-01-12",   # Saturday
            "to_date":         "2099-01-14",
            "reason":          "Trip",
        }
        with patch("keka.leave.validate_leave_dates", return_value="Leave cannot start on a Saturday."):
            result = handle_apply_leave(args, MOCK_EMPLOYEE_EMAIL)

        assert "Saturday" in result

    def test_blocks_leave_on_public_holiday(self):
        args = {
            "leave_type_name": "Casual Leave",
            "from_date":       "2026-01-26",   # Republic Day
            "to_date":         "2026-01-28",
            "reason":          "Outing",
        }
        with patch("keka.leave.validate_leave_dates", return_value="This is a public holiday."):
            result = handle_apply_leave(args, MOCK_EMPLOYEE_EMAIL)

        assert "holiday" in result.lower()

    def test_blocks_past_date(self):
        args = {
            "leave_type_name": "Sick Leave",
            "from_date":       "2020-01-06",
            "to_date":         "2020-01-08",
            "reason":          "Old request",
        }
        with patch("keka.leave.validate_leave_dates", return_value="Cannot apply leave for a past date."):
            result = handle_apply_leave(args, MOCK_EMPLOYEE_EMAIL)

        assert "past" in result.lower()

    def test_blocks_reversed_date_range(self):
        args = {
            "leave_type_name": "Casual Leave",
            "from_date":       "2099-03-15",
            "to_date":         "2099-03-10",
            "reason":          "",
        }
        with patch("keka.leave.validate_leave_dates", return_value="From date must be before or equal to To date."):
            result = handle_apply_leave(args, MOCK_EMPLOYEE_EMAIL)

        assert "before" in result.lower()

    # ── Leave type resolution ─────────────────────────────────────────────────

    def test_unknown_leave_type_returns_available_list(self):
        args = {
            "leave_type_name": "Funday Leave",
            "from_date":       "2099-03-10",
            "to_date":         "2099-03-10",
            "reason":          "",
        }
        with (
            patch("keka.leave.validate_leave_dates", return_value=None),
            patch("keka.leave.count_working_days",   return_value=1),
            _patch_employee(),
            _patch_get(MOCK_LEAVE_TYPES),
        ):
            result = handle_apply_leave(args, MOCK_EMPLOYEE_EMAIL)

        assert "Funday Leave" in result
        assert "Sick Leave" in result      # shows available types
        assert "Casual Leave" in result

    def test_partial_name_match_works(self):
        """'sick' should match 'Sick Leave' via partial match."""
        args = {
            "leave_type_name": "sick",
            "from_date":       "2099-03-10",
            "to_date":         "2099-03-10",
            "reason":          "",
        }
        with (
            patch("keka.leave.validate_leave_dates", return_value=None),
            patch("keka.leave.count_working_days",   return_value=1),
            _patch_employee(),
            _patch_get(MOCK_LEAVE_TYPES),
            _patch_post(MOCK_CREATE_LEAVE_SUCCESS),
        ):
            result = handle_apply_leave(args, MOCK_EMPLOYEE_EMAIL)

        assert "successfully" in result.lower()

    # ── Keka API rejects the request ─────────────────────────────────────────

    def test_insufficient_balance_shows_keka_error(self):
        args = {
            "leave_type_name": "Sick Leave",
            "from_date":       "2099-03-10",
            "to_date":         "2099-03-20",
            "reason":          "Extended illness",
        }
        with (
            patch("keka.leave.validate_leave_dates", return_value=None),
            patch("keka.leave.count_working_days",   return_value=9),
            _patch_employee(),
            _patch_get(MOCK_LEAVE_TYPES),
            _patch_post(MOCK_CREATE_LEAVE_FAILURE),
        ):
            result = handle_apply_leave(args, MOCK_EMPLOYEE_EMAIL)

        assert "could not be submitted" in result.lower() or "not enough" in result.lower()

    # ── Exception handling ────────────────────────────────────────────────────

    def test_network_error_returns_friendly_message(self):
        args = {
            "leave_type_name": "Casual Leave",
            "from_date":       "2099-03-10",
            "to_date":         "2099-03-10",
            "reason":          "",
        }
        with (
            patch("keka.leave.validate_leave_dates", return_value=None),
            patch("keka.leave.count_working_days",   return_value=1),
            patch("keka.leave.get_employee_id", side_effect=ConnectionError("timeout")),
        ):
            result = handle_apply_leave(args, MOCK_EMPLOYEE_EMAIL)

        assert "Sorry" in result or "Error" in result


# ===========================================================================
# handle_get_leave_requests
# ===========================================================================

class TestHandleGetLeaveRequests:

    def test_returns_formatted_list(self):
        with _patch_employee(), _patch_get(MOCK_LEAVE_REQUESTS_RESPONSE):
            result = handle_get_leave_requests({}, MOCK_EMPLOYEE_EMAIL)

        assert "Sick Leave" in result
        assert "Pending" in result
        assert "3" in result    # days count

    def test_empty_result_returns_no_requests_message(self):
        empty = {"data": [], "nextPage": None, "succeeded": True}
        with _patch_employee(), _patch_get(empty):
            result = handle_get_leave_requests({}, MOCK_EMPLOYEE_EMAIL)

        assert "no leave requests" in result.lower()

    def test_filters_to_logged_in_employee_only(self):
        """Records from other employees must not appear in the response."""
        mixed = {
            "data": [
                {
                    "id":                 "req-001",
                    "employeeIdentifier": MOCK_EMPLOYEE_ID,
                    "fromDate":           "2099-03-10T00:00:00Z",
                    "toDate":             "2099-03-10T00:00:00Z",
                    "status":             1,    # Approved
                    "selection":          [{"leaveTypeName": "Sick Leave", "count": 1}],
                },
                {
                    "id":                 "req-002",
                    "employeeIdentifier": "other-employee-id",
                    "fromDate":           "2099-03-11T00:00:00Z",
                    "toDate":             "2099-03-11T00:00:00Z",
                    "status":             0,
                    "selection":          [{"leaveTypeName": "Casual Leave", "count": 1}],
                },
            ],
            "nextPage": None,
        }
        with _patch_employee(), _patch_get(mixed):
            result = handle_get_leave_requests({}, MOCK_EMPLOYEE_EMAIL)

        assert "Sick Leave" in result
        # The other employee's Casual Leave should NOT appear
        assert result.count("Casual Leave") == 0

    def test_shows_correct_status_labels(self):
        statuses = {0: "Pending", 1: "Approved", 2: "Rejected", 3: "Cancelled", 4: "In Approval"}

        for code, label in statuses.items():
            req = dict(MOCK_LEAVE_REQUEST_PENDING)
            req["status"] = code
            resp = {"data": [req], "nextPage": None}

            with _patch_employee(), _patch_get(resp):
                result = handle_get_leave_requests({}, MOCK_EMPLOYEE_EMAIL)

            assert label in result, f"Status {code} should show '{label}'"

    def test_uses_custom_date_range_from_args(self):
        mock_get = MagicMock(return_value=MOCK_LEAVE_REQUESTS_RESPONSE)
        with _patch_employee(), patch("keka.leave.keka_get", mock_get):
            handle_get_leave_requests(
                {"from_date": "01-03-2099", "to_date": "31-03-2099"},
                MOCK_EMPLOYEE_EMAIL,
            )

        call_params = mock_get.call_args[1].get("params") or mock_get.call_args[0][1]
        assert call_params.get("from") == "01-03-2099"
        assert call_params.get("to")   == "31-03-2099"

    def test_exception_returns_friendly_message(self):
        with patch("keka.leave.get_employee_id", side_effect=RuntimeError("boom")):
            result = handle_get_leave_requests({}, MOCK_EMPLOYEE_EMAIL)

        assert "Sorry" in result or "Error" in result


# ===========================================================================
# handle_cancel_leave
# ===========================================================================

class TestHandleCancelLeave:

    def test_cancels_pending_leave_successfully(self):
        with (
            _patch_employee(),
            _patch_get(MOCK_LEAVE_REQUESTS_RESPONSE),
            _patch_delete(MOCK_CANCEL_SUCCESS),
        ):
            result = handle_cancel_leave(
                {"from_date": "2099-03-10", "to_date": "2099-03-12"},
                MOCK_EMPLOYEE_EMAIL,
            )

        assert "cancelled successfully" in result.lower()
        assert "Sick Leave" in result

    def test_already_cancelled_returns_no_action_message(self):
        cancelled_req = dict(MOCK_LEAVE_REQUEST_PENDING)
        cancelled_req["status"] = 3   # Cancelled
        response = {"data": [cancelled_req], "nextPage": None}

        with _patch_employee(), _patch_get(response):
            result = handle_cancel_leave(
                {"from_date": "2099-03-10", "to_date": "2099-03-12"},
                MOCK_EMPLOYEE_EMAIL,
            )

        assert "already" in result.lower() or "cancelled" in result.lower()

    def test_already_rejected_returns_no_action_message(self):
        rejected_req = dict(MOCK_LEAVE_REQUEST_PENDING)
        rejected_req["status"] = 2   # Rejected
        response = {"data": [rejected_req], "nextPage": None}

        with _patch_employee(), _patch_get(response):
            result = handle_cancel_leave(
                {"from_date": "2099-03-10", "to_date": "2099-03-12"},
                MOCK_EMPLOYEE_EMAIL,
            )

        assert "already" in result.lower() or "rejected" in result.lower()

    def test_no_leave_found_for_dates_returns_error(self):
        empty = {"data": [], "nextPage": None}
        with _patch_employee(), _patch_get(empty):
            result = handle_cancel_leave(
                {"from_date": "2099-06-01", "to_date": "2099-06-03"},
                MOCK_EMPLOYEE_EMAIL,
            )

        assert "couldn't find" in result.lower()

    def test_filters_to_own_leave_only(self):
        """Should not cancel another employee's leave."""
        other_req = dict(MOCK_LEAVE_REQUEST_PENDING)
        other_req["employeeIdentifier"] = "not-me-id"
        response = {"data": [other_req], "nextPage": None}

        with _patch_employee(), _patch_get(response):
            result = handle_cancel_leave(
                {"from_date": "2099-03-10", "to_date": "2099-03-12"},
                MOCK_EMPLOYEE_EMAIL,
            )

        assert "couldn't find" in result.lower()

    def test_keka_api_failure_returns_error_message(self):
        failed_cancel = {"data": None, "succeeded": False, "message": "Cannot cancel approved leave."}

        with (
            _patch_employee(),
            _patch_get(MOCK_LEAVE_REQUESTS_RESPONSE),
            _patch_delete(failed_cancel),
        ):
            result = handle_cancel_leave(
                {"from_date": "2099-03-10", "to_date": "2099-03-12"},
                MOCK_EMPLOYEE_EMAIL,
            )

        assert "Could not cancel" in result or "Cannot cancel" in result

    def test_prefers_active_over_cancelled_when_multiple_records(self):
        """If both a cancelled and a pending record exist for the same range,
        the pending one should be targeted for cancellation."""
        pending_req  = dict(MOCK_LEAVE_REQUEST_PENDING)
        pending_req["id"] = "req-pending"

        cancelled_req = dict(MOCK_LEAVE_REQUEST_PENDING)
        cancelled_req["id"]     = "req-cancelled"
        cancelled_req["status"] = 3

        response = {"data": [cancelled_req, pending_req], "nextPage": None}

        mock_delete = MagicMock(return_value=MOCK_CANCEL_SUCCESS)
        with (
            _patch_employee(),
            _patch_get(response),
            patch("keka.leave.keka_delete", mock_delete),
        ):
            result = handle_cancel_leave(
                {"from_date": "2099-03-10", "to_date": "2099-03-12"},
                MOCK_EMPLOYEE_EMAIL,
            )

        assert "cancelled successfully" in result.lower()
        # DELETE should have been called with the PENDING request's id
        delete_url = mock_delete.call_args[0][0]
        assert "req-pending" in delete_url

    def test_exception_returns_friendly_message(self):
        with patch("keka.leave.get_employee_id", side_effect=ConnectionError("network down")):
            result = handle_cancel_leave(
                {"from_date": "2099-03-10", "to_date": "2099-03-12"},
                MOCK_EMPLOYEE_EMAIL,
            )

        assert "Sorry" in result or "Error" in result


# ===========================================================================
# _find_leave_type_id (internal helper — tested indirectly via apply_leave
# but also directly here for edge cases)
# ===========================================================================

class TestFindLeaveTypeId:
    """Tests for the internal _find_leave_type_id helper."""

    def test_exact_match(self):
        from keka.leave import _find_leave_type_id
        result = _find_leave_type_id(MOCK_LEAVE_TYPES["data"], "Sick Leave")
        assert result == "feb73dda-0001"

    def test_case_insensitive_exact_match(self):
        from keka.leave import _find_leave_type_id
        result = _find_leave_type_id(MOCK_LEAVE_TYPES["data"], "sick leave")
        assert result == "feb73dda-0001"

    def test_partial_match(self):
        from keka.leave import _find_leave_type_id
        result = _find_leave_type_id(MOCK_LEAVE_TYPES["data"], "sick")
        assert result == "feb73dda-0001"

    def test_no_match_returns_none(self):
        from keka.leave import _find_leave_type_id
        result = _find_leave_type_id(MOCK_LEAVE_TYPES["data"], "FunDay Leave")
        assert result is None

    def test_empty_list_returns_none(self):
        from keka.leave import _find_leave_type_id
        result = _find_leave_type_id([], "Sick Leave")
        assert result is None
