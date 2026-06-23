import json
import logging
import os
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, MessageFactory
from botbuilder.core.teams import TeamsInfo
from botbuilder.schema import Activity, InvokeResponse

from features import surface_enabled, COMMAND_FLAGS
from rag import ask_policy_question
from keka.mcp_agent import ask_keka_mcp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

APP_ID            = os.getenv("MicrosoftAppId")
APP_PASSWORD      = os.getenv("MicrosoftAppPassword")
TENANT_ID         = os.getenv("MicrosoftAppTenantId")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

adapter = BotFrameworkAdapter(BotFrameworkAdapterSettings(
    app_id=APP_ID,
    app_password=APP_PASSWORD,
    channel_auth_tenant=TENANT_ID,
))

app = FastAPI(title="Caizin HrOps Bot")

if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static", html=True), name="static")


# ── Bot helpers ───────────────────────────────────────────────────────────────

async def _get_user_email(turn_context) -> str:
    try:
        member = await TeamsInfo.get_member(
            turn_context, turn_context.activity.from_property.id
        )
        if member and member.email:
            return member.email.lower()
    except Exception as exc:
        logger.warning("[bot] TeamsInfo.get_member failed: %s", exc)
    return ""


# ── Task-module helpers ───────────────────────────────────────────────────────

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


def _build_apply_leave_card() -> dict:
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "Input.ChoiceSet",
                "id": "leave_type",
                "label": "Leave Type",
                "style": "compact",
                "value": "Casual Leave",
                "choices": [
                    {"title": "Casual Leave",     "value": "Casual Leave"},
                    {"title": "Sick Leave",        "value": "Sick Leave"},
                    {"title": "Earned Leave",      "value": "Earned Leave"},
                    {"title": "Compensatory Off",  "value": "Compensatory Off"},
                    {"title": "Loss of Pay",       "value": "Loss of Pay"},
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


# ── Per-command fetch handlers ────────────────────────────────────────────────

async def _fetch_balance(turn_context, email: str) -> dict:
    answer = await ask_keka_mcp(
        "What is my current leave balance broken down by leave type?",
        email,
        ANTHROPIC_API_KEY,
    )
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": "Your Leave Balance", "size": "Medium", "weight": "Bolder"},
            {"type": "TextBlock", "text": answer, "wrap": True},
        ],
    }
    return _task_continue("Leave Balance", card)


async def _fetch_apply_leave(turn_context, email: str) -> dict:
    return _task_continue("Apply for Leave", _build_apply_leave_card())


async def _fetch_attendance(turn_context, email: str) -> dict:
    from insync_db import get_today_all_records
    records = get_today_all_records()
    if not records:
        return _task_message("No attendance records found for today.")
    buckets: dict = {}
    for r in records:
        buckets.setdefault(r.get("bucket", "Pending"), []).append(r.get("name", ""))
    facts = [
        {"title": f"{bucket} ({len(names)})", "value": ", ".join(sorted(names))}
        for bucket, names in sorted(buckets.items())
    ]
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": "Attendance Today", "size": "Medium", "weight": "Bolder"},
            {"type": "FactSet", "facts": facts},
        ],
    }
    return _task_continue("Attendance Today", card)


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
                    {"title": "Attendance",  "value": "View today's team attendance"},
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


# Dispatch table — add new commands here
_FETCH_HANDLERS = {
    "balance":    _fetch_balance,
    "applyLeave": _fetch_apply_leave,
    "attendance": _fetch_attendance,
    "help":       _fetch_help,
}


# ── Per-command submit handlers ───────────────────────────────────────────────

async def _submit_apply_leave(turn_context, data: dict, email: str) -> dict:
    leave_type = data.get("leave_type", "Casual Leave")
    from_date  = data.get("from_date", "")
    to_date    = data.get("to_date",   "")
    reason     = data.get("reason") or "Not specified"

    if not from_date or not to_date:
        card = _build_apply_leave_card()
        card["body"].insert(0, {
            "type": "TextBlock",
            "text": "Please fill in both From and To dates.",
            "color": "Attention",
            "wrap": True,
        })
        return _task_continue("Apply for Leave", card)

    answer = await ask_keka_mcp(
        f"Apply {leave_type} for me from {from_date} to {to_date}. Reason: {reason}.",
        email,
        ANTHROPIC_API_KEY,
    )
    return _task_message(answer)


# Dispatch table — add new submit handlers here
_SUBMIT_HANDLERS = {
    "applyLeave": _submit_apply_leave,
}


# ── Invoke orchestration ──────────────────────────────────────────────────────

async def _handle_fetch_task(turn_context, command_id: str) -> dict:
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


# ── Bot endpoint ──────────────────────────────────────────────────────────────

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

        if act.type == "invoke" and act.name == "composeExtension/fetchTask":
            command_id = (act.value or {}).get("commandId", "")
            body = await _handle_fetch_task(turn_context, command_id)
            await turn_context.send_activity(Activity(
                type="invokeResponse",
                value=InvokeResponse(status=200, body=body),
            ))
            return

        if act.type == "invoke" and act.name == "composeExtension/submitAction":
            command_id = (act.value or {}).get("commandId", "")
            data       = (act.value or {}).get("data", {})
            body = await _handle_submit_action(turn_context, command_id, data)
            await turn_context.send_activity(Activity(
                type="invokeResponse",
                value=InvokeResponse(status=200, body=body),
            ))
            return

        if act.type != "message":
            return

        await turn_context.send_activity(MessageFactory.text(
            "Type **@HrOps Test** in the chat to access commands:\n\n"
            "• **Balance** — Check your current leave balance\n"
            "• **Apply Leave** — Submit a leave request\n"
            "• **Attendance** — View today's team attendance"
        ))

    invoke_response = await adapter.process_activity(activity, auth_header, turn_handler)

    if invoke_response:
        return Response(
            content=json.dumps(invoke_response.body),
            status_code=invoke_response.status,
            media_type="application/json",
        )
    return Response(status_code=201)


# ── Policy Q&A ────────────────────────────────────────────────────────────────

@app.post("/ask")
async def ask(req: Request):
    body = await req.json()
    return {"answer": await ask_policy_question(
        body.get("question", ""),
        policy_only=bool(body.get("policy_only", False)),
    )}


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


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
