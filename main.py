import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from botbuilder.core import (
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
    CardFactory,
    MessageFactory,
)
from botbuilder.core.teams import TeamsInfo
from botbuilder.schema import Activity, InvokeResponse

from features import surface_enabled, COMMAND_FLAGS
from rag import ask_policy_question, _classify_intent, extract_leave_request
from chat_intents import extract_work_location_query
from keka.leave_service import leave_service
from keka.models import SessionType
from insync_db import (
    get_today_all_records,
    get_latest_records,
    record_attendance_response,
    get_work_status_by_email,
    get_work_status_by_name,
)
import timesheet_attendance
import timesheet_mock
from keka import client as keka_client
from keka import timesheet_service
from keka.dao._http import KekaRateLimited
from keka.models import EmployeeNotFoundError, KekaServiceError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

APP_ID            = os.getenv("MicrosoftAppId")
APP_PASSWORD      = os.getenv("MicrosoftAppPassword")
TENANT_ID         = os.getenv("MicrosoftAppTenantId")
# To test against a fixed Keka account instead of the real Teams user, set this to that email.
# Leave empty ("") in production so each employee's own email is used for all Keka operations.
# _KEKA_EMAIL_OVERRIDE = "recruiter@caizin.com"  # example test account
_KEKA_EMAIL_OVERRIDE = ""

adapter = BotFrameworkAdapter(BotFrameworkAdapterSettings(
    app_id=APP_ID,
    app_password=APP_PASSWORD,
    channel_auth_tenant=TENANT_ID,
))

app = FastAPI(title="Caizin HrOps Bot")

if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static", html=True), name="static")


# ── Response verb constants ──────────────────────────────────────────────────

_RESPONSE_LABEL = {
    "office":        "Office",
    "wfh":           "WFH",
    "leave":         "Leave",
    "client_site":   "Client Location",
    "floater_leave": "Floater Holiday",
}
_RESPONSE_COLOR = {
    "office":        "Good",
    "wfh":           "Accent",
    "leave":         "Warning",
    "client_site":   "Good",
    "floater_leave": "Warning",
}
_VALID_VERBS = frozenset(_RESPONSE_LABEL)

_BUCKET_COLOR = {
    "Office":          "Good",
    "WFH":             "Accent",
    "Leave":           "Warning",
    "Client Location": "Good",
    "Floater Holiday": "Warning",
    "Absent":          "Attention",
    "Pending":         "Default",
}

_BUCKET_TEXT = {
    "Office":          lambda n: f"{n} is in the office today.",
    "WFH":             lambda n: f"{n} is WFH today.",
    "Client Location": lambda n: f"{n} is at a client location today.",
    "Leave":           lambda n: f"{n} is on leave today.",
    "Floater Holiday": lambda n: f"{n} is on a floater holiday today.",
    "Absent":          lambda n: f"{n} is absent today.",
    "Pending":         lambda n: f"{n} is yet to check in today.",
}

_BUCKET_TEXT_SELF = {
    "Office":          "You have been marked as working from the office today.",
    "WFH":             "You have been marked as working from home today.",
    "Client Location": "You have been marked as working from a client location today.",
    "Leave":           "You have been marked as on leave today.",
    "Floater Holiday": "You have been marked as on a floater holiday today.",
    "Absent":          "You have been marked as absent today.",
    "Pending":         "You are yet to check in today.",
}


# ── Card builders ────────────────────────────────────────────────────────────

def _first_name(full_name: str) -> str:
    parts = (full_name or "").split()
    return parts[0] if parts else "there"


def _build_ack_card(name: str, status_verb: str, today: str, employee_id: str = "") -> dict:
    label = _RESPONSE_LABEL.get(status_verb, status_verb.replace("_", " ").title())
    color = _RESPONSE_COLOR.get(status_verb, "Default")
    try:
        date_display = datetime.strptime(today, "%Y-%m-%d").strftime("%B %d, %Y")
    except ValueError:
        date_display = today
    body = [
        {
            "type": "TextBlock",
            "text": f"{date_display} : {label}",
            "weight": "Bolder",
            "color": color,
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": f"Thanks, {name}! Your attendance has been recorded.",
            "wrap": True,
            "isSubtle": True,
        },
    ]
    actions = []
    if employee_id:
        actions = [{
            "type": "Action.Execute",
            "title": "Edit Status",
            "verb": "show_options",
            "data": {"employee_id": employee_id},
        }]
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }
    if actions:
        card["actions"] = actions
    return card


def _build_options_card(name: str, employee_id: str, current_response: str = "", is_floater: bool = False) -> dict:
    if current_response:
        cur_label = _RESPONSE_LABEL.get(current_response, current_response)
        heading = f"Your current status is **{cur_label}**. Select a new location to update:"
    else:
        heading = f"Hi {name}, where are you working from today?"

    buttons = [
        {"type": "Action.Execute", "title": "Office",          "verb": "submit_status", "data": {"status": "office",      "employee_id": employee_id}},
        {"type": "Action.Execute", "title": "Home (WFH)",      "verb": "submit_status", "data": {"status": "wfh",         "employee_id": employee_id}},
        {"type": "Action.Execute", "title": "Leave",           "verb": "submit_status", "data": {"status": "leave",       "employee_id": employee_id}},
        {"type": "Action.Execute", "title": "Client Location", "verb": "submit_status", "data": {"status": "client_site", "employee_id": employee_id}},
    ]
    if is_floater:
        buttons.append({
            "type": "Action.Execute", "title": "Floater Leave",
            "verb": "submit_status", "data": {"status": "floater_leave", "employee_id": employee_id},
        })

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [{"type": "TextBlock", "text": heading, "wrap": True, "weight": "Bolder"}],
        "actions": buttons,
    }


