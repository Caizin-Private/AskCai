"""
Characterization tests for Caizin-HR-Bot/rag.py

Language:  Python 3.11+
Framework: pytest  (pip install pytest pytest-asyncio)
Run:       cd Caizin-HR-Bot && pytest tests/test_rag.py -v

All external calls (Anthropic, Azure Search, Azure OpenAI, boto3) are mocked.
Tests document CURRENT behavior only — not intended behavior.

Setup note: set ANTHROPIC_API_KEY=test in environment before importing rag,
or ensure the mock patches are in place before module-level code runs.
"""

import os
import sys
import types
import importlib
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest


# ---------------------------------------------------------------------------
# Environment stubs — must be set before rag.py is imported so that the
# module-level load_dotenv() / os.getenv() calls resolve correctly.
# ---------------------------------------------------------------------------
os.environ.setdefault("ANTHROPIC_API_KEY",       "test-anthropic-key")
os.environ.setdefault("AZURE_SEARCH_ENDPOINT",   "https://fake.search.windows.net")
os.environ.setdefault("AZURE_SEARCH_KEY",        "fake-search-key")
os.environ.setdefault("AZURE_SEARCH_INDEX",      "fake-index")
os.environ.setdefault("AZURE_OPENAI_API_KEY",    "fake-openai-key")
os.environ.setdefault("AZURE_OPENAI_ENDPOINT",   "https://fake.openai.azure.com")
os.environ.setdefault("AZURE_OPENAI_DEPLOYMENT", "text-embedding-3-small")

# Stub heavy packages that may not be installed
for _pkg in ["azure", "azure.search", "azure.search.documents",
             "azure.search.documents.models", "azure.core",
             "azure.core.credentials", "openai", "anthropic",
             "boto3", "dotenv"]:
    sys.modules.setdefault(_pkg, types.ModuleType(_pkg))

# Provide just enough API surface that rag.py can be imported
sys.modules["azure.search.documents"].SearchClient = MagicMock
sys.modules["azure.search.documents.models"].VectorizedQuery = MagicMock
sys.modules["azure.core.credentials"].AzureKeyCredential = MagicMock
sys.modules["openai"].AzureOpenAI = MagicMock
sys.modules["dotenv"].load_dotenv = lambda: None

# Stub anthropic
_anthropic_mod = sys.modules["anthropic"]
_anthropic_mod.Anthropic = MagicMock
_anthropic_mod.AsyncAnthropic = MagicMock

# boto3 stub needs a 'client' attribute so patch("boto3.client") can target it
sys.modules["boto3"].client = MagicMock(return_value=MagicMock())

# Remove previously-imported rag so we get a fresh import with stubs in place
sys.modules.pop("rag", None)

import rag  # noqa: E402


# ===========================================================================
# _get_anthropic_api_key
# ===========================================================================

class TestGetAnthropicApiKey:
    def test_env_var_takes_priority_over_secrets_manager(self):
        """
        Test name: anthropic_key_env_var_priority
        When ANTHROPIC_API_KEY is set in the environment, it is returned
        immediately — boto3 / Secrets Manager is never called.
        """
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "env-key-123"}):
            with patch("boto3.client") as mock_boto:
                result = rag._get_anthropic_api_key()
        assert result == "env-key-123"
        mock_boto.assert_not_called()

    def test_falls_back_to_secrets_manager_when_env_var_absent(self):
        """
        Test name: anthropic_key_secrets_manager_fallback
        When ANTHROPIC_API_KEY is absent, boto3 Secrets Manager is called
        and the key is extracted from JSON at field 'ANTHROPIC_API_KEY'.
        """
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            mock_client = MagicMock()
            mock_client.get_secret_value.return_value = {
                "SecretString": '{"ANTHROPIC_API_KEY": "secret-key-456"}'
            }
            with patch("boto3.client", return_value=mock_client):
                result = rag._get_anthropic_api_key()
        assert result == "secret-key-456"


# ===========================================================================
# generate_answer
# ===========================================================================

