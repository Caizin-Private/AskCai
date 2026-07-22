"""
Tests for main.py — _build_work_status_card

Pure function tests: no mocking needed, no network, no DB.
These tests will FAIL until _build_work_status_card is added to main.py.

Run:  cd Caizin-HR-Bot && pytest tests/test_main.py -v
"""

import os
import sys
import types
from unittest.mock import MagicMock
import pytest

# ---------------------------------------------------------------------------
# Stub every heavy dependency main.py imports at module level so we can
# import it without a running FastAPI server, Teams adapter, or DB connection.
# ---------------------------------------------------------------------------
os.environ.setdefault("ANTHROPIC_API_KEY",    "test-key")
os.environ.setdefault("MicrosoftAppId",       "fake-app-id")
os.environ.setdefault("MicrosoftAppPassword", "fake-password")

for _pkg in [
    "anthropic", "boto3", "dotenv", "psycopg2", "psycopg2.extras",
    "azure", "azure.search", "azure.search.documents",
    "azure.search.documents.models", "azure.core", "azure.core.credentials",
    "openai",
    "botbuilder", "botbuilder.core", "botbuilder.core.teams",
    "botbuilder.schema",
    "fastapi", "fastapi.responses", "fastapi.staticfiles",
]:
    sys.modules.setdefault(_pkg, types.ModuleType(_pkg))

# Provide just enough surface for the imports in main.py to resolve
sys.modules["anthropic"].Anthropic = MagicMock
sys.modules["boto3"].client = MagicMock(return_value=MagicMock())
sys.modules["dotenv"].load_dotenv = lambda: None
sys.modules["psycopg2"].connect = MagicMock()
sys.modules["psycopg2.extras"].RealDictCursor = MagicMock
sys.modules["azure.search.documents"].SearchClient = MagicMock
sys.modules["azure.search.documents.models"].VectorizedQuery = MagicMock
sys.modules["azure.core.credentials"].AzureKeyCredential = MagicMock
sys.modules["openai"].AzureOpenAI = MagicMock

_bc = sys.modules["botbuilder.core"]
_bc.BotFrameworkAdapter = MagicMock
_bc.BotFrameworkAdapterSettings = MagicMock
_bc.CardFactory = MagicMock
_bc.MessageFactory = MagicMock
_bc.TurnContext = object
sys.modules["botbuilder.core.teams"].TeamsInfo = MagicMock
sys.modules["botbuilder.schema"].Activity = MagicMock
sys.modules["botbuilder.schema"].InvokeResponse = MagicMock

_fa = sys.modules["fastapi"]
_fa.FastAPI = MagicMock
_fa.Request = MagicMock
_fa.Response = MagicMock
sys.modules["fastapi.responses"].FileResponse = MagicMock
sys.modules["fastapi.staticfiles"].StaticFiles = MagicMock

# Stub sub-modules main.py imports directly
for _sub in ["rag", "chat_intents", "features", "insync_db",
             "keka", "keka.leave_service", "keka.models"]:
    sys.modules.setdefault(_sub, types.ModuleType(_sub))

sys.modules["rag"].ask_policy_question = MagicMock()
sys.modules["rag"]._classify_intent = MagicMock()
sys.modules["rag"].extract_leave_request = MagicMock()
sys.modules["chat_intents"].extract_work_location_query = MagicMock()
sys.modules["features"].surface_enabled = MagicMock(return_value=True)
sys.modules["features"].COMMAND_FLAGS = {}
sys.modules["insync_db"].get_today_all_records = MagicMock()
sys.modules["insync_db"].get_latest_records = MagicMock()
sys.modules["insync_db"].record_attendance_response = MagicMock()
sys.modules["insync_db"].get_work_status_by_email = MagicMock()
sys.modules["insync_db"].get_work_status_by_name = MagicMock()
_ks = sys.modules["keka.leave_service"]
_ks.leave_service = MagicMock()
sys.modules["keka.models"].SessionType = MagicMock

sys.modules.pop("main", None)
import main  # noqa: E402


# ===========================================================================
# _build_work_status_card
# ===========================================================================

class TestBuildWorkStatusCard:

    def test_single_result_contains_name_and_bucket(self):
        results = [{"name": "Priya Sharma", "bucket": "WFH"}]

        card = main._build_work_status_card(results)

        body_text = str(card)
        assert "Priya Sharma" in body_text
        assert "WFH" in body_text

    def test_single_result_textblock_color_matches_bucket(self):
        """Each bucket maps to the correct Adaptive Card color from _BUCKET_COLOR."""
        cases = [
            ("Office",          "Good"),
            ("WFH",             "Accent"),
            ("Leave",           "Warning"),
            ("Client Location", "Good"),
            ("Floater Holiday", "Warning"),
            ("Absent",          "Attention"),
            ("Pending",         "Default"),
        ]
        for bucket, expected_color in cases:
            card = main._build_work_status_card([{"name": "Test User", "bucket": bucket}])
            body_text = str(card)
            assert expected_color in body_text, f"Expected color {expected_color} for bucket {bucket}"

    def test_single_result_returns_adaptive_card_schema(self):
        card = main._build_work_status_card([{"name": "Rohan Lande", "bucket": "Office"}])

        assert card["type"] == "AdaptiveCard"
        assert "$schema" in card
        assert "body" in card

    def test_multiple_results_returns_factset(self):
        results = [
            {"name": "Priya Sharma", "bucket": "WFH"},
            {"name": "Rohan Lande",  "bucket": "Office"},
            {"name": "Yash Nikam",   "bucket": "Leave"},
        ]

        card = main._build_work_status_card(results)

        body_text = str(card)
        assert "Priya Sharma" in body_text
        assert "Rohan Lande" in body_text
        assert "Yash Nikam" in body_text

    def test_multiple_results_uses_factset_type(self):
        results = [
            {"name": "A", "bucket": "Office"},
            {"name": "B", "bucket": "WFH"},
        ]

        card = main._build_work_status_card(results)

        fact_set_blocks = [b for b in card["body"] if b.get("type") == "FactSet"]
        assert len(fact_set_blocks) == 1

    def test_empty_results_returns_fallback_card(self):
        card = main._build_work_status_card([])

        assert card["type"] == "AdaptiveCard"
        body_text = str(card)
        assert "No" in body_text or "not found" in body_text.lower() or "record" in body_text.lower()

    def test_multiple_results_caps_at_eight(self):
        """FactSet should not overflow — max 8 entries shown."""
        results = [{"name": f"Person {i}", "bucket": "Office"} for i in range(12)]

        card = main._build_work_status_card(results)

        fact_set = next(b for b in card["body"] if b.get("type") == "FactSet")
        assert len(fact_set["facts"]) <= 8