def _build_dashboard_card(records: list, today: str, name_query: str = "", status_filter: str = "all") -> dict:
    """Full attendance dashboard with name search and status filter."""
    try:
        date_display = datetime.strptime(today, "%Y-%m-%d").strftime("%B %d, %Y")
    except ValueError:
        date_display = today

    # Apply filters
    filtered = records
    if name_query:
        q = name_query.strip().lower()
        filtered = [r for r in filtered if q in (r.get("name") or "").lower()]
    if status_filter and status_filter != "all":
        filtered = [r for r in filtered if r.get("bucket", "Pending") == status_filter]

    total  = len(records)
    shown  = len(filtered)
    summary = f"{shown} of {total}" if (name_query or status_filter != "all") else str(total)

    # Employee rows
    rows = []
    for r in sorted(filtered, key=lambda x: (x.get("name") or "").lower()):
        name   = r.get("name", "")
        bucket = r.get("bucket", "Pending")
        color  = _BUCKET_COLOR.get(bucket, "Default")
        rows.append({
            "type": "ColumnSet",
            "spacing": "Small",
            "columns": [
                {
                    "type": "Column", "width": "stretch",
                    "items": [{"type": "TextBlock", "text": name, "wrap": False, "size": "Small"}],
                },
                {
                    "type": "Column", "width": "auto",
                    "items": [{"type": "TextBlock", "text": bucket, "color": color, "wrap": False, "size": "Small"}],
                },
            ],
        })

    if not rows:
        rows = [{"type": "TextBlock", "text": "No matching employees found.", "isSubtle": True, "spacing": "Small"}]

    body = [
        # Title + count
        {
            "type": "ColumnSet",
            "columns": [
                {
                    "type": "Column", "width": "stretch",
                    "items": [{"type": "TextBlock", "text": f"Attendance — {date_display}", "weight": "Bolder", "size": "Medium"}],
                },
                {
                    "type": "Column", "width": "auto",
                    "items": [{"type": "TextBlock", "text": summary, "isSubtle": True, "size": "Small", "verticalContentAlignment": "Center"}],
                },
            ],
        },
        # Search + filter row
        {
            "type": "ColumnSet",
            "columns": [
                {
                    "type": "Column", "width": "stretch",
                    "items": [{
                        "type": "Input.Text", "id": "name_query",
                        "placeholder": "Search by name", "value": name_query,
                    }],
                },
                {
                    "type": "Column", "width": "auto",
                    "items": [{
                        "type": "Input.ChoiceSet", "id": "status_filter",
                        "style": "compact", "value": status_filter,
                        "choices": [
                            {"title": "All Statuses",    "value": "all"},
                            {"title": "Office",          "value": "Office"},
                            {"title": "WFH",             "value": "WFH"},
                            {"title": "Leave",           "value": "Leave"},
                            {"title": "Client Location", "value": "Client Location"},
                            {"title": "Floater Holiday", "value": "Floater Holiday"},
                            {"title": "Absent",          "value": "Absent"},
                            {"title": "Pending",         "value": "Pending"},
                        ],
                    }],
                },
            ],
        },
        # Table header
        {
            "type": "ColumnSet", "separator": True, "spacing": "Small",
            "columns": [
                {"type": "Column", "width": "stretch", "items": [{"type": "TextBlock", "text": "Employee", "weight": "Bolder", "size": "Small", "isSubtle": True}]},
                {"type": "Column", "width": "auto",    "items": [{"type": "TextBlock", "text": "Status",   "weight": "Bolder", "size": "Small", "isSubtle": True}]},
            ],
        },
    ] + rows

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": [{"type": "Action.Submit", "title": "Search"}],
    }


def _fmt_days(n: float) -> str:
    return str(int(n)) if n == int(n) else str(n)


def _build_balance_card(employee_name: str, balances: list) -> dict:
    def _col(text, width, color=None, bold=False, align="Left"):
        tb = {"type": "TextBlock", "text": text, "size": "Small", "wrap": False,
              "horizontalAlignment": align}
        if bold:
            tb["weight"] = "Bolder"
        if color:
            tb["color"] = color
        return {"type": "Column", "width": width, "items": [tb]}

    header_row = {
        "type": "ColumnSet", "separator": True, "spacing": "Small",
        "columns": [
            _col("Leave Type", "stretch", bold=True),
            _col("Used",      "60px", bold=True, align="Center"),
            _col("Available", "80px", bold=True, align="Center"),
        ],
    }

    rows = []
    for b in sorted(balances, key=lambda x: x.used, reverse=True):
        avail_color = "Good" if b.available > 0 else "Default"
        rows.append({
            "type": "ColumnSet", "spacing": "Small",
            "columns": [
                _col(b.leave_type_name,      "stretch"),
                _col(_fmt_days(b.used),      "60px", align="Center"),
                _col(_fmt_days(b.available), "80px", color=avail_color, align="Center"),
            ],
        })

    first_name = _first_name(employee_name)
    body = [
        {"type": "TextBlock", "text": "Leave Balance", "weight": "Bolder", "size": "Large"},
        {"type": "TextBlock", "text": f"Hi {first_name}, here's a summary of your leave balance.",
         "isSubtle": True, "size": "Small", "spacing": "None", "wrap": True},
        header_row,
        *rows,
    ]

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }


def _sort_leave_types(leave_types: list) -> list:
    priority = ["paid leave", "casual leave"]
    def _key(lt):
        name = lt.name.lower()
        for i, p in enumerate(priority):
            if p in name:
                return i
        return len(priority)
    return sorted(leave_types, key=_key)


