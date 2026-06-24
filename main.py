import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from botbuilder.core import (
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
    CardFactory,
    MessageFactory,
)
from botbuilder.core.teams import TeamsInfo
from botbuilder.schema import Activity, InvokeResponse

from features import surface_enabled, is_pilot, COMMAND_FLAGS
from rag import ask_policy_question, _classify_intent, extract_leave_request
from keka.leave_service import leave_service
from keka.models import SessionType
from insync_db import (
    get_today_all_records,
    get_latest_records,
    record_attendance_response,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

APP_ID            = os.getenv("MicrosoftAppId")
APP_PASSWORD      = os.getenv("MicrosoftAppPassword")
TENANT_ID         = os.getenv("MicrosoftAppTenantId")
_KEKA_EMAIL_OVERRIDE = "recruiter@caizin.com"

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


def _build_balance_card(employee_name: str, email: str, balances: list) -> dict:
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
            _col("Accrued", "80px", bold=True, align="Center"),
            _col("Used",    "60px", bold=True, align="Center"),
            _col("Available","80px", bold=True, align="Center"),
        ],
    }

    rows = []
    for b in balances:
        avail_color = "Good" if b.available > 0 else "Default"
        rows.append({
            "type": "ColumnSet", "spacing": "Small",
            "columns": [
                _col(b.leave_type_name,       "stretch"),
                _col(_fmt_days(b.total),       "80px", align="Center"),
                _col(_fmt_days(b.used),        "60px", align="Center"),
                _col(_fmt_days(b.available),   "80px", color=avail_color, align="Center"),
            ],
        })

    body = [
        {"type": "TextBlock", "text": "Leave Balance", "weight": "Bolder", "size": "Large"},
        {"type": "TextBlock", "text": f"{employee_name}  ·  {email}",
         "isSubtle": True, "size": "Small", "spacing": "None"},
        header_row,
        *rows,
    ]

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }


def _build_chat_leave_form(leave_types: list, error: str = "", prefill: dict = None) -> dict:
    prefill = prefill or {}
    choices = [{"title": lt.name, "value": lt.id} for lt in leave_types]
    default_id = leave_types[0].id if leave_types else ""
    body = [
        {
            "type": "Input.ChoiceSet",
            "id": "leave_type_id",
            "label": "Leave Type",
            "style": "compact",
            "value": prefill.get("leave_type_id") or default_id,
            "choices": choices,
        },
        {
            "type": "Input.ChoiceSet",
            "id": "session_type",
            "label": "Duration",
            "style": "compact",
            "value": prefill.get("session_type") or "full_day",
            "choices": [
                {"title": "Full Day",    "value": "full_day"},
                {"title": "First Half",  "value": "first_half"},
                {"title": "Second Half", "value": "second_half"},
            ],
        },
        {"type": "Input.Date", "id": "from_date", "label": "From Date", "value": prefill.get("from_date") or ""},
        {"type": "Input.Date", "id": "to_date",   "label": "To Date",   "value": prefill.get("to_date") or ""},
        {
            "type": "Input.Text",
            "id": "reason",
            "label": "Reason (optional)",
            "isMultiline": True,
            "placeholder": "e.g. Personal work",
            "value": prefill.get("reason") or "",
        },
    ]
    if error:
        body.insert(0, {"type": "TextBlock", "text": error, "color": "Attention", "wrap": True})
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": [{"type": "Action.Execute", "title": "Apply Leave", "verb": "apply_leave"}],
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


def _build_help_card() -> dict:
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": "InSync — Available Commands", "weight": "Bolder", "size": "Medium"},
            {"type": "FactSet", "facts": [
                {"title": "/balance",    "value": "Check your leave balance"},
                {"title": "/leave",      "value": "Apply for leave"},
                {"title": "/dashboard",  "value": "View team attendance dashboard"},
                {"title": "/help",       "value": "Show this help"},
            ]},
            {
                "type": "TextBlock",
                "text": "For policy questions, switch to the **AskCAI** tab.",
                "wrap": True,
                "isSubtle": True,
                "spacing": "Medium",
            },
        ],
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


