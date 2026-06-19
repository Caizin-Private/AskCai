"""
Characterization tests for Caizin-HR-Bot/keka/client.py and keka/mcp_agent.py

Language:  Python 3.11+
Framework: pytest  (pip install pytest pytest-asyncio)
Run:       cd Caizin-HR-Bot && pytest tests/test_keka.py -v

All network calls are mocked. Tests document CURRENT behavior only.
"""

import os
import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub packages that may not be installed
# ---------------------------------------------------------------------------
os.environ.setdefault("KEKA_CLIENT_ID",     "test-client-id")
os.environ.setdefault("KEKA_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("KEKA_API_KEY",       "test-api-key")
os.environ.setdefault("KEKA_TEST_EMAIL",    "recruiter@caizin.com")

for _pkg in ["requests", "dotenv", "anthropic"]:
    sys.modules.setdefault(_pkg, types.ModuleType(_pkg))

sys.modules["dotenv"].load_dotenv = lambda: None

_fake_requests = sys.modules["requests"]
_fake_requests.post = MagicMock()

_fake_anthropic = sys.modules["anthropic"]
_fake_anthropic.AsyncAnthropic = MagicMock
_fake_anthropic.Anthropic = MagicMock

# Re-import fresh copies
sys.modules.pop("keka", None)
sys.modules.pop("keka.client", None)
sys.modules.pop("keka.mcp_agent", None)

import keka.client as keka_client  # noqa: E402
import keka.mcp_agent as mcp_agent  # noqa: E402


# ===========================================================================
# keka/client.py — constants
# ===========================================================================

class TestKekaClientConstants:
    def test_mcp_url_is_hardcoded_not_from_env(self):
        """
        Test name: keka_mcp_url_hardcoded
        KEKA_MCP_URL is a hardcoded constant, not sourced from environment
        variables — changes to env have no effect.
        """
        assert keka_client.KEKA_MCP_URL == "https://developers.keka.com/mcp"

    def test_default_test_email(self):
        """
        Test name: keka_default_test_email
        TEST_EMPLOYEE_EMAIL defaults to 'recruiter@caizin.com' when
        KEKA_TEST_EMAIL env var is not set.
        """
        with patch.dict(os.environ, {}, clear=False):
            # Value is already resolved at import time; test the default
            assert keka_client.TEST_EMPLOYEE_EMAIL == "recruiter@caizin.com"

    def test_default_token_url(self):
        """
        Test name: keka_default_token_url
        KEKA_TOKEN_URL defaults to the Keka login endpoint.
        """
        assert keka_client.KEKA_TOKEN_URL == "https://login.keka.com/connect/token"

    def test_default_base_url(self):
        """
        Test name: keka_default_base_url
        KEKA_BASE_URL defaults to the Caizin Keka tenant API.
        """
        assert keka_client.KEKA_BASE_URL == "https://caizin.keka.com/api/v1"


# ===========================================================================
# keka/client.py — get_access_token caching
# ===========================================================================

class TestGetAccessToken:
    def setup_method(self):
        """Reset token cache before each test."""
        keka_client._token_cache["access_token"] = None
        keka_client._token_cache["expires_at"] = 0.0

    def test_cache_hit_returns_existing_token_without_http_call(self):
        """
        Test name: token_cache_hit_no_http_call
        When a valid token exists and expires_at is > now + 60 seconds,
        the cached token is returned and no HTTP POST is made.
        """
        keka_client._token_cache["access_token"] = "cached-token-abc"
        keka_client._token_cache["expires_at"] = time.time() + 7200  # 2 hours ahead

        with patch.object(sys.modules["requests"], "post") as mock_post:
            result = keka_client.get_access_token()

        assert result == "cached-token-abc"
        mock_post.assert_not_called()

    def test_cache_miss_within_60s_of_expiry_triggers_refresh(self):
        """
        Test name: token_cache_refresh_within_60s_expiry
        When the token expires within the next 60 seconds (expires_at < now + 60),
        a new HTTP POST is made to refresh the token.
        """
        keka_client._token_cache["access_token"] = "almost-expired-token"
        keka_client._token_cache["expires_at"] = time.time() + 30  # expires in 30s

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "new-token-xyz",
            "expires_in": 86400,
        }
        mock_resp.raise_for_status = MagicMock()

        with patch.object(sys.modules["requests"], "post", return_value=mock_resp) as mock_post:
            result = keka_client.get_access_token()

        assert result == "new-token-xyz"
        mock_post.assert_called_once()

    def test_cache_miss_when_no_token_set(self):
        """
        Test name: token_cache_miss_no_existing_token
        With no cached token (access_token=None), an HTTP POST is always made.
        """
        keka_client._token_cache["access_token"] = None
        keka_client._token_cache["expires_at"] = 0.0

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "brand-new", "expires_in": 86400}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(sys.modules["requests"], "post", return_value=mock_resp) as mock_post:
            keka_client.get_access_token()

        mock_post.assert_called_once()

    def test_token_stored_in_cache_after_refresh(self):
        """
        Test name: token_stored_in_cache_after_refresh
        After a successful refresh, the new token and expiry are written
        back into _token_cache.
        """
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "stored-token", "expires_in": 3600}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(sys.modules["requests"], "post", return_value=mock_resp):
            keka_client.get_access_token()

        assert keka_client._token_cache["access_token"] == "stored-token"
        assert keka_client._token_cache["expires_at"] > time.time() + 3500

    def test_missing_expires_in_defaults_to_86400(self):
        """
        Test name: token_missing_expires_in_defaults_86400
        When the response omits 'expires_in', the cache uses 86400 seconds
        (24 hours) as the default expiry window.
        """
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "token-no-expiry"}  # no expires_in
        mock_resp.raise_for_status = MagicMock()

        before = time.time()
        with patch.object(sys.modules["requests"], "post", return_value=mock_resp):
            keka_client.get_access_token()

        expected_min = before + 86400 - 5  # 5s tolerance
        assert keka_client._token_cache["expires_at"] >= expected_min

    def test_http_request_uses_form_encoding(self):
        """
        Test name: token_request_form_encoded
        The token request uses Content-Type: application/x-www-form-urlencoded
        and a grant_type of 'kekaapi'.
        """
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "t", "expires_in": 3600}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(sys.modules["requests"], "post", return_value=mock_resp) as mock_post:
            keka_client.get_access_token()

        _, kwargs = mock_post.call_args
        assert kwargs["data"]["grant_type"] == "kekaapi"
        assert kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
        assert kwargs["timeout"] == 10