def _build_chat_leave_form(leave_types: list, error: str = "", prefill: dict = None) -> dict:
    prefill = prefill or {}
    leave_types = _sort_leave_types(leave_types)
    choices = [{"title": lt.name, "value": lt.id} for lt in leave_types]
    paid = next((lt for lt in leave_types if "paid leave" in lt.name.lower()), None)
    default_id = paid.id if paid else (leave_types[0].id if leave_types else "")
    body = [
        {
            "type": "Input.ChoiceSet",
            "id": "leave_type_id",
            "label": "Leave Type",
            "style": "compact",
            "isRequired": True,
            "errorMessage": "Please select a leave type.",
            "value": prefill.get("leave_type_id") or default_id,
            "choices": choices,
        },
        {
            "type": "Input.ChoiceSet",
            "id": "session_type",
            "label": "Duration",
            "style": "compact",
            "isRequired": True,
            "errorMessage": "Please select a duration.",
            "value": prefill.get("session_type") or "full_day",
            "choices": [
                {"title": "Full Day",    "value": "full_day"},
                {"title": "First Half",  "value": "first_half"},
                {"title": "Second Half", "value": "second_half"},
            ],
        },
        {"type": "Input.Date", "id": "from_date", "label": "From Date", "isRequired": True, "errorMessage": "Please select a From date.", "value": prefill.get("from_date") or ""},
        {"type": "Input.Date", "id": "to_date",   "label": "To Date",   "isRequired": True, "errorMessage": "Please select a To date.",   "value": prefill.get("to_date") or ""},
        {
            "type": "Input.Text",
            "id": "reason",
            "label": "Reason",
            "isRequired": True,
            "errorMessage": "Please provide a reason for your leave.",
            "isMultiline": True,
            "placeholder": "e.g. Personal work",
            "value": prefill.get("reason") or "",
        },
    ]
    if error:
        body.append({"type": "TextBlock", "text": error, "color": "Attention", "wrap": True})
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": [
            {"type": "Action.Submit", "title": "Apply Leave", "data": {"form_type": "chat_leave_submit"}},
            {"type": "Action.Submit", "title": "Cancel", "associatedInputs": "none", "data": {"form_type": "chat_leave_discard"}},
        ],
    }


def _build_text_card(title: str, text: str) -> dict:
    body = []
    if title:
        body.append({"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium"})
    body.append({"type": "TextBlock", "text": text, "wrap": True})
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }


def _build_work_status_card(results: list[dict], is_self: bool = False) -> dict:
    """Adaptive Card showing today's work status for one or more employees."""
    if not results:
        return _build_text_card("", "No attendance record found for today.")

    if len(results) == 1:
        person = results[0]
        bucket = person["bucket"]
        color  = _BUCKET_COLOR.get(bucket, "Default")
        if is_self:
            text = _BUCKET_TEXT_SELF.get(bucket, f"You are {bucket} today.")
        else:
            text = _BUCKET_TEXT.get(bucket, lambda n: f"{n} is {bucket} today.")(person["name"])
        return {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [
                {
                    "type": "TextBlock",
                    "text": text,
                    "color": color,
                    "wrap": True,
                    "size": "Medium",
                },
            ],
        }

    facts = [{"title": p["name"], "value": p["bucket"]} for p in results[:8]]
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": f"Found {len(results)} people matching that name:",
                "isSubtle": True,
                "wrap": True,
            },
            {"type": "FactSet", "facts": facts},
        ],
    }


def _build_help_card(header: str = "", footer: str = "") -> dict:
    body = []
    if header:
        body.append({"type": "TextBlock", "text": header, "wrap": True, "spacing": "None"})
    body += [
        {"type": "TextBlock", "text": "InSync — Available Commands", "weight": "Bolder", "size": "Medium", "spacing": "Medium" if header else "None"},
        {"type": "FactSet", "facts": [
            {"title": "balance",    "value": "Check your leave balance"},
            {"title": "leave",      "value": "Apply for leave"},
            {"title": "help",       "value": "Show this help"},
        ]},
        {
            "type": "TextBlock",
            "text": footer or "Use the commands above to manage your leave.",
            "wrap": True,
            "isSubtle": True,
            "spacing": "Medium",
        },
    ]
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }


# ── Invoke response builders ─────────────────────────────────────────────────

def _card_action_response(card: dict) -> dict:
    return {"statusCode": 200, "type": "application/vnd.microsoft.card.adaptive", "value": card}



# ── Task-module helpers (compose extension) ──────────────────────────────────

def _task_continue(title: str, card: dict, height: str = "medium", width: str = "medium") -> dict:
    return {
        "task": {
            "type": "continue",
            "value": {
                "title": title,
                "height": height,
                "width": width,
                "card": {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card,
                },
            },
        }
    }


def _task_message(text: str) -> dict:
    return {"task": {"type": "message", "value": text}}


def _build_apply_leave_card(leave_types: list, prefill: dict = None) -> dict:
    """Apply leave card for task modules (compose extension + chat trigger). Uses Action.Submit."""
    prefill = prefill or {}
    leave_types = _sort_leave_types(leave_types)
    choices = [{"title": lt.name, "value": lt.id} for lt in leave_types]
    paid = next((lt for lt in leave_types if "paid leave" in lt.name.lower()), None)
    default_id = paid.id if paid else (leave_types[0].id if leave_types else "")
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "Input.ChoiceSet",
                "id": "leave_type_id",
                "label": "Leave Type",
                "style": "compact",
                "isRequired": True,
                "errorMessage": "Please select a leave type.",
                "value": prefill.get("leave_type_id") or default_id,
                "choices": choices,
            },
            {
                "type": "Input.ChoiceSet",
                "id": "session_type",
                "label": "Duration",
                "style": "compact",
                "isRequired": True,
                "errorMessage": "Please select a duration.",
                "value": prefill.get("session_type") or "full_day",
                "choices": [
                    {"title": "Full Day",    "value": "full_day"},
                    {"title": "First Half",  "value": "first_half"},
                    {"title": "Second Half", "value": "second_half"},
                ],
            },
            {"type": "Input.Date", "id": "from_date", "label": "From Date", "isRequired": True, "errorMessage": "Please select a From date.", "value": prefill.get("from_date") or ""},
            {"type": "Input.Date", "id": "to_date",   "label": "To Date",   "isRequired": True, "errorMessage": "Please select a To date.",   "value": prefill.get("to_date") or ""},
            {
                "type": "Input.Text",
                "id": "reason",
                "label": "Reason",
                "isRequired": True,
                "errorMessage": "Please provide a reason for your leave.",
                "isMultiline": True,
                "placeholder": "e.g. Personal work",
                "value": prefill.get("reason") or "",
            },
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Submit",
                "data": {"commandId": "applyLeave"},
            }
        ],
    }


# ── User email helper ────────────────────────────────────────────────────────

