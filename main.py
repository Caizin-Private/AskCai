import json
import logging
import os
import sys
from datetime import datetime, timezone
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

from teams_bot import (
    on_message_activity, send_suggested_questions, send_apply_leave_form, APPLY_LEAVE_TRIGGER,
    build_dummy_attendance_card, build_attendance_ack_card,
)
from rag import ask_policy_question
from conv_refs import save_ref, get_all_refs
from features import is_pilot
from teams_bot import _get_employee_email
from attendance_log import log_attendance, get_attendance_log

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


# =============================================================
# M4 — Proactive attendance card via APScheduler
# =============================================================
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from botbuilder.schema import ConversationReference

scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")


async def _send_attendance_card_to_all():
    refs = get_all_refs()
    logger.info(f"[scheduler] sending attendance card to {len(refs)} pilot(s)")
    for email, ref_dict in refs.items():
        try:
            ref = ConversationReference().deserialize(ref_dict)

            async def _callback(tc, email=email):
                await tc.send_activity(Activity(
                    type="message",
                    attachments=[Attachment(
                        content_type="application/vnd.microsoft.card.adaptive",
                        content=build_dummy_attendance_card(),
                    )]
                ))
                logger.info(f"[scheduler] sent attendance card to {email}")

            await adapter.continue_conversation(ref, _callback, APP_ID)
        except Exception as e:
            logger.error(f"[scheduler] failed for {email}: {e}")


scheduler.add_job(
    _send_attendance_card_to_all,
    CronTrigger(hour=9, minute=0, timezone="Asia/Kolkata"),
    id="daily_attendance_card",
    replace_existing=True,
)


@app.on_event("startup")
async def startup():
    scheduler.start()
    logger.info("[scheduler] APScheduler started — attendance card at 09:00 IST")


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)

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
                    await send_suggested_questions(turn_context)
                    # SPIKE M2: send dummy attendance card to verify Action.Execute works under isNotificationOnly
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
                await turn_context.send_activity("❌ Leave application cancelled.")
                await send_suggested_questions(turn_context)
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

            # Greeting triggers menu
            if user_text_lower in ["hi", "hello", "hey", "start", "menu"]:
                await send_suggested_questions(turn_context)
                return

            # SPIKE M2: type "spike" to re-send the dummy attendance card on demand
            if user_text_lower == "spike":
                await turn_context.send_activity(Activity(
                    type="message",
                    attachments=[Attachment(
                        content_type="application/vnd.microsoft.card.adaptive",
                        content=build_dummy_attendance_card(),
                    )]
                ))
                return

            # Apply Leave button on welcome card → show the form
            if user_text.strip() == APPLY_LEAVE_TRIGGER:
                await send_apply_leave_form(turn_context)
                return

            # Otherwise → RAG / tool routing
            await on_message_activity(turn_context)
            return

        # 4️⃣ Action.Execute invoke (Adaptive Card button tap)
        elif activity_type == "invoke" and (turn_context.activity.name or "").lower() == "adaptivecard/action":
            value  = turn_context.activity.value or {}
            action = value.get("action", {})
            if action.get("verb") == "dummy_attendance":
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


@app.get("/tabs/home")
async def tab_home():
    return FileResponse(os.path.join("static", "home.html"))


@app.get("/tabs/askcai")
async def tab_askcai():
    return FileResponse(os.path.join("static", "askcai.html"))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)