# ===========================================================================
# keka/mcp_agent.py — ask_keka_mcp error handling
# ===========================================================================

class TestAskKekaMcp:
    @pytest.mark.asyncio
    async def test_token_fetch_failure_returns_error_string(self):
        """
        Test name: mcp_token_failure_returns_error_string
        When get_access_token raises an exception, ask_keka_mcp catches it
        and returns a user-friendly error string starting with
        "Sorry, I couldn't connect to Keka right now."
        mcp_agent imports get_access_token directly, so patch its local binding.
        """
        with patch("keka.mcp_agent.get_access_token", side_effect=Exception("network timeout")):
            result = await mcp_agent.ask_keka_mcp("what is my leave?", "user@caizin.com", "key")

        assert result.startswith("Sorry, I couldn't connect to Keka right now.")
        assert "network timeout" in result

    @pytest.mark.asyncio
    async def test_anthropic_api_exception_returns_error_string(self):
        """
        Test name: mcp_api_exception_returns_error_string
        When the Anthropic API call raises an exception, ask_keka_mcp returns
        a string starting with "Sorry, I couldn't complete your HR request."
        """
        mock_token = "valid-token"
        with patch("keka.client.get_access_token", return_value=mock_token):
            mock_async_client = MagicMock()
            mock_async_client.beta.messages.create = AsyncMock(
                side_effect=Exception("API quota exceeded")
            )
            with patch.object(sys.modules["anthropic"], "AsyncAnthropic",
                               return_value=mock_async_client):
                result = await mcp_agent.ask_keka_mcp(
                    "leave balance", "user@caizin.com", "key"
                )

        assert result.startswith("Sorry, I couldn't complete your HR request.")
        assert "API quota exceeded" in result

    @pytest.mark.asyncio
    async def test_empty_text_blocks_returns_fallback_message(self):
        """
        Test name: mcp_no_text_blocks_returns_fallback
        When the Anthropic response has no text blocks (e.g. only tool_use blocks),
        ask_keka_mcp returns the fixed fallback message
        "I couldn't complete your HR request. Please try again or contact HR."
        """
        mock_block = MagicMock()
        mock_block.type = "tool_use"
        del mock_block.text  # no .text attribute

        mock_response = MagicMock()
        mock_response.content = [mock_block]
        mock_response.stop_reason = "tool_use"

        with patch("keka.client.get_access_token", return_value="token"):
            mock_async_client = MagicMock()
            mock_async_client.beta.messages.create = AsyncMock(return_value=mock_response)
            with patch.object(sys.modules["anthropic"], "AsyncAnthropic",
                               return_value=mock_async_client):
                result = await mcp_agent.ask_keka_mcp(
                    "leave balance", "user@caizin.com", "key"
                )

        assert result == "I couldn't complete your HR request. Please try again or contact HR."

    @pytest.mark.asyncio
    async def test_returns_last_text_block_when_multiple_exist(self):
        """
        Test name: mcp_returns_last_text_block
        When multiple text blocks are present in the response,
        ask_keka_mcp returns the LAST text block's content.
        """
        def _text_block(text):
            b = MagicMock()
            b.type = "text"
            b.text = text
            return b

        mock_response = MagicMock()
        mock_response.content = [
            _text_block("First partial answer."),
            _text_block("Final complete answer."),
        ]
        mock_response.stop_reason = "end_turn"

        with patch("keka.client.get_access_token", return_value="token"):
            mock_async_client = MagicMock()
            mock_async_client.beta.messages.create = AsyncMock(return_value=mock_response)
            with patch.object(sys.modules["anthropic"], "AsyncAnthropic",
                               return_value=mock_async_client):
                result = await mcp_agent.ask_keka_mcp(
                    "anything", "user@caizin.com", "key"
                )

        assert result == "Final complete answer."

    @pytest.mark.asyncio
    async def test_system_prompt_contains_employee_email_and_today(self):
        """
        Test name: mcp_system_prompt_contains_email_and_date
        The system prompt passed to the Anthropic API contains the employee email
        and today's date (YYYY-MM-DD format) from the Python date module.
        """
        from datetime import date

        captured = {}

        async def _fake_create(**kwargs):
            captured["system"] = kwargs.get("system", "")
            b = MagicMock()
            b.type = "text"
            b.text = "response"
            resp = MagicMock()
            resp.content = [b]
            resp.stop_reason = "end_turn"
            return resp

        with patch("keka.client.get_access_token", return_value="token"):
            mock_async_client = MagicMock()
            mock_async_client.beta.messages.create = _fake_create
            with patch.object(sys.modules["anthropic"], "AsyncAnthropic",
                               return_value=mock_async_client):
                await mcp_agent.ask_keka_mcp("Q", "alice@caizin.com", "key")

        assert "alice@caizin.com" in captured["system"]
        assert date.today().isoformat() in captured["system"]

    @pytest.mark.asyncio
    async def test_uses_haiku_model_and_mcp_beta(self):
        """
        Test name: mcp_uses_haiku_and_beta_flag
        The Anthropic API call uses model 'claude-haiku-4-5-20251001' and
        betas=['mcp-client-2025-11-20'].
        """
        captured = {}

        async def _fake_create(**kwargs):
            captured.update(kwargs)
            b = MagicMock()
            b.type = "text"
            b.text = "ok"
            resp = MagicMock()
            resp.content = [b]
            resp.stop_reason = "end_turn"
            return resp

        with patch("keka.client.get_access_token", return_value="token"):
            mock_async_client = MagicMock()
            mock_async_client.beta.messages.create = _fake_create
            with patch.object(sys.modules["anthropic"], "AsyncAnthropic",
                               return_value=mock_async_client):
                await mcp_agent.ask_keka_mcp("Q", "user@caizin.com", "key")

        assert captured["model"] == "claude-haiku-4-5-20251001"
        assert "mcp-client-2025-11-20" in captured["betas"]

    @pytest.mark.asyncio
    async def test_mcp_toolset_enables_search_and_execute(self):
        """
        Test name: mcp_toolset_only_search_and_execute_enabled
        The MCP toolset disables all tools by default and enables only
        'search-endpoints' and 'execute-request'.
        """
        toolset = mcp_agent._MCP_TOOLSET
        assert toolset["default_config"]["enabled"] is False
        assert toolset["configs"]["search-endpoints"]["enabled"] is True
        assert toolset["configs"]["execute-request"]["enabled"] is True
        assert "get-endpoint" not in toolset["configs"]
