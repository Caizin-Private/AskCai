import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
from botbuilder.schema import Activity, ActivityTypes, Attachment, InvokeResponse

from teams_bot import build_dummy_attendance_card, build_attendance_ack_card, _get_employee_email
from shared.teams_client import build_dashboard_card
from rag import ask_policy_question
from conv_refs import save_ref
from features import is_pilot
from attendance_log import log_attendance, get_attendance_log

# Maps attendance_log human-readable labels back to (db_status, employee_response)
# so build_dashboard_card's _status_bucket() produces the correct bucket.
_LOG_LABEL_TO_RECORD: dict[str, tuple[str, str | None]] = {
    "In Office":   ("present",                None),
    "WFH":         ("present",                "wfh"),
    "On Leave":    ("pre_approved_leave",      None),
    "Client Site": ("pre_applied_client_site", None),
}

APP_ID = os.getenv("MicrosoftAppId")
APP_PASSWORD = os.getenv("MicrosoftAppPassword")
TENANT_ID = os.getenv("MicrosoftAppTenantId")

settings = BotFrameworkAdapterSettings(
    app_id=APP_ID,
    app_password=APP_PASSWORD,
    channel_auth_tenant=TENANT_ID
)

adapter = BotFrameworkAdapter(settings)
app = FastAPI(title="Caizin Policy RAG Bot")

if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static", html=True), name="static")


@app.post("/api/messages")
async def messages(req: Request):

    if req.headers.get("content-length") == "0":
        return Response(status_code=400)

    try:
        body = await req.json()
    except Exception:
        return Response(status_code=400)

    activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")

    async def turn_handler(turn_context):

        activity_type = turn_context.activity.type

        # 1️⃣ Conversation started / Bot added
        if activity_type == "conversationUpdate":
            members_added = turn_context.activity.members_added or []

            for member in members_added:
                if member.id != turn_context.activity.recipient.id:
                    email = _get_employee_email(turn_context)
                    if is_pilot(email):
                        save_ref(email, turn_context)
                    # Chat is notification-only — send attendance card only, no policy buttons
                    await turn_context.send_activity(Activity(
                        type="message",
                        attachments=[Attachment(
                            content_type="application/vnd.microsoft.card.adaptive",
                            content=build_dummy_attendance_card(),
                        )]
                    ))
                    return

        # 2️⃣ Adaptive Card submit (Action.Submit fires this)
        elif activity_type == "message" and turn_context.activity.value:
            form_data = turn_context.activity.value

            action = form_data.get("action", "")

            # ── Cancel button ────────────────────────────────────────────
            if action == "apply_leave_cancel":
                return

            # ── Submit button ────────────────────────────────────────────
            if action == "apply_leave_submit":
                await turn_context.send_activity(
                    Activity(type=ActivityTypes.typing)
                )

                leave_type = form_data.get("leave_type", "Casual Leave")
                from_date  = form_data.get("from_date", "")
                to_date    = form_data.get("to_date", "")
                reason     = form_data.get("reason", "")

                if not from_date or not to_date:
                    await turn_context.send_activity(
                        "⚠️ Please fill in both **From** and **To** dates before submitting."
                    )
                    return

                from keka.client import TEST_EMPLOYEE_EMAIL
                from keka.mcp_agent import ask_keka_mcp
                from rag import _get_anthropic_api_key

                employee_email = TEST_EMPLOYEE_EMAIL
                query = (
                    f"Apply {leave_type} from {from_date} to {to_date}. "
                    f"Reason: {reason or 'Not specified'}."
                )
                result = await ask_keka_mcp(query, employee_email, _get_anthropic_api_key())
                await turn_context.send_activity(result)
                return

        # 3️⃣ User sent a regular text message
        elif activity_type == "message":
            email = _get_employee_email(turn_context)
            if is_pilot(email):
                save_ref(email, turn_context)

            user_text = (turn_context.activity.text or "").strip()
            user_text_lower = user_text.lower()

            # "spike" — re-send attendance card on demand for testing
            if user_text_lower == "spike":
                await turn_context.send_activity(Activity(
                    type="message",
                    attachments=[Attachment(
                        content_type="application/vnd.microsoft.card.adaptive",
                        content=build_dummy_attendance_card(),
                    )]
                ))
                return

            # Bot is notification-only — ignore all other text
            return

        # 4️⃣ Action.Execute invoke (Adaptive Card button tap)
        elif activity_type == "invoke" and (turn_context.activity.name or "").lower() == "adaptivecard/action":
            value  = turn_context.activity.value or {}
            action = value.get("action", {})
            verb   = action.get("verb")

            if verb == "dummy_attendance":
                status = action.get("data", {}).get("status", "unknown")
                ack = build_attendance_ack_card(status)
                await turn_context.send_activity(Activity(
                    type=ActivityTypes.invoke_response,
                    value=InvokeResponse(
                        status=200,
                        body={
                            "statusCode": 200,
                            "type": "application/vnd.microsoft.card.adaptive",
                            "value": ack,
                        },
                    )
                ))
                # Persist attendance choice for the Report pop-up
                email = _get_employee_email(turn_context)
                name  = (turn_context.activity.from_property.name or email) if turn_context.activity.from_property else email
                ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                log_attendance(email, name, status, ts)

            elif verb == "view_dashboard":
                today = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")
                display_date = datetime.strptime(today, "%Y-%m-%d").strftime("%d %b %Y")
                entries = get_attendance_log()
                # Convert attendance_log entries → records/employees for build_dashboard_card
                records = [
                    {
                        "employee_id": e["email"],
                        "status": _LOG_LABEL_TO_RECORD.get(e["status"], ("card_sent", None))[0],
                        "employee_response": _LOG_LABEL_TO_RECORD.get(e["status"], ("card_sent", None))[1],
                        "check_in_time": e.get("timestamp"),
                    }
                    for e in entries
                ]
                employees = [
                    {"employee_id": e["email"], "name": e["name"]}
                    for e in entries
                ]
                card = build_dashboard_card(records, employees, today)
                await turn_context.send_activity(Activity(
                    type=ActivityTypes.invoke_response,
                    value=InvokeResponse(
                        status=200,
                        body={
                            "statusCode": 200,
                            "type": "application/vnd.microsoft.activity.taskInfo",
                            "value": {
                                "type": "continue",
                                "value": {
                                    "title": f"Attendance — {display_date}",
                                    "height": "large",
                                    "width": "large",
                                    "card": {
                                        "contentType": "application/vnd.microsoft.card.adaptive",
                                        "content": card,
                                    },
                                },
                            },
                        },
                    )
                ))
                logger.info(f"[TeamsBot] dashboard popup: {len(records)} records for {today}")

            return

        # 5️⃣ Ignore everything else safely
        else:
            return

    invoke_response = await adapter.process_activity(
        activity,
        auth_header,
        turn_handler
    )

    if invoke_response:
        return Response(
            content=json.dumps(invoke_response.body),
            status_code=invoke_response.status,
            media_type="application/json"
        )

    return Response(status_code=201)