def _leave_email(user_email: str) -> str:
    # Returns override email if set (for local testing), otherwise the real Teams user email.
    return _KEKA_EMAIL_OVERRIDE or user_email


async def _get_user_email(turn_context) -> str:
    try:
        member = await TeamsInfo.get_member(
            turn_context, turn_context.activity.from_property.id
        )
        if member and member.email:
            return member.email.lower()
    except Exception as exc:
        logger.warning("[bot] TeamsInfo.get_member failed: %s", exc)
    # Local-dev fallback: parse display name / id from the Teams activity
    from teams_bot import _get_employee_email
    return _get_employee_email(turn_context)


# ── adaptiveCard/action handlers ─────────────────────────────────────────────

async def _handle_submit_status(turn_context, data: dict) -> dict:
    employee_id = data.get("employee_id", "")
    status_verb = data.get("status", "")

    if status_verb not in _VALID_VERBS:
        return _card_action_response(
            _build_text_card("Error", "Unknown status response. Please try again.")
        )

    name = _first_name(
        (turn_context.activity.from_property.name or "")
        if turn_context.activity.from_property else ""
    )

    if employee_id:
        saved = record_attendance_response(employee_id, status_verb)
        if not saved:
            logger.warning("[bot] submit_status: DB write failed emp=%s verb=%s", employee_id, status_verb)

    today = datetime.now(IST).strftime("%Y-%m-%d")
    return _card_action_response(_build_ack_card(name, status_verb, today, employee_id))


async def _handle_show_options(turn_context, data: dict) -> dict:
    employee_id      = data.get("employee_id", "")
    current_response = data.get("current_response", "")
    name = _first_name(
        (turn_context.activity.from_property.name or "")
        if turn_context.activity.from_property else ""
    )
    return _card_action_response(_build_options_card(name, employee_id, current_response))


async def _handle_view_dashboard() -> dict:
    records = get_today_all_records()
    today   = datetime.now(IST).strftime("%Y-%m-%d")
    return _card_action_response(_build_dashboard_card(records, today))


async def _handle_filter_dashboard(data: dict) -> dict:
    name_query    = (data.get("name_query")    or "").strip()
    status_filter = (data.get("status_filter") or "all").strip()
    records = get_today_all_records()
    today   = datetime.now(IST).strftime("%Y-%m-%d")
    return _card_action_response(_build_dashboard_card(records, today, name_query, status_filter))


async def _execute_leave_submission(email: str, data: dict):
    """Shared core: validate form data, call Keka, return (result, error, parsed_data).
    Returns (None, error_str, parsed) on validation failure,
    (result, None, parsed) after API call."""
    leave_type_id = data.get("leave_type_id") or ""
    from_date     = data.get("from_date", "")
    to_date       = data.get("to_date", "")
    session_type  = SessionType(data.get("session_type") or "full_day")
    reason        = (data.get("reason") or "").strip()

    logger.info("[apply_leave] email=%s leave_type_id=%s from=%s to=%s session=%s",
                email, leave_type_id, from_date, to_date, session_type)

    if not reason:
        return None, "Please provide a reason for your leave.", None
    if not from_date or not to_date:
        return None, "Please select both the From and To dates.", None
    if datetime.strptime(to_date, "%Y-%m-%d") < datetime.strptime(from_date, "%Y-%m-%d"):
        return None, "The To date cannot be earlier than the From date.", None
    if session_type != SessionType.FULL_DAY and from_date != to_date:
        return None, "Half-day leave can only be applied for a single day. Please select the same date for both the From and To fields.", None
    if session_type != SessionType.FULL_DAY:
        to_date = from_date

    result = await leave_service.apply_leave(
        _leave_email(email), leave_type_id, from_date, to_date, session_type, reason
    )
    logger.info("[apply_leave] result: success=%s message=%s", result.success, result.message)
    return result, None, None


async def _nlp_leave_submit(turn_context, data: dict) -> dict:
    """Handler for chat/NLP adaptive card submit (adaptiveCard/action verb=apply_leave)."""
    logger.info("[apply_leave] received data: %s", data)

    email = await _get_user_email(turn_context)
    if not email:
        return _card_action_response(
            _build_text_card("Error", "Could not identify your account. Please contact IT.")
        )

    try:
        result, error, _ = await _execute_leave_submission(email, data)
    except Exception as exc:
        logger.error("[apply_leave] service error: %s", exc, exc_info=True)
        return _card_action_response(
            _build_text_card("Error", f"Could not submit leave request: {exc}")
        )

    if error:
        leave_types = await leave_service.get_applicable_leave_types(_leave_email(email))
        return _card_action_response(_build_chat_leave_form(leave_types, error=error))
    if result.success:
        return _card_action_response(
            _build_text_card("Leave Applied", "Your leave request has been submitted successfully.")
        )
    leave_types = await leave_service.get_applicable_leave_types(_leave_email(email))
    return _card_action_response(_build_chat_leave_form(leave_types, error=result.message))


# ── Chat command handlers ────────────────────────────────────────────────────

async def _handle_cmd_balance(turn_context, email: str) -> None:
    emp_name, balances = await leave_service.get_leave_balance(_leave_email(email))
    await turn_context.send_activity(
        MessageFactory.attachment(CardFactory.adaptive_card(
            _build_balance_card(emp_name, balances)
        ))
    )


async def _send_chat_leave_form(turn_context, leave_types: list, prefill: dict = None, error: str = "") -> None:
    card = _build_chat_leave_form(leave_types, error=error, prefill=prefill)
    await turn_context.send_activity(MessageFactory.attachment(CardFactory.adaptive_card(card)))


async def _handle_cmd_leave(turn_context, email: str) -> None:
    leave_types = await leave_service.get_applicable_leave_types(_leave_email(email))
    await _send_chat_leave_form(turn_context, leave_types)


async def _handle_cmd_attendance(turn_context) -> None:
    records = get_today_all_records()
    today   = datetime.now(IST).strftime("%Y-%m-%d")
    await turn_context.send_activity(
        MessageFactory.attachment(
            CardFactory.adaptive_card(_build_dashboard_card(records, today))
        )
    )