def _build_apply_leave_card(leave_types: list) -> dict:
    """Apply leave card for the compose extension task module (uses Action.Submit)."""
    choices = [{"title": lt.name, "value": lt.id} for lt in leave_types]
    default_id = leave_types[0].id if leave_types else ""
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
                "value": default_id,
                "choices": choices,
            },
            {
                "type": "Input.ChoiceSet",
                "id": "session_type",
                "label": "Duration",
                "style": "compact",
                "value": "full_day",
                "choices": [
                    {"title": "Full Day",    "value": "full_day"},
                    {"title": "First Half",  "value": "first_half"},
                    {"title": "Second Half", "value": "second_half"},
                ],
            },
            {"type": "Input.Date", "id": "from_date", "label": "From Date"},
            {"type": "Input.Date", "id": "to_date",   "label": "To Date"},
            {
                "type": "Input.Text",
                "id": "reason",
                "label": "Reason (optional)",
                "isMultiline": True,
                "placeholder": "e.g. Personal work",
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
    """Return KEKA_TEST_EMAIL override if set, otherwise the real user email."""
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


async def _handle_apply_leave_action(turn_context, data: dict) -> dict:
    logger.info("[apply_leave] received data: %s", data)

    leave_type_id = data.get("leave_type_id") or ""
    from_date     = data.get("from_date", "")
    to_date       = data.get("to_date", "")
    session_type  = SessionType(data.get("session_type") or "full_day")
    reason        = data.get("reason") or "Not specified"

    logger.info("[apply_leave] leave_type_id=%s from=%s to=%s session=%s",
                leave_type_id, from_date, to_date, session_type)

    if not from_date or not to_date:
        leave_types = await leave_service.get_leave_types()
        return _card_action_response(
            _build_chat_leave_form(leave_types, error="Please fill in both From and To dates.")
        )

    if session_type != SessionType.FULL_DAY:
        to_date = from_date

    email = await _get_user_email(turn_context)
    if not email:
        return _card_action_response(
            _build_text_card("Error", "Could not identify your account. Please contact IT.")
        )

    try:
        result = await leave_service.apply_leave(
            _leave_email(email), leave_type_id, from_date, to_date, session_type, reason
        )
    except Exception as exc:
        logger.error("[apply_leave] service error: %s", exc, exc_info=True)
        return _card_action_response(
            _build_text_card("Error", f"Could not submit leave request: {exc}")
        )

    logger.info("[apply_leave] result: success=%s message=%s", result.success, result.message)

    if result.success:
        return _card_action_response(
            _build_text_card("Leave Applied", "Your leave request has been submitted successfully.")
        )

    leave_types = await leave_service.get_leave_types()
    return _card_action_response(
        _build_chat_leave_form(leave_types, error=result.message)
    )


# ── Chat command handlers ────────────────────────────────────────────────────

async def _handle_cmd_balance(turn_context, email: str) -> None:
    emp_name, balances = await leave_service.get_leave_balance(_leave_email(email))
    await turn_context.send_activity(
        MessageFactory.attachment(CardFactory.adaptive_card(
            _build_balance_card(emp_name, _leave_email(email), balances)
        ))
    )


async def _handle_cmd_leave(turn_context, email: str) -> None:
    leave_types = await leave_service.get_leave_types()
    await turn_context.send_activity(
        MessageFactory.attachment(CardFactory.adaptive_card(_build_chat_leave_form(leave_types)))
    )


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




# ── Per-command fetch handlers (compose extension) ───────────────────────────

async def _fetch_balance(turn_context, email: str) -> dict:
    emp_name, balances = await leave_service.get_leave_balance(_leave_email(email))
    return _task_continue("Leave Balance", _build_balance_card(emp_name, _leave_email(email), balances))


async def _fetch_apply_leave(turn_context, email: str) -> dict:
    leave_types = await leave_service.get_leave_types()
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

async def _submit_apply_leave(turn_context, data: dict, email: str) -> dict:
    leave_type_id = data.get("leave_type_id") or ""
    from_date     = data.get("from_date", "")
    to_date       = data.get("to_date", "")
    session_type  = SessionType(data.get("session_type") or "full_day")
    reason        = data.get("reason") or "Not specified"

    if not from_date or not to_date:
        leave_types = await leave_service.get_leave_types()
        card = _build_apply_leave_card(leave_types)
        card["body"].insert(0, {
            "type": "TextBlock",
            "text": "Please fill in both From and To dates.",
            "color": "Attention",
            "wrap": True,
        })
        return _task_continue("Apply for Leave", card)

    if session_type != SessionType.FULL_DAY:
        to_date = from_date

    result = await leave_service.apply_leave(
        _leave_email(email), leave_type_id, from_date, to_date, session_type, reason
    )
    if result.success:
        return _task_message("Your leave request has been submitted successfully.")

    leave_types = await leave_service.get_leave_types()
    card = _build_apply_leave_card(leave_types)
    card["body"].insert(0, {
        "type": "TextBlock",
        "text": result.message,
        "color": "Attention",
        "wrap": True,
    })
    return _task_continue("Apply for Leave", card)


_SUBMIT_HANDLERS = {
    "applyLeave": _submit_apply_leave,
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
                resp = await _handle_apply_leave_action(turn_context, data)
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

        # Strip @mention tags Teams injects when the bot is mentioned
        raw_text = act.text or ""
        text = re.sub(r"<at>[^<]*</at>", "", raw_text).strip()

        email = await _get_user_email(turn_context)
        cmd   = text.split()[0].lower() if text else ""

        if cmd in ("/balance", "balance"):
            if not email or not is_pilot(email):
                await turn_context.send_activity(MessageFactory.text(
                    "This feature is coming soon to your account."
                ))
                return
            await _handle_cmd_balance(turn_context, email)

        elif cmd in ("/leave", "leave", "/applyleave", "applyleave"):
            if not email or not is_pilot(email):
                await turn_context.send_activity(MessageFactory.text(
                    "This feature is coming soon to your account."
                ))
                return
            await _handle_cmd_leave(turn_context, email)

        elif cmd in ("/attendance", "attendance", "/dashboard", "dashboard"):
            if not email or not is_pilot(email):
                await turn_context.send_activity(MessageFactory.text(
                    "This feature is coming soon to your account."
                ))
                return
            await _handle_cmd_attendance(turn_context)

        elif cmd in ("/help", "help"):
            await _handle_cmd_help(turn_context)

        else:
            _LEAVE_KEYWORDS = ("leave", "apply", "off", "balance", "vacation", "sick", "casual", "annual", "holiday")
            if email and is_pilot(email) and any(kw in text.lower() for kw in _LEAVE_KEYWORDS):
                today_str = datetime.now(IST).strftime("%Y-%m-%d")
                parsed = await asyncio.get_event_loop().run_in_executor(
                    None, extract_leave_request, text, today_str
                )
                if parsed:
                    if parsed.get("action") == "check_balance":
                        await _handle_cmd_balance(turn_context, email)
                        return
                    if parsed.get("action") == "apply_leave":
                        leave_types = await leave_service.get_leave_types()
                        await turn_context.send_activity(
                            MessageFactory.attachment(CardFactory.adaptive_card(
                                _build_chat_leave_form(leave_types, prefill=parsed)
                            ))
                        )
                        return

            answer, _ = await ask_policy_question(
                text,
                employee_email=email,
                policy_only=True,
            )
            await turn_context.send_activity(
                MessageFactory.attachment(CardFactory.adaptive_card(_build_text_card("", answer)))
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
    email    = body.get("employee_email", "")
    answer, intent = await ask_policy_question(
        question,
        employee_email=email,
        policy_only=bool(body.get("policy_only", False)),
    )
    return {"answer": answer, "intent": intent}


# ── Tab endpoints ─────────────────────────────────────────────────────────────

_TAB_CSP = "frame-ancestors teams.microsoft.com *.teams.microsoft.com *.skype.com"


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
    r = FileResponse(os.path.join("static", "dashboard.html"))
    r.headers["Content-Security-Policy"] = _TAB_CSP
    return r


@app.get("/tabs/people-pulse")
async def tab_people_pulse():
    r = FileResponse(os.path.join("static", "dashboard.html"))
    r.headers["Content-Security-Policy"] = _TAB_CSP
    return r


@app.get("/tabs/dashboard-data")
async def dashboard_data():
    records, date = get_latest_records()
    return {"date": date, "records": records}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