class TestGenerateAnswer:
    def test_empty_context_docs_returns_fixed_fallback(self):
        """
        Test name: generate_answer_empty_context_fallback
        When context_docs is empty, the function short-circuits and returns
        the fixed string "I couldn't find this in the company policy."
        with an empty set — no Anthropic call is made.
        """
        answer, used = rag.generate_answer("Any question?", [], [])
        assert answer == "I couldn't find this in the company policy."
        assert used == set()

    def test_used_policies_are_non_none_chunk_sources(self):
        """
        Test name: generate_answer_used_policies_from_chunk_sources
        used_policies is the set of chunk_sources values that are not None.
        """
        mock_response = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = "Answer text."
        mock_response.content = [mock_block]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        rag._anthropic_client = mock_client

        _, used = rag.generate_answer(
            "Question?",
            ["doc1 content"],
            ["PolicyA", None, "PolicyB", "PolicyA"],
        )
        assert used == {"PolicyA", "PolicyB"}

    def test_answer_text_extracted_from_first_text_block(self):
        """
        Test name: generate_answer_extracts_text_block
        The answer is taken from the first content block whose type is 'text'.
        """
        mock_block_text = MagicMock()
        mock_block_text.type = "text"
        mock_block_text.text = "Here is the policy answer."

        mock_block_non_text = MagicMock()
        mock_block_non_text.type = "tool_use"

        mock_response = MagicMock()
        mock_response.content = [mock_block_non_text, mock_block_text]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        rag._anthropic_client = mock_client

        answer, _ = rag.generate_answer("Q?", ["context doc"], ["PolicyX"])
        assert answer == "Here is the policy answer."

    def test_uses_claude_haiku_model(self):
        """
        Test name: generate_answer_uses_haiku_model
        generate_answer calls the Anthropic client with model = 'claude-haiku-4-5'.
        """
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = "ok"
        mock_response = MagicMock()
        mock_response.content = [mock_block]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        rag._anthropic_client = mock_client

        rag.generate_answer("Q?", ["some doc"], ["PolicyX"])
        call_kwargs = mock_client.messages.create.call_args
        assert call_kwargs.kwargs.get("model") == "claude-haiku-4-5"


# ===========================================================================
# _classify_intent — fallback behavior
# ===========================================================================

class TestClassifyIntent:
    def test_returns_rag_on_any_exception(self):
        """
        Test name: classify_intent_fallback_on_exception
        When the Anthropic call raises any exception, _classify_intent
        returns 'rag' instead of propagating the error.
        """
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API error")
        rag._anthropic_client = mock_client

        result = rag._classify_intent("What is the travel policy?")
        assert result == "rag"

    def test_returns_rag_for_unrecognised_model_output(self):
        """
        Test name: classify_intent_unknown_label_falls_back_to_rag
        If the model returns a string that is not one of the four recognised
        labels, the function falls back to 'rag'.
        """
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = "unknown_label"
        mock_response = MagicMock()
        mock_response.content = [mock_block]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        rag._anthropic_client = mock_client

        result = rag._classify_intent("something")
        assert result == "rag"

    @pytest.mark.parametrize("label", ["greeting", "list_policies", "hr_action", "rag"])
    def test_passes_through_valid_labels(self, label):
        """
        Test name: classify_intent_valid_label_passthrough[{label}]
        Each of the four valid labels is returned unchanged (case-insensitive strip applied).
        """
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = f"  {label.upper()}  "
        mock_response = MagicMock()
        mock_response.content = [mock_block]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        rag._anthropic_client = mock_client

        result = rag._classify_intent("any question")
        assert result == label

    def test_uses_max_tokens_20_temperature_0(self):
        """
        Test name: classify_intent_generation_params
        The intent classifier uses max_tokens=20 and temperature=0 to minimise
        token waste and maximise determinism.
        """
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = "rag"
        mock_response = MagicMock()
        mock_response.content = [mock_block]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        rag._anthropic_client = mock_client

        rag._classify_intent("anything")
        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs["max_tokens"] == 20
        assert kwargs["temperature"] == 0


# ===========================================================================
# ask_policy_question — synchronous routing and Phase-1 quirks
# ===========================================================================