async def _handle_cmd_help(turn_context) -> None:
    await turn_context.send_activity(
        MessageFactory.attachment(CardFactory.adaptive_card(_build_help_card()))
    )


async def _handle_work_location_query(turn_context, user_email: str, parsed: dict) -> None:
    """Fetch and send a work status card based on the parsed work location query."""
    target = parsed.get("target")

    if target == "self":
        result = await asyncio.get_event_loop().run_in_executor(
            None, get_work_status_by_email, user_email
        )
        results = [result] if result else []
    else:
        name = (parsed.get("name") or "").strip()
        if not name:
            await turn_context.send_activity(
                MessageFactory.text("I couldn't identify the person you're asking about. Please include their name.")
            )
            return
        results = await asyncio.get_event_loop().run_in_executor(
            None, get_work_status_by_name, name
        )

    if not results:
        label = "your attendance record" if target == "self" else f"**{parsed.get('name')}**"
        await turn_context.send_activity(
            MessageFactory.text(f"No attendance record found for {label} today.")
        )
        return

    card = _build_work_status_card(results, is_self=(target == "self"))
    await turn_context.send_activity(
        MessageFactory.attachment(CardFactory.adaptive_card(card))
    )



# ── Per-command fetch handlers (compose extension) ───────────────────────────

async def _fetch_balance(turn_context, email: str) -> dict:
    emp_name, balances = await leave_service.get_leave_balance(_leave_email(email))
    return _task_continue("Leave Balance", _build_balance_card(emp_name, balances))


async def _fetch_apply_leave(turn_context, email: str) -> dict:
    leave_types = await leave_service.get_applicable_leave_types(_leave_email(email))
    return _task_continue("Apply for Leave", _build_apply_leave_card(leave_types))



async def _fetch_help(turn_context, email: str) -> dict:
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": "Available Commands", "size": "Medium", "weight": "Bolder"},
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Balance",     "value": "Check your current leave balance"},
                    {"title": "Apply Leave", "value": "Submit a leave request"},
                    {"title": "Attendance",  "value": "View today’s team attendance"},
                ],
            },
            {
                "type": "TextBlock",
                "text": "Type @HrOps Test in the chat to access these commands.",
                "wrap": True,
                "isSubtle": True,
                "spacing": "Medium",
            },
        ],
    }
    return _task_continue("Help", card, height="small")


_FETCH_HANDLERS = {
    "balance": _fetch_balance,
    "leave":   _fetch_apply_leave,
    "help":    _fetch_help,
}


# ── Per-command submit handlers (compose extension) ──────────────────────────

async def _compose_leave_submit(turn_context, data: dict, email: str) -> dict:
    """Handler for compose extension submit (composeExtension/submitAction)."""
    try:
        result, error, _ = await _execute_leave_submission(email, data)
    except Exception as exc:
        logger.error("[apply_leave] service error: %s", exc, exc_info=True)
        leave_types = await leave_service.get_applicable_leave_types(_leave_email(email))
        card = _build_apply_leave_card(leave_types)
        card["body"].append({"type": "TextBlock", "text": f"Could not submit leave: {exc}", "color": "Attention", "wrap": True})
        return _task_continue("Apply for Leave", card)

    if error:
        leave_types = await leave_service.get_applicable_leave_types(_leave_email(email))
        card = _build_apply_leave_card(leave_types)
        card["body"].append({"type": "TextBlock", "text": error, "color": "Attention", "wrap": True})
        return _task_continue("Apply for Leave", card)
    if result.success:
        return _task_message("Your leave request has been submitted successfully.")
    leave_types = await leave_service.get_applicable_leave_types(_leave_email(email))
    card = _build_apply_leave_card(leave_types)
    card["body"].append({"type": "TextBlock", "text": result.message, "color": "Attention", "wrap": True})
    return _task_continue("Apply for Leave", card)


_SUBMIT_HANDLERS = {
    "leave": _compose_leave_submit,
}


# ── Invoke orchestration (compose extension) ─────────────────────────────────

async def _handle_fetch_task(turn_context, command_id: str) -> dict:
    try:
        email = await _get_user_email(turn_context)

        if command_id in COMMAND_FLAGS:
            if not email:
                return _task_message("Could not identify your account. Please contact IT.")
            if not surface_enabled(COMMAND_FLAGS[command_id], email):
                return _task_message("This feature is not available for your account yet.")

        handler = _FETCH_HANDLERS.get(command_id)
        if not handler:
            return _task_message("Unknown command.")
        return await handler(turn_context, email)
    except Exception as exc:
        logger.error("[bot] _handle_fetch_task error command=%s: %s", command_id, exc)
        return _task_message("Something went wrong. Please try again.")


async def _handle_submit_action(turn_context, command_id: str, data: dict) -> dict:
    email = await _get_user_email(turn_context)

    if command_id in COMMAND_FLAGS:
        if not email:
            return _task_message("Could not identify your account. Please contact IT.")
        if not surface_enabled(COMMAND_FLAGS[command_id], email):
            return _task_message("This feature is not available for your account.")

    handler = _SUBMIT_HANDLERS.get(command_id)
    if not handler:
        return _task_message("Request processed.")
    return await handler(turn_context, data, email)


# ── Bot endpoint ─────────────────────────────────────────────────────────────