@app.post("/ask")
async def ask(req: Request):
    body = await req.json()
    return {"answer": await ask_policy_question(
        body.get("question", ""),
        policy_only=bool(body.get("policy_only", False)),
    )}


@app.get("/tabs/report-data")
async def report_data():
    from fastapi.responses import JSONResponse
    return JSONResponse(content={"entries": get_attendance_log()})


_TAB_CSP = "frame-ancestors teams.microsoft.com *.teams.microsoft.com *.skype.com"


@app.get("/tabs/attendance-status")
async def attendance_status(email: str = ""):
    from fastapi.responses import JSONResponse
    email = email.strip().lower()
    if not email:
        return JSONResponse(status_code=400, content={"error": "email required"})
    if not is_pilot(email):
        return JSONResponse(status_code=403, content={"error": "not in pilot"})
    from insync_db import get_today_status
    data = get_today_status(email)
    r = JSONResponse(content=data)
    r.headers["Content-Security-Policy"] = _TAB_CSP
    return r


@app.post("/tabs/record-attendance")
async def record_attendance(req: Request):
    from fastapi.responses import JSONResponse
    body   = await req.json()
    email  = (body.get("email")  or "").strip()
    name   = (body.get("name")   or email).strip()
    status = (body.get("status") or "unknown").strip()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log_attendance(email, name, status, ts)
    # Write to tracker RDS only when TRACKER_ALLOW_EDITS=true
    from insync_db import record_response
    record_response(email, status)
    return JSONResponse(content={"ok": True})


@app.get("/tabs/tracker-dashboard")
async def tracker_dashboard():
    from fastapi.responses import JSONResponse
    from insync_db import get_today_all_records
    ist = timezone(timedelta(hours=5, minutes=30))
    today = datetime.now(ist).strftime("%Y-%m-%d")
    data = get_today_all_records()
    r = JSONResponse(content={"entries": data, "date": today})
    r.headers["Content-Security-Policy"] = _TAB_CSP
    return r


@app.get("/tabs/home")
async def tab_home():
    r = FileResponse(os.path.join("static", "home.html"))
    r.headers["Content-Security-Policy"] = _TAB_CSP
    return r


@app.get("/tabs/askcai")
async def tab_askcai():
    r = FileResponse(os.path.join("static", "askcai.html"))
    r.headers["Content-Security-Policy"] = _TAB_CSP
    return r


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)