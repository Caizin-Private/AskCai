from botbuilder.core import TurnContext
from botbuilder.schema import Activity, ActivityTypes, Attachment
from botbuilder.schema import HeroCard, CardAction, ActionTypes
import json
import logging
from rag import ask_policy_question


logger = logging.getLogger(__name__)

# Sentinel value used to trigger the Apply Leave form from the welcome card
APPLY_LEAVE_TRIGGER = "__open_apply_leave_form__"


# =========================
# EMAIL RESOLUTION
#
# Teams work accounts set from_property.name to the user's UPN which
# is usually their work email (user@company.com).
# If your org uses a different format, swap this logic for a Graph API
# call: GET https://graph.microsoft.com/v1.0/users/{aadObjectId}/mail
# =========================
def _get_employee_email(turn_context: TurnContext) -> str:
    """Extract the employee's email from the Teams activity."""
    from_prop = turn_context.activity.from_property

    if not from_prop:
        logger.warning("[email] from_property is None")
        return ""


    logger.info(f"[email] from_prop.id:   {from_prop.id}")
    logger.info(f"[email] from_prop.name: {from_prop.name}")
    logger.info(f"[email] aad_object_id:  {getattr(from_prop, 'aad_object_id', None)}")

    # Also log channel_data for extra info
    channel_data = turn_context.activity.channel_data or {}
    logger.info(f"[email] channel_data: {channel_data}")


    # Option 1: name field contains UPN / email (most common for work accounts)
    name = from_prop.name or ""
    if "@" in name:
        logger.info(f"[email] ✅ found in name: {name}")
        return name.lower().strip()

    # Option 2: id field sometimes contains the email in webchat / dev testing
    user_id = from_prop.id or ""
    if "@" in user_id:
        logger.info(f"[email] ✅ found in id: {user_id}")
        return user_id.lower().strip()

    # Option 3: Construct email from display name as firstname.lastname@caizin.com
    if name:
        parts = name.lower().split()
        email = f"{parts[0]}.{parts[-1]}@caizin.com" if len(parts) >= 2 else f"{parts[0]}@caizin.com"
        logger.info(f"[email] ✅ constructed from name: {email}")
        return email

    logger.warning("[email] could not resolve email")

    return ""


# =========================
# MESSAGE HANDLER  (updated: passes employee_email)
# =========================
async def on_message_activity(turn_context: TurnContext):
    user_text = turn_context.activity.text

    if not user_text:
        return

    await turn_context.send_activity(
        Activity(type=ActivityTypes.typing)
    )

    employee_email = _get_employee_email(turn_context)
    logger.info(f"[bot] question='{user_text}' | email='{employee_email}'")

    answer = ask_policy_question(user_text, employee_email=employee_email)
    await turn_context.send_activity(answer)


# =========================
# APPLY LEAVE FORM  (Adaptive Card with dropdowns + date inputs)
# =========================
async def send_apply_leave_form(turn_context: TurnContext):
    """Send an Adaptive Card form for applying leave."""

    card_json = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": "📅 Apply for Leave",
                "size": "Large",
                "weight": "Bolder",
                "color": "Accent"
            },
            {
                "type": "TextBlock",
                "text": "Fill in the details below and hit **Submit**.",
                "wrap": True,
                "isSubtle": True,
                "spacing": "Small"
            },
            {
                "type": "Input.ChoiceSet",
                "id": "leave_type",
                "label": "Leave Type",
                "isRequired": True,
                "errorMessage": "Please select a leave type.",
                "choices": [
                    {"title": "Casual Leave",    "value": "Casual Leave"},
                    {"title": "Sick Leave",      "value": "Sick Leave"},
                    {"title": "Earned Leave",    "value": "Earned Leave"},
                    {"title": "Compensatory Off","value": "Compensatory Off"},
                    {"title": "Maternity Leave", "value": "Maternity Leave"},
                    {"title": "Paternity Leave", "value": "Paternity Leave"},
                    {"title": "Loss of Pay",     "value": "Loss of Pay"},
                ],
                "placeholder": "Select leave type",
                "style": "compact"
            },
            {
                "type": "Input.Date",
                "id": "from_date",
                "label": "From Date",
                "isRequired": True,
                "errorMessage": "Please select a start date."
            },
            {
                "type": "Input.Date",
                "id": "to_date",
                "label": "To Date",
                "isRequired": True,
                "errorMessage": "Please select an end date."
            },
            {
                "type": "Input.Text",
                "id": "reason",
                "label": "Reason (optional)",
                "placeholder": "e.g. Personal work",
                "isMultiline": True
            }
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": "✅ Submit Leave",
                "data": {
                    "action": "apply_leave_submit"
                },
                "style": "positive"
            },
            {
                "type": "Action.Submit",
                "title": "✖ Cancel",
                "data": {
                    "action": "apply_leave_cancel"
                }
            }
        ]
    }

    attachment = Attachment(
        content_type="application/vnd.microsoft.card.adaptive",
        content=card_json
    )

    reply = Activity(
        type="message",
        attachments=[attachment]
    )

    await turn_context.send_activity(reply)


# =========================
# WELCOME CARD  (updated: added Leave Balance + Apply Leave buttons)
# =========================
async def send_suggested_questions(turn_context: TurnContext):

    card_json = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": "📘 Caizin Policy Assistant",
                "size": "Large",
                "weight": "Bolder",
                "color": "Accent"
            },
            {
                "type": "TextBlock",
                "text": "Please select a policy topic:",
                "wrap": True,
                "spacing": "Small"
            },

            # Row 1
            {
                "type": "ColumnSet",
                "spacing": "Medium",
                "columns": [
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "ActionSet",
                                "actions": [
                                    {
                                        "type": "Action.Submit",
                                        "title": "My Leave Balance",
                                        "data": {"text": "What is my leave balance?"}
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "ActionSet",
                                "actions": [
                                    {
                                        "type": "Action.Submit",
                                        "title": "📝 Apply Leave",
                                        "data": {"text": "__open_apply_leave_form__"}
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },

            # Row 2
            {
                "type": "ColumnSet",
                "spacing": "Small",
                "columns": [
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "ActionSet",
                                "actions": [
                                    {
                                        "type": "Action.Submit",
                                        "title": "Leaves",
                                        "data": {"text": "Tell me about leave policy"}
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "ActionSet",
                                "actions": [
                                    {
                                        "type": "Action.Submit",
                                        "title": "Fitness Policy",
                                        "data": {"text": "Tell me about fitness reimbursement policy"}
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },

            # Row 3
            {
                "type": "ColumnSet",
                "spacing": "Small",
                "columns": [
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "ActionSet",
                                "actions": [
                                    {
                                        "type": "Action.Submit",
                                        "title": "Travel Policy",
                                        "data": {"text": "Tell me about travel policy"}
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "ActionSet",
                                "actions": [
                                    {
                                        "type": "Action.Submit",
                                        "title": "Referral Policy",
                                        "data": {"text": "Tell me about referral policy"}
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },

            # Row 4
            {
                "type": "ColumnSet",
                "spacing": "Small",
                "columns": [
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "ActionSet",
                                "actions": [
                                    {
                                        "type": "Action.Submit",
                                        "title": "POSH Policy",
                                        "data": {"text": "Tell me about POSH policy"}
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "type": "Column",
                        "width": "stretch"
                    }
                ]
            }
        ]
    }

    attachment = Attachment(
        content_type="application/vnd.microsoft.card.adaptive",
        content=card_json
    )

    reply = Activity(
        type="message",
        attachments=[attachment]
    )

    await turn_context.send_activity(reply)