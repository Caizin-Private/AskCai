"""
tests/test_apply_leave.py

Unit tests for the apply-leave flow.
All Keka HTTP calls and Anthropic API calls are mocked — no real leave is submitted.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ─── shared test data ──────────────────────────────────────────────────────

FAKE_TOKEN = "fake-bearer-token"
EMP_ID     = "emp-uuid-123"
LT_ID      = "lt-uuid-456"

# Valid Phase-1 JSON that _extract_endpoint can parse
ENDPOINT_JSON = (
    '{"title": "Time", "path": "/time/leaverequests", "method": "POST",'
    ' "params": {}, "body": {"employeeId": "string", "leaveTypeId": "string"}}'
)
SUCCESS_TEXT = "✅ Leave request submitted successfully."

BASE_PARAMS = {
    "leave_type": "Casual Leave",
    "from_date":  "2025-06-23",
    "to_date":    "2025-06-25",
    "session":    "full",
    "reason":     "Personal work",
}


# ─── helpers ───────────────────────────────────────────────────────────────

def _text_block(text: str):
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


def _response(text: str):
    r = MagicMock()
    r.stop_reason = "end_turn"
    r.content = [_text_block(text)]
    return r


async def _fake_to_thread(fn, *args, **kwargs):
    """Replaces asyncio.to_thread — calls the (already-patched) fn directly."""
    return fn(*args, **kwargs)


# ─── get_leave_type_id ─────────────────────────────────────────────────────

class TestGetLeaveTypeId:
    """Unit tests for keka.client.get_leave_type_id — HTTP fully mocked."""

    def setup_method(self):
        import keka.client as c
        c._leave_type_cache.clear()

    @patch("keka.client.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.client.requests.get")
    def test_returns_uuid_when_found(self, mock_get, _tok):
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.return_value = [
            {"name": "Casual Leave", "id": LT_ID},
        ]
        from keka.client import get_leave_type_id
        assert get_leave_type_id("Casual Leave") == LT_ID

    @patch("keka.client.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.client.requests.get")
    def test_case_insensitive(self, mock_get, _tok):
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.return_value = [
            {"name": "Casual Leave", "id": LT_ID},
        ]
        from keka.client import get_leave_type_id
        assert get_leave_type_id("casual leave") == LT_ID
        assert get_leave_type_id("CASUAL LEAVE") == LT_ID

    @patch("keka.client.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.client.requests.get")
    def test_returns_none_when_not_found(self, mock_get, _tok):
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.return_value = [
            {"name": "Sick Leave", "id": "other-uuid"},
        ]
        from keka.client import get_leave_type_id
        assert get_leave_type_id("Casual Leave") is None

    @patch("keka.client.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.client.requests.get")
    def test_cache_hit_skips_http(self, mock_get, _tok):
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.return_value = [
            {"name": "Casual Leave", "id": LT_ID},
        ]
        from keka.client import get_leave_type_id
        get_leave_type_id("Casual Leave")  # populates cache
        get_leave_type_id("Casual Leave")  # should hit cache, no HTTP
        assert mock_get.call_count == 1

    @patch("keka.client.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.client.requests.get")
    def test_dict_response_with_data_key(self, mock_get, _tok):
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.return_value = {
            "data": [{"name": "Casual Leave", "id": LT_ID}]
        }
        from keka.client import get_leave_type_id
        assert get_leave_type_id("Casual Leave") == LT_ID

    @patch("keka.client.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.client.requests.get")
    def test_alternate_field_names(self, mock_get, _tok):
        """Handles displayName / leaveTypeId / identifier variants in Keka response."""
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.return_value = [
            {"displayName": "Casual Leave", "leaveTypeId": LT_ID},
        ]
        from keka.client import get_leave_type_id
        assert get_leave_type_id("Casual Leave") == LT_ID


# ─── _extract_endpoint ─────────────────────────────────────────────────────

class TestExtractEndpoint:

    def test_parses_valid_post_json(self):
        from keka.mcp_agent import _extract_endpoint
        result = _extract_endpoint(ENDPOINT_JSON)
        assert result["path"]   == "/time/leaverequests"
        assert result["method"] == "POST"
        assert result["title"]  == "Time"
        assert result["body"]   == {"employeeId": "string", "leaveTypeId": "string"}

    def test_defaults_params_and_body_when_absent(self):
        from keka.mcp_agent import _extract_endpoint
        text = '{"title": "Core Hr", "path": "/hris/employees", "method": "GET"}'
        result = _extract_endpoint(text)
        assert result["params"] == {}
        assert result["body"]   == {}

    def test_returns_none_for_no_json(self):
        from keka.mcp_agent import _extract_endpoint
        assert _extract_endpoint("no JSON here") is None

    def test_returns_none_for_missing_required_keys(self):
        from keka.mcp_agent import _extract_endpoint
        assert _extract_endpoint('{"title": "Foo"}') is None

    def test_extracts_json_embedded_in_prose(self):
        from keka.mcp_agent import _extract_endpoint
        text = f"Here is the endpoint info: {ENDPOINT_JSON} — use it directly."
        result = _extract_endpoint(text)
        assert result is not None
        assert result["path"] == "/time/leaverequests"


# ─── _SESSION_MAP ──────────────────────────────────────────────────────────

class TestSessionMap:

    def test_full_day(self):
        from keka.mcp_agent import _SESSION_MAP
        assert _SESSION_MAP["full"] == (1, 2)

    def test_first_half(self):
        from keka.mcp_agent import _SESSION_MAP
        assert _SESSION_MAP["first_half"] == (1, 1)

    def test_second_half(self):
        from keka.mcp_agent import _SESSION_MAP
        assert _SESSION_MAP["second_half"] == (2, 2)


# ─── ask_keka_mcp_apply_leave ──────────────────────────────────────────────

class TestAskKekaMcpApplyLeave:
    """
    Integration-level tests for ask_keka_mcp_apply_leave.
    Patches applied at the keka.mcp_agent namespace so asyncio.to_thread
    invokes the mocks rather than the real Keka HTTP calls.
    """

    def _make_client(self, p1_text=ENDPOINT_JSON, p2_text=SUCCESS_TEXT):
        mock_client = MagicMock()
        mock_client.beta.messages.create = AsyncMock(
            side_effect=[_response(p1_text), _response(p2_text)]
        )
        return mock_client

    def _run(self, coro):
        return asyncio.run(coro)

    # ── happy path ──────────────────────────────────────────────────────────

    @patch("keka.mcp_agent.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("keka.mcp_agent.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.mcp_agent.get_employee_id", return_value=EMP_ID)
    @patch("keka.mcp_agent.get_leave_type_id", return_value=LT_ID)
    @patch("keka.mcp_agent.anthropic.AsyncAnthropic")
    def test_happy_path_full_day(self, mock_cls, _lt, _emp, _tok, _thread):
        mock_cls.return_value = self._make_client()
        from keka.mcp_agent import ask_keka_mcp_apply_leave
        result = self._run(ask_keka_mcp_apply_leave(BASE_PARAMS, "emp@caizin.com", "api-key"))
        assert result == SUCCESS_TEXT

    # ── token fetch fails ───────────────────────────────────────────────────

    @patch("keka.mcp_agent.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("keka.mcp_agent.get_access_token", side_effect=RuntimeError("auth down"))
    def test_token_failure_returns_error(self, _tok, _thread):
        from keka.mcp_agent import ask_keka_mcp_apply_leave
        result = self._run(ask_keka_mcp_apply_leave(BASE_PARAMS, "emp@caizin.com", "api-key"))
        assert "couldn't connect" in result.lower()
        assert "auth down" in result

    # ── Phase 1 failures ─────────────────────────────────────────────────────

    @patch("keka.mcp_agent.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("keka.mcp_agent.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.mcp_agent.anthropic.AsyncAnthropic")
    def test_phase1_exception_returns_error(self, mock_cls, _tok, _thread):
        mock_client = MagicMock()
        mock_client.beta.messages.create = AsyncMock(side_effect=Exception("timeout"))
        mock_cls.return_value = mock_client
        from keka.mcp_agent import ask_keka_mcp_apply_leave
        result = self._run(ask_keka_mcp_apply_leave(BASE_PARAMS, "emp@caizin.com", "api-key"))
        assert "couldn't identify the leave request endpoint" in result

    @patch("keka.mcp_agent.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("keka.mcp_agent.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.mcp_agent.anthropic.AsyncAnthropic")
    def test_phase1_unparseable_returns_error(self, mock_cls, _tok, _thread):
        mock_client = MagicMock()
        mock_client.beta.messages.create = AsyncMock(
            return_value=_response("I could not find the endpoint sorry.")
        )
        mock_cls.return_value = mock_client
        from keka.mcp_agent import ask_keka_mcp_apply_leave
        result = self._run(ask_keka_mcp_apply_leave(BASE_PARAMS, "emp@caizin.com", "api-key"))
        assert "couldn't identify the leave request endpoint" in result

    # ── employee / leave-type resolution failures ───────────────────────────

    @patch("keka.mcp_agent.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("keka.mcp_agent.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.mcp_agent.get_employee_id", return_value=None)
    @patch("keka.mcp_agent.get_leave_type_id", return_value=LT_ID)
    @patch("keka.mcp_agent.anthropic.AsyncAnthropic")
    def test_employee_not_found(self, mock_cls, _lt, _emp, _tok, _thread):
        mock_cls.return_value = self._make_client()
        from keka.mcp_agent import ask_keka_mcp_apply_leave
        result = self._run(ask_keka_mcp_apply_leave(BASE_PARAMS, "emp@caizin.com", "api-key"))
        assert "couldn't identify your employee profile" in result

    @patch("keka.mcp_agent.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("keka.mcp_agent.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.mcp_agent.get_employee_id", return_value=EMP_ID)
    @patch("keka.mcp_agent.get_leave_type_id", return_value=None)
    @patch("keka.mcp_agent.anthropic.AsyncAnthropic")
    def test_leave_type_not_found(self, mock_cls, _lt, _emp, _tok, _thread):
        mock_cls.return_value = self._make_client()
        from keka.mcp_agent import ask_keka_mcp_apply_leave
        result = self._run(ask_keka_mcp_apply_leave(BASE_PARAMS, "emp@caizin.com", "api-key"))
        assert "Casual Leave" in result
        assert "not found in Keka" in result

    # ── Phase 2 failure ──────────────────────────────────────────────────────

    @patch("keka.mcp_agent.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("keka.mcp_agent.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.mcp_agent.get_employee_id", return_value=EMP_ID)
    @patch("keka.mcp_agent.get_leave_type_id", return_value=LT_ID)
    @patch("keka.mcp_agent.anthropic.AsyncAnthropic")
    def test_phase2_exception_returns_error(self, mock_cls, _lt, _emp, _tok, _thread):
        mock_client = MagicMock()
        mock_client.beta.messages.create = AsyncMock(
            side_effect=[_response(ENDPOINT_JSON), Exception("network error")]
        )
        mock_cls.return_value = mock_client
        from keka.mcp_agent import ask_keka_mcp_apply_leave
        result = self._run(ask_keka_mcp_apply_leave(BASE_PARAMS, "emp@caizin.com", "api-key"))
        assert "couldn't submit your leave request" in result

    @patch("keka.mcp_agent.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("keka.mcp_agent.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.mcp_agent.get_employee_id", return_value=EMP_ID)
    @patch("keka.mcp_agent.get_leave_type_id", return_value=LT_ID)
    @patch("keka.mcp_agent.anthropic.AsyncAnthropic")
    def test_phase2_empty_response_returns_fallback(self, mock_cls, _lt, _emp, _tok, _thread):
        mock_cls.return_value = self._make_client(p2_text="")
        from keka.mcp_agent import ask_keka_mcp_apply_leave
        result = self._run(ask_keka_mcp_apply_leave(BASE_PARAMS, "emp@caizin.com", "api-key"))
        assert "verify in Keka" in result

    # ── half-day date guard ──────────────────────────────────────────────────

    @patch("keka.mcp_agent.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("keka.mcp_agent.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.mcp_agent.get_employee_id", return_value=EMP_ID)
    @patch("keka.mcp_agent.get_leave_type_id", return_value=LT_ID)
    @patch("keka.mcp_agent.anthropic.AsyncAnthropic")
    def test_first_half_overrides_to_date_and_sessions(self, mock_cls, _lt, _emp, _tok, _thread):
        mock_client = MagicMock()
        mock_client.beta.messages.create = AsyncMock(
            side_effect=[_response(ENDPOINT_JSON), _response(SUCCESS_TEXT)]
        )
        mock_cls.return_value = mock_client

        params = {**BASE_PARAMS, "session": "first_half", "from_date": "2025-06-23", "to_date": "2025-06-25"}
        from keka.mcp_agent import ask_keka_mcp_apply_leave
        self._run(ask_keka_mcp_apply_leave(params, "emp@caizin.com", "api-key"))

        p2_content = mock_client.beta.messages.create.call_args_list[1].kwargs["messages"][0]["content"]
        assert "toDate:        2025-06-23" in p2_content   # to_date forced to from_date
        assert "fromSession:   1" in p2_content
        assert "toSession:     1" in p2_content

    @patch("keka.mcp_agent.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("keka.mcp_agent.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.mcp_agent.get_employee_id", return_value=EMP_ID)
    @patch("keka.mcp_agent.get_leave_type_id", return_value=LT_ID)
    @patch("keka.mcp_agent.anthropic.AsyncAnthropic")
    def test_second_half_overrides_to_date_and_sessions(self, mock_cls, _lt, _emp, _tok, _thread):
        mock_client = MagicMock()
        mock_client.beta.messages.create = AsyncMock(
            side_effect=[_response(ENDPOINT_JSON), _response(SUCCESS_TEXT)]
        )
        mock_cls.return_value = mock_client

        params = {**BASE_PARAMS, "session": "second_half", "from_date": "2025-06-23", "to_date": "2025-06-25"}
        from keka.mcp_agent import ask_keka_mcp_apply_leave
        self._run(ask_keka_mcp_apply_leave(params, "emp@caizin.com", "api-key"))

        p2_content = mock_client.beta.messages.create.call_args_list[1].kwargs["messages"][0]["content"]
        assert "toDate:        2025-06-23" in p2_content
        assert "fromSession:   2" in p2_content
        assert "toSession:     2" in p2_content

    @patch("keka.mcp_agent.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("keka.mcp_agent.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.mcp_agent.get_employee_id", return_value=EMP_ID)
    @patch("keka.mcp_agent.get_leave_type_id", return_value=LT_ID)
    @patch("keka.mcp_agent.anthropic.AsyncAnthropic")
    def test_full_day_preserves_original_to_date(self, mock_cls, _lt, _emp, _tok, _thread):
        mock_client = MagicMock()
        mock_client.beta.messages.create = AsyncMock(
            side_effect=[_response(ENDPOINT_JSON), _response(SUCCESS_TEXT)]
        )
        mock_cls.return_value = mock_client

        params = {**BASE_PARAMS, "session": "full", "from_date": "2025-06-23", "to_date": "2025-06-25"}
        from keka.mcp_agent import ask_keka_mcp_apply_leave
        self._run(ask_keka_mcp_apply_leave(params, "emp@caizin.com", "api-key"))

        p2_content = mock_client.beta.messages.create.call_args_list[1].kwargs["messages"][0]["content"]
        assert "toDate:        2025-06-25" in p2_content   # to_date NOT overridden
        assert "fromSession:   1" in p2_content
        assert "toSession:     2" in p2_content

    # ── Phase 2 receives resolved IDs ────────────────────────────────────────

    @patch("keka.mcp_agent.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("keka.mcp_agent.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.mcp_agent.get_employee_id", return_value=EMP_ID)
    @patch("keka.mcp_agent.get_leave_type_id", return_value=LT_ID)
    @patch("keka.mcp_agent.anthropic.AsyncAnthropic")
    def test_phase2_content_contains_resolved_ids(self, mock_cls, _lt, _emp, _tok, _thread):
        mock_client = MagicMock()
        mock_client.beta.messages.create = AsyncMock(
            side_effect=[_response(ENDPOINT_JSON), _response(SUCCESS_TEXT)]
        )
        mock_cls.return_value = mock_client

        from keka.mcp_agent import ask_keka_mcp_apply_leave
        self._run(ask_keka_mcp_apply_leave(BASE_PARAMS, "emp@caizin.com", "api-key"))

        p2_content = mock_client.beta.messages.create.call_args_list[1].kwargs["messages"][0]["content"]
        assert EMP_ID in p2_content
        assert LT_ID in p2_content
        assert FAKE_TOKEN in p2_content   # auth header injected

    # ── Anthropic client created only once per call ──────────────────────────

    @patch("keka.mcp_agent.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("keka.mcp_agent.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.mcp_agent.get_employee_id", return_value=EMP_ID)
    @patch("keka.mcp_agent.get_leave_type_id", return_value=LT_ID)
    @patch("keka.mcp_agent.anthropic.AsyncAnthropic")
    def test_anthropic_client_created_once(self, mock_cls, _lt, _emp, _tok, _thread):
        mock_cls.return_value = self._make_client()
        from keka.mcp_agent import ask_keka_mcp_apply_leave
        self._run(ask_keka_mcp_apply_leave(BASE_PARAMS, "emp@caizin.com", "api-key"))
        assert mock_cls.call_count == 1

    # ── existing ask_keka_mcp still works (no regression) ───────────────────

    @patch("keka.mcp_agent.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("keka.mcp_agent.get_access_token", return_value=FAKE_TOKEN)
    @patch("keka.mcp_agent.get_employee_id", return_value=EMP_ID)
    @patch("keka.mcp_agent.anthropic.AsyncAnthropic")
    def test_existing_ask_keka_mcp_unaffected(self, mock_cls, _emp, _tok, _thread):
        """Smoke-test that the get-leave-balance path still runs without error."""
        get_leave_json = (
            '{"title": "Core Hr", "path": "/hris/employees/leavesummary",'
            ' "method": "GET", "params": {"employeeId": "filter"}, "body": {}}'
        )
        mock_client = MagicMock()
        mock_client.beta.messages.create = AsyncMock(
            side_effect=[
                _response(get_leave_json),
                _response("You have 5 Casual Leave days remaining."),
            ]
        )
        mock_cls.return_value = mock_client

        from keka.mcp_agent import ask_keka_mcp
        result = self._run(ask_keka_mcp("What is my leave balance?", "emp@caizin.com", "api-key"))
        assert "leave" in result.lower()
        # apply-leave function was never called
        assert mock_client.beta.messages.create.call_count == 2
