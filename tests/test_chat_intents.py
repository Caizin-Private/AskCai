"""
Tests for chat_intents.py — extract_work_location_query

These tests will FAIL until chat_intents.py is created (that is intentional —
they define the expected behavior before the implementation exists).

Run:  cd Caizin-HR-Bot && pytest tests/test_chat_intents.py -v

All Anthropic calls are mocked — no network access required.
"""

import os
import sys
import json
import types
from unittest.mock import MagicMock
import pytest

# ---------------------------------------------------------------------------
# Stub heavy packages before chat_intents is imported
# ---------------------------------------------------------------------------
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

for _pkg in ["anthropic", "boto3", "dotenv"]:
    sys.modules.setdefault(_pkg, types.ModuleType(_pkg))

sys.modules["anthropic"].Anthropic = MagicMock
sys.modules["boto3"].client = MagicMock(return_value=MagicMock())
sys.modules["dotenv"].load_dotenv = lambda: None

sys.modules.pop("chat_intents", None)
import chat_intents  # noqa: E402


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _make_llm_response(text: str):
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    client = MagicMock()
    client.messages.create.return_value = response
    return client


# ===========================================================================
# extract_work_location_query — keyword guard
# ===========================================================================

class TestExtractWorkLocationQueryKeywordGuard:
    """Messages without a work-location keyword must return None without an LLM call."""

    @pytest.mark.parametrize("text", [
        "hello",
        "apply leave tomorrow",
        "what is the leave policy",
        "check my balance",
        "good morning",
        "",
    ])
    def test_no_keyword_returns_none_without_llm(self, text):
        mock_client = MagicMock()
        chat_intents._anthropic_client = mock_client

        result = chat_intents.extract_work_location_query(text)

        assert result is None
        mock_client.messages.create.assert_not_called()

    @pytest.mark.parametrize("text", [
        "what is work location of Priya",
        "where is Rohan working today",
        "what is my work status today",
        "is Yash in office today",
        "is Sarah wfh today",
        "where am i working",
        "is anyone working remotely",
        "working from home today?",
        "work from home today anyone?",
    ])
    def test_keyword_present_triggers_llm(self, text):
        chat_intents._anthropic_client = _make_llm_response('{"target": null}')

        chat_intents.extract_work_location_query(text)

        chat_intents._anthropic_client.messages.create.assert_called_once()


# ===========================================================================
# extract_work_location_query — LLM response parsing
# ===========================================================================

class TestExtractWorkLocationQueryParsing:

    def test_self_target_returned(self):
        chat_intents._anthropic_client = _make_llm_response('{"target": "self"}')

        result = chat_intents.extract_work_location_query("what is my work status today")

        assert result == {"target": "self"}

    def test_other_target_with_name_returned(self):
        payload = {"target": "other", "name": "Priya Sharma"}
        chat_intents._anthropic_client = _make_llm_response(json.dumps(payload))

        result = chat_intents.extract_work_location_query("where is Priya Sharma working today")

        assert result["target"] == "other"
        assert result["name"] == "Priya Sharma"

    def test_null_target_returns_none(self):
        chat_intents._anthropic_client = _make_llm_response('{"target": null}')

        result = chat_intents.extract_work_location_query("where is the office located")

        assert result is None

    def test_markdown_code_fence_stripped(self):
        payload = {"target": "other", "name": "Rohan"}
        fenced = f"```json\n{json.dumps(payload)}\n```"
        chat_intents._anthropic_client = _make_llm_response(fenced)

        result = chat_intents.extract_work_location_query("where is Rohan working today")

        assert result is not None
        assert result["target"] == "other"
        assert result["name"] == "Rohan"


# ===========================================================================
# extract_work_location_query — error handling
# ===========================================================================

class TestExtractWorkLocationQueryErrorHandling:

    def test_llm_exception_returns_none(self):
        """A network or API error must never crash the bot."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("network error")
        chat_intents._anthropic_client = mock_client

        result = chat_intents.extract_work_location_query("what is my work status today")

        assert result is None

    def test_invalid_json_returns_none(self):
        chat_intents._anthropic_client = _make_llm_response("I cannot help with that.")

        result = chat_intents.extract_work_location_query("where is Yash working today")

        assert result is None

    def test_empty_llm_response_returns_none(self):
        chat_intents._anthropic_client = _make_llm_response("")

        result = chat_intents.extract_work_location_query("work status today")

        assert result is None


# ===========================================================================
# extract_work_location_query — LLM call parameters
# ===========================================================================

class TestExtractWorkLocationQueryLlmParams:

    def test_uses_temperature_0(self):
        chat_intents._anthropic_client = _make_llm_response('{"target": null}')
        chat_intents.extract_work_location_query("work status today")
        kwargs = chat_intents._anthropic_client.messages.create.call_args.kwargs
        assert kwargs["temperature"] == 0

    def test_uses_claude_haiku_model(self):
        chat_intents._anthropic_client = _make_llm_response('{"target": null}')
        chat_intents.extract_work_location_query("work status today")
        kwargs = chat_intents._anthropic_client.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-haiku-4-5"