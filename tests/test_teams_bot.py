"""
Characterization tests for Caizin-HR-Bot/teams_bot.py

Language:  Python 3.11+
Framework: pytest  (pip install pytest)
Run:       cd Caizin-HR-Bot && pytest tests/test_teams_bot.py -v

These tests document the CURRENT behavior of the module.
They do not assert intended behavior — only observed behavior at the time of writing.

External dependencies (rag, botbuilder) are mocked at import time so no
network or Azure credentials are required to run these tests.
"""

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Isolate the import: stub out 'rag' before teams_bot is imported
# so that from rag import ask_policy_question doesn't hit the network.
# ---------------------------------------------------------------------------
_fake_rag = types.ModuleType("rag")
_fake_rag.ask_policy_question = AsyncMock(return_value="mock answer")
sys.modules.setdefault("rag", _fake_rag)

# Stub botbuilder packages (not installed in test env by default)
for _mod in [
    "botbuilder", "botbuilder.core", "botbuilder.schema",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

# Provide the symbols teams_bot uses from botbuilder
_bc = sys.modules["botbuilder.core"]
_bc.TurnContext = object

_bs = sys.modules["botbuilder.schema"]
_bs.Activity = MagicMock
_bs.ActivityTypes = MagicMock()
_bs.ActivityTypes.typing = "typing"
_bs.Attachment = MagicMock
_bs.HeroCard = MagicMock
_bs.CardAction = MagicMock
_bs.ActionTypes = MagicMock()
_bs.ActionTypes.im_back = "imBack"

from teams_bot import (  # noqa: E402  (imports must come after stubs)
    APPLY_LEAVE_TRIGGER,
    _get_employee_email,
    build_attendance_ack_card,
    build_dummy_attendance_card,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _make_from_prop(name: str = "", user_id: str = "", aad_object_id: str = ""):
    fp = MagicMock()
    fp.name = name
    fp.id = user_id
    # aad_object_id is accessed via getattr — set as attribute
    fp.aad_object_id = aad_object_id
    return fp


def _make_turn_context(from_prop=None, channel_data=None):
    tc = MagicMock()
    tc.activity.from_property = from_prop
    tc.activity.channel_data = channel_data or {}
    return tc


# ===========================================================================
# _get_employee_email
# ===========================================================================

class TestGetEmployeeEmail:
    """Documents the email-resolution priority chain."""

    def test_email_in_name_field(self):
        """
        Test name: email_resolved_from_name
        When from_property.name contains '@', the name is returned lowercased and stripped.
        """
        tc = _make_turn_context(_make_from_prop(name="Alice.Smith@Caizin.COM"))
        result = _get_employee_email(tc)
        assert result == "alice.smith@caizin.com"

    def test_email_in_id_field_when_name_has_no_at(self):
        """
        Test name: email_resolved_from_id
        Falls through to from_property.id when name has no '@'.
        """
        tc = _make_turn_context(_make_from_prop(name="Alice Smith", user_id="alice@caizin.com"))
        result = _get_employee_email(tc)
        assert result == "alice@caizin.com"

    def test_email_constructed_from_two_word_name(self):
        """
        Test name: email_constructed_from_fullname
        When neither name nor id contains '@', email is built as
        'firstname.lastname@caizin.com' from a two-word display name.
        """
        tc = _make_turn_context(_make_from_prop(name="Alice Smith", user_id="no-at-sign"))
        result = _get_employee_email(tc)
        assert result == "alice.smith@caizin.com"

    def test_email_constructed_from_single_word_name(self):
        """
        Test name: email_constructed_from_single_name
        Single-word display name → 'word@caizin.com' (not firstname.lastname format).
        """
        tc = _make_turn_context(_make_from_prop(name="Alice", user_id="no-at-sign"))
        result = _get_employee_email(tc)
        assert result == "alice@caizin.com"

    def test_empty_string_when_from_property_is_none(self):
        """
        Test name: email_empty_when_from_property_none
        Returns empty string when from_property is None — no exception raised.
        """
        tc = _make_turn_context(from_prop=None)
        result = _get_employee_email(tc)
        assert result == ""

    def test_empty_string_when_name_and_id_both_empty(self):
        """
        Test name: email_empty_when_no_name_no_id
        When name and id are both empty strings (no '@' in either), returns ''.
        """
        tc = _make_turn_context(_make_from_prop(name="", user_id=""))
        result = _get_employee_email(tc)
        assert result == ""

    def test_name_takes_priority_over_id_email(self):
        """
        Test name: name_field_has_priority_over_id
        When name contains '@', the id field is never consulted —
        even if it contains a different email.
        """
        tc = _make_turn_context(
            _make_from_prop(name="alice@caizin.com", user_id="other@caizin.com")
        )
        result = _get_employee_email(tc)
        assert result == "alice@caizin.com"

    def test_name_with_more_than_two_words_uses_first_and_last(self):
        """
        Test name: email_uses_first_and_last_word
        For a three-word name the email is 'first.last@caizin.com',
        middle word is dropped.
        """
        tc = _make_turn_context(_make_from_prop(name="Alice B Smith", user_id="no-at"))
        result = _get_employee_email(tc)
        assert result == "alice.smith@caizin.com"


# ===========================================================================
# APPLY_LEAVE_TRIGGER sentinel
# ===========================================================================

class TestApplyLeaveTrigger:
    def test_sentinel_value(self):
        """
        Test name: apply_leave_trigger_sentinel
        The sentinel string that triggers the leave form must remain stable;
        main.py does an exact string comparison against it.
        """
        assert APPLY_LEAVE_TRIGGER == "__open_apply_leave_form__"


# ===========================================================================
# build_dummy_attendance_card
# ===========================================================================

class TestBuildDummyAttendanceCard:
    """Documents the structure of the Phase-1 dummy attendance card."""

    def test_returns_dict_with_adaptive_card_type(self):
        """
        Test name: dummy_card_is_adaptive_card
        Top-level type must be 'AdaptiveCard'.
        """
        card = build_dummy_attendance_card()
        assert card["type"] == "AdaptiveCard"

    def test_version_is_1_4(self):
        """
        Test name: dummy_card_version_1_4
        Card version is '1.4' — Teams requires this for Action.Execute support.
        """
        card = build_dummy_attendance_card()
        assert card["version"] == "1.4"

    def test_default_name_is_there(self):
        """
        Test name: dummy_card_default_greeting_says_there
        With no arguments, the greeting reads 'Hey there, where are you...'
        """
        card = build_dummy_attendance_card()
        body_text = card["body"][0]["text"]
        assert "Hey there," in body_text

    def test_custom_name_appears_in_body(self):
        """
        Test name: dummy_card_custom_name_in_greeting
        Passing name='Alice' produces 'Hey Alice, where are you...'
        """
        card = build_dummy_attendance_card(name="Alice")
        body_text = card["body"][0]["text"]
        assert "Hey Alice," in body_text

    def test_exactly_four_actions(self):
        """
        Test name: dummy_card_four_actions
        The card has exactly 4 Action.Execute buttons.
        """
        card = build_dummy_attendance_card()
        assert len(card["actions"]) == 4

    def test_action_statuses_are_correct(self):
        """
        Test name: dummy_card_action_statuses
        The four button data.status values are exactly:
        office, wfh, leave, client_site — in that order.
        """
        card = build_dummy_attendance_card()
        statuses = [a["data"]["status"] for a in card["actions"]]
        assert statuses == ["office", "wfh", "leave", "client_site"]

    def test_all_actions_use_action_execute_verb(self):
        """
        Test name: dummy_card_actions_use_execute
        Every action has type 'Action.Execute' and verb 'dummy_attendance'.
        """
        card = build_dummy_attendance_card()
        for action in card["actions"]:
            assert action["type"] == "Action.Execute"
            assert action["verb"] == "dummy_attendance"

    def test_schema_url_present(self):
        """
        Test name: dummy_card_has_schema
        The $schema field points to the Adaptive Cards schema URL.
        """
        card = build_dummy_attendance_card()
        assert "adaptivecards.io" in card["$schema"]


# ===========================================================================
# build_attendance_ack_card
# ===========================================================================

class TestBuildAttendanceAckCard:
    """Documents the label mapping and structure of the acknowledgement card."""

    @pytest.mark.parametrize("status,expected_label", [
        ("office",      "In Office"),
        ("wfh",         "WFH"),
        ("leave",       "On Leave"),
        ("client_site", "Client Site"),
    ])
    def test_known_status_label(self, status, expected_label):
        """
        Test name: ack_card_known_status_label[{status}]
        Known status values are mapped to their human-readable display labels.
        """
        card = build_attendance_ack_card(status)
        body_text = card["body"][0]["text"]
        assert expected_label in body_text

    def test_unknown_status_title_cased_fallback(self):
        """
        Test name: ack_card_unknown_status_fallback
        An unknown status is title-cased after replacing underscores with spaces.
        e.g. 'some_status' → 'Some Status'.
        """
        card = build_attendance_ack_card("some_status")
        body_text = card["body"][0]["text"]
        assert "Some Status" in body_text

    def test_unknown_simple_status_fallback(self):
        """
        Test name: ack_card_unknown_simple_fallback
        A plain unknown word like 'unknown' → 'Unknown'.
        """
        card = build_attendance_ack_card("unknown")
        body_text = card["body"][0]["text"]
        assert "Unknown" in body_text

    def test_returns_adaptive_card_type(self):
        """
        Test name: ack_card_is_adaptive_card
        Top-level type is 'AdaptiveCard'.
        """
        card = build_attendance_ack_card("office")
        assert card["type"] == "AdaptiveCard"

    def test_first_text_block_color_is_good(self):
        """
        Test name: ack_card_color_good
        The primary text block uses color 'Good' (green in Teams).
        """
        card = build_attendance_ack_card("office")
        assert card["body"][0]["color"] == "Good"

    def test_body_has_test_only_disclaimer(self):
        """
        Test name: ack_card_has_test_disclaimer
        The second text block contains the test-only disclaimer note.
        """
        card = build_attendance_ack_card("office")
        assert len(card["body"]) >= 2
        disclaimer = card["body"][1]["text"]
        assert "Test only" in disclaimer or "test" in disclaimer.lower()