class TestAskPolicyQuestion:
    """
    Tests for the main router.
    All Anthropic / Azure calls are mocked.
    """

    @pytest.fixture(autouse=True)
    def _reset_clients(self):
        """Ensure module-level client singletons don't bleed between tests."""
        old = rag._anthropic_client
        rag._anthropic_client = None
        yield
        rag._anthropic_client = old

    @pytest.mark.asyncio
    async def test_greeting_intent_returns_fixed_response(self):
        """
        Test name: ask_policy_greeting_fixed_response
        When intent is 'greeting', the router returns the fixed greeting string
        without touching RAG or Keka.
        """
        with patch("rag._classify_intent", return_value="greeting"):
            result = await rag.ask_policy_question("hi")
        assert "Good day" in result or "How can I help" in result

    @pytest.mark.asyncio
    async def test_out_of_scope_answer_has_no_disclaimer_or_sources(self):
        """
        Test name: ask_policy_out_of_scope_no_footer
        When the RAG answer contains a recognised out-of-scope phrase,
        no source link and no disclaimer footer are appended.
        """
        with patch("rag._classify_intent", return_value="rag"), \
             patch("rag.search_documents", return_value=(
                 ["some doc"], {"PolicyA": "http://policy.url"}, ["PolicyA"]
             )), \
             patch("rag.generate_answer", return_value=(
                 "I couldn't find this in the company policy.", {"PolicyA"}
             )):
            result = await rag.ask_policy_question("unknown topic")

        assert "📎" not in result
        assert "For final confirmation" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("phrase", [
        "does not contain any information",
        "cannot answer this question",
        "not found in the company policy",
        "no information about this",
        "outside the scope of",
        "not available in the provided",
        "context does not provide",
        "provided context does not",
        "i couldn't find this in the company policy",
    ])
    async def test_all_out_of_scope_phrases_suppress_footer(self, phrase):
        """
        Test name: ask_policy_each_out_of_scope_phrase_suppresses_footer[{phrase}]
        Each of the nine out-of-scope marker phrases suppresses the source link
        and disclaimer footer.
        """
        answer_with_phrase = f"Unfortunately, the {phrase} provided."
        with patch("rag._classify_intent", return_value="rag"), \
             patch("rag.search_documents", return_value=(
                 ["doc"], {"P": "http://p.url"}, ["P"]
             )), \
             patch("rag.generate_answer", return_value=(answer_with_phrase, {"P"})):
            result = await rag.ask_policy_question("something")

        assert "For final confirmation" not in result
        assert "📎" not in result

    @pytest.mark.asyncio
    async def test_policy_answer_appends_source_and_disclaimer(self):
        """
        Test name: ask_policy_in_scope_appends_footer
        When the answer does NOT contain any out-of-scope phrase AND relevant
        sources exist, both the source link and disclaimer are appended.
        """
        with patch("rag._classify_intent", return_value="rag"), \
             patch("rag.search_documents", return_value=(
                 ["policy text"], {"Leave Policy": "http://leave.url"}, ["Leave Policy"]
             )), \
             patch("rag.generate_answer", return_value=(
                 "You are entitled to 12 days of casual leave.", {"Leave Policy"}
             )):
            result = await rag.ask_policy_question("how many casual leave days?")

        assert "📎" in result
        assert "For final confirmation" in result
        assert "Leave Policy" in result

    @pytest.mark.asyncio
    async def test_only_used_policies_appear_in_source_link(self):
        """
        Test name: ask_policy_sources_filtered_to_used_policies
        The footer only shows links for policies whose names appear in used_policies;
        unreferenced policies from all_sources are excluded.
        """
        with patch("rag._classify_intent", return_value="rag"), \
             patch("rag.search_documents", return_value=(
                 ["doc"],
                 {"Leave Policy": "http://leave.url", "Travel Policy": "http://travel.url"},
                 ["Leave Policy"],
             )), \
             patch("rag.generate_answer", return_value=(
                 "Answer about leave.", {"Leave Policy"}
             )):
            result = await rag.ask_policy_question("leave?")

        assert "Leave Policy" in result
        assert "Travel Policy" not in result

    @pytest.mark.asyncio
    async def test_only_first_relevant_source_appears(self):
        """
        Test name: ask_policy_only_first_source_in_footer
        The current implementation only appends the FIRST entry from
        relevant_sources, not all of them.
        """
        with patch("rag._classify_intent", return_value="rag"), \
             patch("rag.search_documents", return_value=(
                 ["doc"],
                 {"PolicyA": "http://a.url", "PolicyB": "http://b.url"},
                 ["PolicyA", "PolicyB"],
             )), \
             patch("rag.generate_answer", return_value=(
                 "Answer mentioning both.", {"PolicyA", "PolicyB"}
             )):
            result = await rag.ask_policy_question("something?")

        # Only the first one from relevant_sources dict is shown
        source_lines = [ln for ln in result.split("\n") if "http://" in ln]
        assert len(source_lines) == 1


# ===========================================================================
# extract_leave_request — characterization tests
# Locks down current behavior so any future refactor is caught immediately.
# All tests mock rag._anthropic_client — no real Anthropic calls made.
# ===========================================================================

