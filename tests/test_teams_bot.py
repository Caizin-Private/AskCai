"""
Tests for Caizin-HR-Bot/teams_bot.py

Run: cd Caizin-HR-Bot && pytest tests/test_teams_bot.py -v
"""

import sys
import types
from unittest.mock import MagicMock

# Stub botbuilder packages
for _mod in ["botbuilder", "botbuilder.core", "botbuilder.schema"]:
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

sys.modules["botbuilder.core"].TurnContext = object

from teams_bot import _get_employee_email  # noqa: E402


def _make_from_prop(name: str = "", user_id: str = ""):
    fp = MagicMock()
    fp.name = name
    fp.id = user_id
    return fp


def _make_turn_context(from_prop=None):
    tc = MagicMock()
    tc.activity.from_property = from_prop
    tc.activity.channel_data = {}
    return tc


class TestGetEmployeeEmail:
    def test_email_in_name_field(self):
        tc = _make_turn_context(_make_from_prop(name="Alice.Smith@Caizin.COM"))
        assert _get_employee_email(tc) == "alice.smith@caizin.com"

    def test_email_in_id_field_when_name_has_no_at(self):
        tc = _make_turn_context(_make_from_prop(name="Alice Smith", user_id="alice@caizin.com"))
        assert _get_employee_email(tc) == "alice@caizin.com"

    def test_email_constructed_from_two_word_name(self):
        tc = _make_turn_context(_make_from_prop(name="Alice Smith", user_id="no-at-sign"))
        assert _get_employee_email(tc) == "alice.smith@caizin.com"

    def test_email_constructed_from_single_word_name(self):
        tc = _make_turn_context(_make_from_prop(name="Alice", user_id="no-at-sign"))
        assert _get_employee_email(tc) == "alice@caizin.com"

    def test_empty_string_when_from_property_is_none(self):
        tc = _make_turn_context(from_prop=None)
        assert _get_employee_email(tc) == ""

    def test_empty_string_when_name_and_id_both_empty(self):
        tc = _make_turn_context(_make_from_prop(name="", user_id=""))
        assert _get_employee_email(tc) == ""

    def test_name_takes_priority_over_id_email(self):
        tc = _make_turn_context(_make_from_prop(name="alice@caizin.com", user_id="other@caizin.com"))
        assert _get_employee_email(tc) == "alice@caizin.com"

    def test_name_with_more_than_two_words_uses_first_and_last(self):
        tc = _make_turn_context(_make_from_prop(name="Alice B Smith", user_id="no-at"))
        assert _get_employee_email(tc) == "alice.smith@caizin.com"
