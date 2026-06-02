"""
tests/test_client.py
Unit tests for keka/client.py.
All HTTP calls are mocked — no real Keka API requests are made.
"""

import time
import pytest
from unittest.mock import patch, MagicMock

import keka.client as client
from tests.conftest import (
    MOCK_ACCESS_TOKEN,
    MOCK_TOKEN_RESPONSE,
    MOCK_EMPLOYEE_ID,
    MOCK_EMPLOYEE_EMAIL,
    MOCK_EMPLOYEES_PAGE_1,
)


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Helper — returns a mock requests.Response."""
    resp = MagicMock()
    resp.status_code  = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()   # no-op for 200
    return resp


def _mock_error_response(status_code: int = 400) -> MagicMock:
    from requests.exceptions import HTTPError
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status.side_effect = HTTPError(f"HTTP {status_code}")
    return resp


# ---------------------------------------------------------------------------
# get_access_token
# ---------------------------------------------------------------------------

class TestGetAccessToken:

    @patch("keka.client.requests.post")
    def test_fetches_token_on_first_call(self, mock_post):
        mock_post.return_value = _mock_response(MOCK_TOKEN_RESPONSE)

        token = client.get_access_token()

        assert token == MOCK_ACCESS_TOKEN
        mock_post.assert_called_once()

        # Verify correct params sent to Keka token endpoint
        call_kwargs = mock_post.call_args
        data_sent = call_kwargs.kwargs.get("data") or call_kwargs.args[1] if len(call_kwargs.args) > 1 else call_kwargs.kwargs.get("data", {})
        # use call_args[1] which is kwargs
        data_sent = mock_post.call_args[1]["data"]
        assert data_sent["grant_type"] == "kekaapi"
        assert data_sent["scope"] == "kekaapi"

    @patch("keka.client.requests.post")
    def test_returns_cached_token_without_re_fetching(self, mock_post):
        mock_post.return_value = _mock_response(MOCK_TOKEN_RESPONSE)

        token1 = client.get_access_token()
        token2 = client.get_access_token()

        assert token1 == token2 == MOCK_ACCESS_TOKEN
        mock_post.assert_called_once()   # only one network call

    @patch("keka.client.requests.post")
    def test_refreshes_expired_token(self, mock_post):
        mock_post.return_value = _mock_response(MOCK_TOKEN_RESPONSE)

        # Seed an expired token
        client._token_cache["access_token"] = "old-token"
        client._token_cache["expires_at"]   = time.time() - 100   # already expired

        token = client.get_access_token()

        assert token == MOCK_ACCESS_TOKEN
        mock_post.assert_called_once()

    @patch("keka.client.requests.post")
    def test_http_error_propagates(self, mock_post):
        from requests.exceptions import HTTPError
        mock_post.return_value = _mock_error_response(401)

        with pytest.raises(HTTPError):
            client.get_access_token()


# ---------------------------------------------------------------------------
# keka_get
# ---------------------------------------------------------------------------

class TestKekаGet:

    @patch("keka.client.requests.post")
    @patch("keka.client.requests.get")
    def test_get_calls_correct_url(self, mock_get, mock_post):
        mock_post.return_value = _mock_response(MOCK_TOKEN_RESPONSE)
        mock_get.return_value  = _mock_response({"data": [], "succeeded": True})

        result = client.keka_get("/time/leavetypes")

        assert result == {"data": [], "succeeded": True}
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith("/time/leavetypes")

    @patch("keka.client.requests.post")
    @patch("keka.client.requests.get")
    def test_get_passes_query_params(self, mock_get, mock_post):
        mock_post.return_value = _mock_response(MOCK_TOKEN_RESPONSE)
        mock_get.return_value  = _mock_response({"data": [], "succeeded": True})

        client.keka_get("/time/leaverequests", {"from": "01-01-2099", "to": "31-12-2099"})

        call_kwargs = mock_get.call_args[1]
        params = call_kwargs.get("params", {})
        assert params["from"] == "01-01-2099"
        assert params["to"]   == "31-12-2099"

    @patch("keka.client.requests.post")
    @patch("keka.client.requests.get")
    def test_get_http_error_propagates(self, mock_get, mock_post):
        from requests.exceptions import HTTPError
        mock_post.return_value = _mock_response(MOCK_TOKEN_RESPONSE)
        mock_get.return_value  = _mock_error_response(500)

        with pytest.raises(HTTPError):
            client.keka_get("/time/leavetypes")


# ---------------------------------------------------------------------------
# keka_post
# ---------------------------------------------------------------------------

class TestKekaPost:

    @patch("keka.client.requests.post")
    def test_post_returns_parsed_json(self, mock_post):
        response_data = {"data": "new-leave-id", "succeeded": True}

        # First call = token, second call = the actual POST
        mock_post.side_effect = [
            _mock_response(MOCK_TOKEN_RESPONSE),
            _mock_response(response_data),
        ]

        payload = {"employeeId": MOCK_EMPLOYEE_ID, "fromDate": "2099-03-10"}
        result  = client.keka_post("/time/leaverequests", payload)

        assert result == response_data
        assert mock_post.call_count == 2   # token + actual post

    @patch("keka.client.requests.post")
    def test_post_sends_json_body(self, mock_post):
        mock_post.side_effect = [
            _mock_response(MOCK_TOKEN_RESPONSE),
            _mock_response({"data": "id", "succeeded": True}),
        ]

        payload = {"employeeId": "abc-123", "fromDate": "2099-03-10"}
        client.keka_post("/time/leaverequests", payload)

        actual_call = mock_post.call_args_list[1]
        assert actual_call[1]["json"] == payload


# ---------------------------------------------------------------------------
# get_employee_id
# ---------------------------------------------------------------------------

class TestGetEmployeeId:

    @patch("keka.client.requests.post")
    @patch("keka.client.requests.get")
    def test_finds_employee_on_first_page(self, mock_get, mock_post):
        mock_post.return_value = _mock_response(MOCK_TOKEN_RESPONSE)
        mock_get.return_value  = _mock_response(MOCK_EMPLOYEES_PAGE_1)

        emp_id = client.get_employee_id(MOCK_EMPLOYEE_EMAIL)

        assert emp_id == MOCK_EMPLOYEE_ID

    @patch("keka.client.requests.post")
    @patch("keka.client.requests.get")
    def test_email_lookup_is_case_insensitive(self, mock_get, mock_post):
        mock_post.return_value = _mock_response(MOCK_TOKEN_RESPONSE)
        mock_get.return_value  = _mock_response(MOCK_EMPLOYEES_PAGE_1)

        emp_id = client.get_employee_id(MOCK_EMPLOYEE_EMAIL.upper())

        assert emp_id == MOCK_EMPLOYEE_ID

    @patch("keka.client.requests.post")
    @patch("keka.client.requests.get")
    def test_caches_result_after_first_lookup(self, mock_get, mock_post):
        mock_post.return_value = _mock_response(MOCK_TOKEN_RESPONSE)
        mock_get.return_value  = _mock_response(MOCK_EMPLOYEES_PAGE_1)

        client.get_employee_id(MOCK_EMPLOYEE_EMAIL)   # first call
        client.get_employee_id(MOCK_EMPLOYEE_EMAIL)   # second call — should use cache

        # GET /hris/employees called only once
        assert mock_get.call_count == 1

    @patch("keka.client.requests.post")
    @patch("keka.client.requests.get")
    def test_finds_employee_on_second_page(self, mock_get, mock_post):
        mock_post.return_value = _mock_response(MOCK_TOKEN_RESPONSE)

        page_1 = {
            "data":     [{"id": "other-id", "email": "other@caizin.com"}],
            "nextPage": "https://caizin.keka.com/api/v1/hris/employees?pageNumber=2",
            "succeeded": True,
        }
        page_2 = {
            "data":     [{"id": MOCK_EMPLOYEE_ID, "email": MOCK_EMPLOYEE_EMAIL}],
            "nextPage": None,
            "succeeded": True,
        }
        mock_get.side_effect = [_mock_response(page_1), _mock_response(page_2)]

        emp_id = client.get_employee_id(MOCK_EMPLOYEE_EMAIL)

        assert emp_id == MOCK_EMPLOYEE_ID
        assert mock_get.call_count == 2

    @patch("keka.client.requests.post")
    @patch("keka.client.requests.get")
    def test_raises_for_unknown_email(self, mock_get, mock_post):
        mock_post.return_value = _mock_response(MOCK_TOKEN_RESPONSE)
        mock_get.return_value  = _mock_response({
            "data": [{"id": "abc", "email": "someone@caizin.com"}],
            "nextPage": None,
        })

        with pytest.raises(ValueError, match="No Keka employee found"):
            client.get_employee_id("ghost@caizin.com")