@app.post("/api/messages")
async def messages(req: Request):
    if req.headers.get("content-length") == "0":
        return Response(status_code=400)
    try:
        body = await req.json()
    except Exception:
        return Response(status_code=400)

    activity    = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")

    async def turn_handler(turn_context):
        act = turn_context.activity
        logger.info("[bot] incoming activity type=%s name=%s", act.type, getattr(act, "name", None))

        # ── Compose extension ─────────────────────────────────────────────
        if act.type == "invoke" and act.name == "composeExtension/fetchTask":
            command_id = (act.value or {}).get("commandId", "")
            resp = await _handle_fetch_task(turn_context, command_id)
            await turn_context.send_activity(Activity(
                type="invokeResponse",
                value=InvokeResponse(status=200, body=resp),
            ))
            return

        if act.type == "invoke" and act.name == "composeExtension/submitAction":
            command_id = (act.value or {}).get("commandId", "")
            data       = (act.value or {}).get("data", {})
            resp = await _handle_submit_action(turn_context, command_id, data)
            await turn_context.send_activity(Activity(
                type="invokeResponse",
                value=InvokeResponse(status=200, body=resp),
            ))
            return

        # ── Adaptive Card Action.Execute ──────────────────────────────────
        if act.type == "invoke" and act.name == "adaptiveCard/action":
            action = (act.value or {}).get("action", {}) or {}
            verb   = action.get("verb", "")
            data   = action.get("data", {}) or {}

            if verb == "submit_status":
                resp = await _handle_submit_status(turn_context, data)
            elif verb == "show_options":
                resp = await _handle_show_options(turn_context, data)
            elif verb == "view_dashboard":
                resp = await _handle_view_dashboard()
            elif verb == "apply_leave":
                resp = await _nlp_leave_submit(turn_context, data)
            elif verb == "filter_dashboard":
                resp = await _handle_filter_dashboard(data)
            else:
                logger.warning("[bot] unknown adaptiveCard/action verb: %s", verb)
                resp = _card_action_response({})

            await turn_context.send_activity(Activity(
                type="invokeResponse",
                value=InvokeResponse(status=200, body=resp),
            ))
            return

        # ── Chat messages ─────────────────────────────────────────────────
        if act.type != "message":
            return

        # ── Chat leave form submit (Action.Submit from inline chat card) ──
        if isinstance(act.value, dict) and act.value.get("form_type") == "chat_leave_submit":
            email = await _get_user_email(turn_context)
            if not email or not surface_enabled("leave_management", email):
                await turn_context.send_activity(MessageFactory.text(
                    "This feature is not enabled for your account."
                ))
                return
            card_activity_id = getattr(act, "reply_to_id", None) or act.value.get("card_activity_id") or ""
            logger.info("[leave_form] submit card_activity_id=%r (reply_to_id=%r)", card_activity_id, getattr(act, "reply_to_id", None))

            async def _update_or_send(card: dict) -> None:
                if card_activity_id:
                    logger.info("[leave_form] update_activity id=%s", card_activity_id)
                    msg = MessageFactory.attachment(CardFactory.adaptive_card(card))
                    msg.id = card_activity_id
                    await turn_context.update_activity(msg)
                else:
                    logger.info("[leave_form] fallback send_activity (no card_activity_id)")
                    await turn_context.send_activity(MessageFactory.attachment(CardFactory.adaptive_card(card)))

            try:
                result, error, _ = await _execute_leave_submission(email, act.value)
            except Exception as exc:
                logger.error("[apply_leave] service error: %s", exc, exc_info=True)
                await _update_or_send(_build_text_card("Error", f"Could not submit leave request: {exc}"))
                return
            if error:
                leave_types = await leave_service.get_applicable_leave_types(_leave_email(email))
                await _update_or_send(_build_chat_leave_form(leave_types, error=error, prefill=act.value))
                return
            if result.success:
                leave_types = await leave_service.get_applicable_leave_types(_leave_email(email))
                lt_map = {lt.id: lt.name for lt in leave_types}
                lt_name     = lt_map.get(act.value.get("leave_type_id") or "", "Leave")
                from_date   = act.value.get("from_date", "")
                to_date     = act.value.get("to_date", "")
                session     = act.value.get("session_type", "full_day").replace("_", " ").title()
                date_str    = from_date if from_date == to_date else f"{from_date} → {to_date}"
                ack_card = {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": "Leave Applied ✓", "weight": "Bolder", "size": "Medium", "color": "Good"},
                        {"type": "FactSet", "facts": [
                            {"title": "Type",     "value": lt_name},
                            {"title": "Date",     "value": date_str},
                            {"title": "Session",  "value": session},
                        ]},
                    ],
                }
                await _update_or_send(ack_card)
            else:
                leave_types = await leave_service.get_applicable_leave_types(_leave_email(email))
                await _update_or_send(_build_chat_leave_form(leave_types, error=result.message, prefill=act.value))
            return

        # ── Chat leave form discard ──
        if isinstance(act.value, dict) and act.value.get("form_type") == "chat_leave_discard":
            card_activity_id = getattr(act, "reply_to_id", None) or act.value.get("card_activity_id") or ""
            help_card = _build_help_card(header="Changed your mind? Here's what I can help you with:")
            if card_activity_id:
                msg = MessageFactory.attachment(CardFactory.adaptive_card(help_card))
                msg.id = card_activity_id
                await turn_context.update_activity(msg)
            else:
                await turn_context.send_activity(MessageFactory.attachment(CardFactory.adaptive_card(help_card)))
            return

        # Strip @mention tags Teams injects when the bot is mentioned
        raw_text = act.text or ""
        text = re.sub(r"<at>[^<]*</at>", "", raw_text).strip()

        email = await _get_user_email(turn_context)
        cmd   = text.split()[0].lower() if text else ""

        if cmd in ("/balance", "balance"):
            if not email or not surface_enabled("leave_management", email):
                await turn_context.send_activity(MessageFactory.text(
                    "This feature is not enabled for your account."
                ))
                return
            await _handle_cmd_balance(turn_context, email)

        elif cmd in ("/leave", "/applyleave", "applyleave") or text.lower() == "leave":
            if not email or not surface_enabled("leave_management", email):
                await turn_context.send_activity(MessageFactory.text(
                    "This feature is not enabled for your account."
                ))
                return
            await _handle_cmd_leave(turn_context, email)

        elif cmd in ("/attendance", "attendance"):
            if not email or not surface_enabled("attendance_card", email):
                await turn_context.send_activity(MessageFactory.text(
                    "This feature is coming soon to your account."
                ))
                return
            await _handle_cmd_attendance(turn_context)

        elif cmd in ("/dashboard", "dashboard"):
            await turn_context.send_activity(
                MessageFactory.attachment(CardFactory.adaptive_card(
                    _build_help_card(footer="To view your colleagues' work locations, check the **People Pulse** tab.")
                ))
            )

        elif cmd in ("/help", "help"):
            await _handle_cmd_help(turn_context)

        else:
            text_lower = text.lower()
            _WORK_LOC_KEYWORDS = (
                "work location", "working from", "work status",
                "where is", "where am i", "working today",
                "in office today", "wfh today", "work from home today",
                "working remotely",
            )
            if email and surface_enabled("attendance_card", email) and any(kw in text_lower for kw in _WORK_LOC_KEYWORDS):
                parsed_wl = await asyncio.get_event_loop().run_in_executor(
                    None, extract_work_location_query, text
                )
                if parsed_wl:
                    await _handle_work_location_query(turn_context, email, parsed_wl)
                    return

            _LEAVE_KEYWORDS = ("leave", "apply", "off", "balance", "vacation", "sick", "casual", "annual", "holiday", "cancel", "withdraw", "revoke")
            _CANCEL_KEYWORDS = ("cancel", "withdraw", "revoke")
            if email and surface_enabled("leave_management", email) and any(kw in text_lower for kw in _LEAVE_KEYWORDS):
                if any(kw in text_lower for kw in _CANCEL_KEYWORDS) and "leave" in text_lower:
                    await turn_context.send_activity(
                        "Leave cancellation is currently not available in Caizin InSync. "
                        "Please cancel your leave request in Keka."
                    )
                    return
                today_str  = datetime.now(IST).strftime("%Y-%m-%d")
                leave_types = await leave_service.get_applicable_leave_types(_leave_email(email))
                lt_names    = [lt.name for lt in leave_types]
                parsed = await asyncio.get_event_loop().run_in_executor(
                    None, extract_leave_request, text, today_str, lt_names
                )
                if parsed:
                    if parsed.get("action") == "check_balance":
                        await _handle_cmd_balance(turn_context, email)
                        return
                    if parsed.get("action") == "cancel_leave":
                        await turn_context.send_activity(
                            "Cancel leave is not supported in this system. "
                            "Please cancel your leave directly from **Keka**."
                        )
                        return
                    if parsed.get("action") == "apply_leave":
                        hint = (parsed.get("leave_type_hint") or "").strip().lower()
                        form_error = ""
                        if hint:
                            for lt in leave_types:
                                if lt.name.lower() == hint:
                                    parsed["leave_type_id"] = lt.id
                                    break
                            else:
                                raw_hint = (parsed.get("leave_type_hint") or hint).strip()
                                form_error = (
                                    f"**'{raw_hint}'** is not an available leave type for you. "
                                    "Please select from the options below."
                                )
                        await _send_chat_leave_form(turn_context, leave_types, prefill=parsed, error=form_error)
                        return

            intent = await asyncio.get_event_loop().run_in_executor(None, _classify_intent, text)
            if intent == "greeting":
                await turn_context.send_activity(
                    MessageFactory.attachment(CardFactory.adaptive_card(
                        _build_help_card(header="Hi there! 👋 Here's what I can help you with:")
                    ))
                )
            else:
                await turn_context.send_activity(
                    MessageFactory.attachment(CardFactory.adaptive_card(
                        _build_help_card(
                            header="This chat supports **work location and leave** only."
                        )
                    ))
                )

    invoke_response = await adapter.process_activity(activity, auth_header, turn_handler)

    if invoke_response:
        return Response(
            content=json.dumps(invoke_response.body),
            status_code=invoke_response.status,
            media_type="application/json",
        )
    return Response(status_code=201)


