import json
import os
import sys
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
from botbuilder.schema import Activity, ActivityTypes

from teams_bot import on_message_activity, send_suggested_questions, send_apply_leave_form, APPLY_LEAVE_TRIGGER
from rag import ask_policy_question

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
                    await send_suggested_questions(turn_context)
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
                from teams_bot import _get_employee_email
                from keka.leave import handle_apply_leave
                from botbuilder.schema import Activity as _Activity, ActivityTypes as _AT

                # Show typing indicator
                await turn_context.send_activity(
                    Activity(type=ActivityTypes.typing)
                )

                # Extract form values
                leave_type = form_data.get("leave_type", "Casual Leave")
                from_date_raw  = form_data.get("from_date", "")
                to_date_raw    = form_data.get("to_date", "")
                reason         = form_data.get("reason", "")

                from_date = from_date_raw
                to_date   = to_date_raw

                if not from_date or not to_date:
                    await turn_context.send_activity(
                        "⚠️ Please fill in both **From** and **To** dates before submitting."
                    )
                    return

                employee_email = _get_employee_email(turn_context)

                args = {
                    "leave_type_name": leave_type,
                    "from_date":       from_date,
                    "to_date":         to_date,
                    "reason":          reason,
                }

                result = handle_apply_leave(args, employee_email)
                await turn_context.send_activity(result)
                return

        # 3️⃣ User sent a regular text message
        elif activity_type == "message":

            user_text = (turn_context.activity.text or "").strip()
            user_text_lower = user_text.lower()

            # Greeting triggers menu
            if user_text_lower in ["hi", "hello", "hey", "start", "menu"]:
                await send_suggested_questions(turn_context)
                return

            # Apply Leave button on welcome card → show the form
            if user_text.strip() == APPLY_LEAVE_TRIGGER:
                await send_apply_leave_form(turn_context)
                return

            # Otherwise → RAG / tool routing
            await on_message_activity(turn_context)
            return

        # 4️⃣ Ignore everything else safely
        else:
            return

    invoke_response = await adapter.process_activity(
        activity,
        auth_header,
        turn_handler
    )

    if invoke_response:
        return Response(
            content=invoke_response.body,
            status_code=invoke_response.status,
            media_type="application/json"
        )

    return Response(status_code=201)


@app.post("/ask")
async def ask(req: Request):
    body = await req.json()
    return {"answer": ask_policy_question(body.get("question"))}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)