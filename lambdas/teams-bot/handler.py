"""
lambdas/teams-bot/handler.py
AWS Lambda handler for the Teams bot webhook.

Handles Action.Execute invokes from Adaptive Cards, including the
attendance dashboard popup triggered by the "Report" button.
"""

import json
import logging
from datetime import datetime, timezone, timedelta

from shared.teams_client import (
    build_dashboard_card,
)

# Import db when wired to real infrastructure
# from shared import db_client as db

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _task_module_response(card: dict, title: str = "Attendance Dashboard") -> dict:
    """Task Module response — Teams opens a popup dialog with the card inside."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "statusCode": 200,
            "type": "application/vnd.microsoft.activity.taskInfo",
            "value": {
                "type": "continue",
                "value": {
                    "title": title,
                    "height": "large",
                    "width": "large",
                    "card": {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": card,
                    },
                },
            },
        }),
    }


# ---------------------------------------------------------------------------
# Verb handlers
# ---------------------------------------------------------------------------

def _handle_view_dashboard() -> dict:
    """Query today's attendance from DB and return a Task Module popup."""
    today   = datetime.now(IST).strftime("%Y-%m-%d")
    title   = f"Attendance — {datetime.strptime(today, '%Y-%m-%d').strftime('%d %b %Y')}"

    # --- Replace with real DB calls when wired up ---
    # records   = db.query_attendance_by_date(today)
    # employees = db.get_all_active_employees()
    records   = []
    employees = []
    # ------------------------------------------------

    card = build_dashboard_card(records, employees, today)
    logger.info(f"[TeamsBot] dashboard popup: {len(records)} records for {today}")
    return _task_module_response(card, title)


# ---------------------------------------------------------------------------
# Main routing
# ---------------------------------------------------------------------------

def _handle_response(body: dict) -> dict:
    """Route an Action.Execute invoke to the correct handler."""
    action = (
        body.get("value", {})
            .get("action", {})
    )
    verb = action.get("verb", "")
    data = action.get("data", {})

    logger.info(f"[TeamsBot] verb={verb} data={data}")

    if verb == "view_dashboard":
        return _handle_view_dashboard()

    # Add other verb handlers here (e.g. dummy_attendance, show_options …)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"statusCode": 200, "type": "application/vnd.microsoft.card.adaptive", "value": {}}),
    }


def lambda_handler(event: dict, context) -> dict:
    """AWS Lambda entry point."""
    logger.info(f"[lambda] event keys: {list(event.keys())}")

    # Adaptive Card Action.Execute invokes arrive via API Gateway
    if "requestContext" in event:
        raw_body = event.get("body", "{}")
        body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body

        invoke_type = body.get("type", "")
        invoke_name = body.get("name", "")

        if invoke_type == "invoke" and invoke_name == "adaptiveCard/action":
            return _handle_response(body)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"status": "ok"}),
    }