# ── Policy Q&A (AskCAI tab) ──────────────────────────────────────────────────

@app.post("/ask")
async def ask(req: Request):
    body     = await req.json()
    question = body.get("question", "")
    answer, intent = await ask_policy_question(question)
    return {"answer": answer, "intent": intent}


# ── Tab endpoints ─────────────────────────────────────────────────────────────

_TAB_CSP = "frame-ancestors teams.microsoft.com *.teams.microsoft.com *.skype.com"

_FEATURE_OFF_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Not available</title><style>
 html,body{height:100%;margin:0;font-family:"Segoe UI",system-ui,-apple-system,sans-serif;
  font-size:14px;background:#fff;color:#1b1d21}
 @media (prefers-color-scheme:dark){html,body{background:#1b1b1d;color:#e7e8ea}}
 .s{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;
  text-align:center;gap:8px;padding:24px}
 h1{font-size:1.1rem;font-weight:650}p{color:#8a909e;font-size:.875rem;max-width:42ch}
</style></head><body><div class="s">
<h1>Not available yet</h1>
<p>This tab is switched off for your account. It will appear here once the feature is released.</p>
</div></body></html>"""


def _feature_off_page() -> HTMLResponse:
    """Shown when a tab's feature flag is off."""
    return HTMLResponse(content=_FEATURE_OFF_HTML, headers={"Content-Security-Policy": _TAB_CSP})


@app.get("/icon.png")
async def app_icon():
    return FileResponse(os.path.join("..", "manifest", "color.png"), media_type="image/png")


@app.get("/tabs/askcai")
async def tab_askcai():
    r = FileResponse(os.path.join("static", "askcai.html"))
    r.headers["Content-Security-Policy"] = _TAB_CSP
    return r


@app.get("/tabs/dashboard")
async def tab_dashboard():
    if os.getenv("FEATURE_DASHBOARD", "1") == "0":
        return _feature_off_page()
    r = FileResponse(os.path.join("static", "dashboard.html"))
    r.headers["Content-Security-Policy"] = _TAB_CSP
    return r


@app.get("/tabs/people-pulse")
async def tab_people_pulse():
    dashboard_on = os.getenv("FEATURE_DASHBOARD", "1") != "0"
    filename = "dashboard.html" if dashboard_on else "coming_soon.html"
    r = FileResponse(os.path.join("static", filename))
    r.headers["Content-Security-Policy"] = _TAB_CSP
    return r


@app.get("/tabs/dashboard-data")
async def dashboard_data():
    records, date = get_latest_records()
    return {"date": date, "records": records}


# ── Timesheet tab ─────────────────────────────────────────────────────────────

def _timesheet_on() -> bool:
    return os.getenv("FEATURE_TIMESHEET", "1") != "0"


def _contract_error(status: int, code: str, message: str, retry_after: int = None) -> JSONResponse:
    """Error body shaped by artifacts/timesheet-ui-contract.yaml."""
    body = {"error": {"code": code, "message": message}}
    if retry_after is not None:
        body["error"]["retry_after_seconds"] = retry_after
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(status_code=status, content=body, headers=headers)


@app.get("/tabs/timesheet")
async def tab_timesheet():
    if not _timesheet_on():
        return _feature_off_page()
    r = FileResponse(os.path.join("static", "timesheet-dashboard.html"))
    r.headers["Content-Security-Policy"] = _TAB_CSP
    return r


_TEST_FIXTURE = os.path.join("artifacts", "examples", "timesheet-2026-08.json")


@app.get("/tabs/timesheet-testdata")
async def timesheet_testdata():
    """
    The checked-in fixture, so the tab renders with no API and no Keka.

    The tab requests this only in test mode — a browser session outside Teams, or
    an explicit ?data=test. A live Teams session goes to the contract endpoint.
    Same shape as GET /api/timesheet/months/{month}; it is validated against
    artifacts/timesheet-ui-contract.yaml.
    """
    if not os.path.isfile(_TEST_FIXTURE):
        return _contract_error(
            502, "upstream_unavailable",
            "Test data is missing. Expected artifacts/examples/timesheet-2026-08.json.",
        )
    return FileResponse(_TEST_FIXTURE, media_type="application/json")


def _timesheet_source() -> str:
    """
    'keka'  read live from Keka (keka/timesheet_service.py)
    'mock'  synthesise it (timesheet_mock.py)

    TIMESHEET_SOURCE forces either; otherwise Keka is used when KEKA_CLIENT_ID,
    KEKA_CLIENT_SECRET and KEKA_API_KEY are all set in the environment, and the mock
    covers local dev without them.
    """
    forced = (os.getenv("TIMESHEET_SOURCE") or "").strip().lower()
    if forced in ("keka", "mock"):
        return forced
    return "keka" if keka_client.is_configured() else "mock"


@app.get("/api/timesheet/months/{month}")
async def timesheet_month(month: str, request: Request):
    """
    Contract: artifacts/timesheet-ui-contract.yaml (GET /api/timesheet/months/{month})
    Upstreams: artifacts/keka-timesheet-apis.md

    Reads Keka when configured, else the mock, then layers the employee's clock-in
    status over the result from the attendance tracker. Either way the response shape is
    the contract — the tab is written against that, not against this handler.
    """
    if not _timesheet_on():
        return _contract_error(403, "not_entitled", "Timesheet is not enabled for your account yet.")

    source = _timesheet_source()

    # X-Mock-Employee is a stand-in for the Teams SSO token, which is what will
    # identify the employee once auth lands. Remove both together.
    email = (request.headers.get("X-Mock-Employee") or "").strip().lower()
    if source == "keka" and not email:
        return _contract_error(
            403, "not_entitled",
            "Could not identify your account. Please reopen the tab from Teams.",
        )

    if source == "keka" and not keka_client.is_configured():
        # Overwhelmingly the cause when TIMESHEET_SOURCE=keka is forced on a box with
        # no credentials. Worth naming, rather than reporting it as a transient outage.
        logger.error("[timesheet] source=keka but these env vars are unset: %s",
                     ", ".join(keka_client.missing_secrets()))
        return _contract_error(
            502, "upstream_unavailable",
            "Timesheet is not connected to the HR system yet. Please contact IT.",
        )

    builder = timesheet_service.build_month if source == "keka" else timesheet_mock.build_month

    try:
        payload = await asyncio.get_event_loop().run_in_executor(
            None, builder, month, email, ""
        )
    except ValueError:
        return _contract_error(400, "invalid_month", "Month must be in YYYY-MM format.")
    except EmployeeNotFoundError:
        logger.warning("[timesheet] no Keka employee for %s", email)
        return _contract_error(
            403, "not_entitled",
            "No employee record was found for your account. Please contact HR.",
        )
    except KekaRateLimited as exc:
        return _contract_error(
            429, "rate_limited",
            "Too many requests to the HR system right now. Please retry shortly.",
            retry_after=getattr(exc, "retry_after", 60),
        )
    except KekaServiceError as exc:
        # build_month degrades each upstream read on its own now, so reaching here
        # means the failure was outside any single read. Kept as a backstop.
        logger.error("[timesheet] upstream failed month=%s: %s", month, exc)
        return _contract_error(
            502, "upstream_unavailable",
            "Timesheet data is temporarily unavailable. Try again in a minute.",
        )
    except Exception as exc:
        logger.error("[timesheet] build failed month=%s: %s", month, exc, exc_info=True)
        return _contract_error(
            502, "upstream_unavailable",
            "Timesheet data is temporarily unavailable. Try again in a minute.",
        )

    # Clock-in marks are not a Keka read — they come from the attendance tracker, the
    # same row the People Pulse tab renders. That independence is the point: a Keka
    # outage degrades the month it cannot fill, and the employee still sees where they
    # worked each day. The mock keeps the attendance it synthesised.
    if source == "keka":
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, timesheet_attendance.attach, payload, email
            )
        except Exception as exc:
            logger.warning("[timesheet] attendance unavailable for %s: %s", email, exc)
            # Named alongside the Keka reads that failed, so the UI can say which
            # part of the calendar is blank rather than leaving it unexplained.
            unavailable = payload.setdefault("unavailable", [])
            if "attendance" not in unavailable:
                unavailable.append("attendance")

    if payload.get("unavailable"):
        logger.warning("[timesheet] degraded month=%s email=%s missing=%s",
                       month, email, ",".join(payload["unavailable"]))

    # Which side produced this, without touching the contract body.
    return JSONResponse(content=payload, headers={"X-Timesheet-Source": source})


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