def _make_llm_response(text: str):
    """Return a fake Anthropic response whose first text block contains `text`."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    client = MagicMock()
    client.messages.create.return_value = response
    return client


class TestExtractLeaveRequestFastPath:
    """Balance phrases must return {"action": "check_balance"} with zero LLM calls."""

    @pytest.mark.parametrize("text", [
        "balance",
        "Balance",
        "BALANCE",
        "leave balance",
        "my balance",
        "check balance",
        "view balance",
        "show balance",
        "  balance  ",
        "leave balance please",
    ])
    def test_balance_phrase_returns_check_balance_without_llm(self, text):
        mock_client = MagicMock()
        rag._anthropic_client = mock_client

        result = rag.extract_leave_request(text, "2026-07-22")

        assert result == {"action": "check_balance"}
        mock_client.messages.create.assert_not_called()

    def test_non_standalone_balance_word_hits_llm(self):
        """'unbalanced' contains 'balance' but must NOT fast-path."""
        rag._anthropic_client = _make_llm_response('{"action": null}')

        result = rag.extract_leave_request("I feel unbalanced today", "2026-07-22")

        rag._anthropic_client.messages.create.assert_called_once()
        assert result is None


class TestExtractLeaveRequestApplyLeave:

    def test_apply_leave_full_dict_returned(self):
        import json
        payload = {
            "action": "apply_leave",
            "from_date": "2026-07-25",
            "to_date": "2026-07-25",
            "session_type": "full_day",
            "reason": "",
            "leave_type_hint": "sick",
        }
        rag._anthropic_client = _make_llm_response(json.dumps(payload))

        result = rag.extract_leave_request("I want sick leave tomorrow", "2026-07-22")

        assert result["action"] == "apply_leave"
        assert result["from_date"] == "2026-07-25"
        assert result["leave_type_hint"] == "sick"

    def test_null_dates_preserved(self):
        import json
        payload = {"action": "apply_leave", "from_date": None,
                   "to_date": None, "session_type": "full_day", "reason": "", "leave_type_hint": ""}
        rag._anthropic_client = _make_llm_response(json.dumps(payload))

        result = rag.extract_leave_request("I need a day off", "2026-07-22")

        assert result["from_date"] is None
        assert result["to_date"] is None

    def test_markdown_code_fence_stripped(self):
        import json
        payload = {"action": "apply_leave", "from_date": "2026-07-28",
                   "to_date": "2026-07-28", "session_type": "full_day", "reason": "", "leave_type_hint": ""}
        rag._anthropic_client = _make_llm_response(f"```json\n{json.dumps(payload)}\n```")

        result = rag.extract_leave_request("take leave on Monday", "2026-07-22")

        assert result is not None
        assert result["action"] == "apply_leave"

    def test_leave_type_names_injected_into_prompt(self):
        rag._anthropic_client = _make_llm_response('{"action": null}')

        rag.extract_leave_request(
            "take earned leave next week", "2026-07-22",
            leave_type_names=["Sick Leave", "Earned Leave", "Casual Leave"],
        )

        prompt = rag._anthropic_client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "Earned Leave" in prompt
        assert "Casual Leave" in prompt

    def test_today_date_injected_into_prompt(self):
        rag._anthropic_client = _make_llm_response('{"action": null}')

        rag.extract_leave_request("take leave next Monday", "2026-07-22")

        prompt = rag._anthropic_client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "2026-07-22" in prompt


class TestExtractLeaveRequestCancelLeave:

    def test_cancel_leave_returned(self):
        rag._anthropic_client = _make_llm_response('{"action": "cancel_leave"}')

        result = rag.extract_leave_request("cancel my leave request", "2026-07-22")

        assert result == {"action": "cancel_leave"}


class TestExtractLeaveRequestNullAction:

    def test_null_action_returns_none(self):
        rag._anthropic_client = _make_llm_response('{"action": null}')

        result = rag.extract_leave_request("hello how are you", "2026-07-22")

        assert result is None


class TestExtractLeaveRequestErrorHandling:

    def test_llm_exception_returns_none(self):
        """A network or API error must never crash the bot — always returns None."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("network error")
        rag._anthropic_client = mock_client

        result = rag.extract_leave_request("apply leave tomorrow", "2026-07-22")

        assert result is None

    def test_invalid_json_returns_none(self):
        rag._anthropic_client = _make_llm_response("Sorry, I cannot help.")

        result = rag.extract_leave_request("take leave", "2026-07-22")

        assert result is None

    def test_empty_llm_response_returns_none(self):
        rag._anthropic_client = _make_llm_response("")

        result = rag.extract_leave_request("apply leave", "2026-07-22")

        assert result is None


class TestExtractLeaveRequestLlmParams:

    def test_uses_temperature_0(self):
        rag._anthropic_client = _make_llm_response('{"action": null}')
        rag.extract_leave_request("take leave", "2026-07-22")
        kwargs = rag._anthropic_client.messages.create.call_args.kwargs
        assert kwargs["temperature"] == 0

    def test_uses_claude_haiku_model(self):
        rag._anthropic_client = _make_llm_response('{"action": null}')
        rag.extract_leave_request("take leave", "2026-07-22")
        kwargs = rag._anthropic_client.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-haiku-4-5"
