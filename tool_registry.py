"""
tool_registry.py
Single source of truth for all tools.
To add a new tool: add to TOOL_DEFINITIONS + TOOL_HANDLERS only.
"""

from zoho.leave import *

TOOL_DEFINITIONS = [

    # ── Leave Balance ───────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_leave_balance",
            "description": (
                "Fetch the employee's current leave balance — how many days remaining "
                "for each leave type (Casual Leave, Sick Leave, Earned Leave, etc.). "
                "Use when employee asks: 'how many leaves do I have', "
                "'what is my leave balance', 'how many sick days are left'."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },

    # ── Apply Leave ─────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "apply_leave",
            "description": (
                "Submit a leave application for the employee. "
                "Use when the employee wants to apply, book, or request leave. "
                "The system will automatically resolve the employee ID and leave type ID. "
                "Only call this when you have from_date, to_date, and leave_type_name confirmed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "leave_type_name": {
                        "type": "string",
                        "description": (
                            "The name of the leave type as the user mentioned it. "
                            "e.g. 'Casual Leave', 'Sick Leave', 'Earned Leave'. "
                            "Do NOT try to resolve the ID — pass the name as-is."
                        ),
                    },
                    "from_date": {
                        "type": "string",
                        "description": "Leave start date in dd-MMM-yyyy format, e.g. '10-Mar-2026'.",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "Leave end date in dd-MMM-yyyy format, e.g. '12-Mar-2026'.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for the leave.",
                    },
                },
                "required": ["leave_type_name", "from_date", "to_date", "reason"],
            },
        },
    },

    # ── Get Leave Requests ──────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_leave_requests",
            "description": (
                "Fetch the list of all leave requests the employee has applied for. "
                "Shows leave type, dates, number of days, and approval status "
                "(Pending, Approved, Cancelled, Rejected). "
                "Use when employee asks: 'show my leaves', 'what leaves have I applied', "
                "'list my leave history', 'what leave requests do I have', "
                "'show my pending leaves', 'which leaves are approved'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_date": {
                        "type": "string",
                        "description": (
                            "Start of date range to fetch leaves from, in dd-MMM-yyyy format. "
                            "Defaults to 01-Jan of current year if not provided."
                        ),
                    },
                    "to_date": {
                        "type": "string",
                        "description": (
                            "End of date range to fetch leaves till, in dd-MMM-yyyy format. "
                            "Defaults to 31-Dec of current year if not provided."
                        ),
                    },
                },
                "required": [],
            },
        },
    },

    # ── Cancel Leave ────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "cancel_leave",
            "description": (
                "Cancel a pending or approved leave request for the employee. "
                "Use when employee says: 'cancel my leave', 'withdraw my leave', "
                "'revoke my leave from X to Y'. "
                "Requires the from_date and to_date of the leave to cancel. "
                "If the employee does not provide dates, ask them for the leave dates first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_date": {
                        "type": "string",
                        "description": "Start date of the leave to cancel, in dd-MMM-yyyy format.",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "End date of the leave to cancel, in dd-MMM-yyyy format.",
                    },
                },
                "required": ["from_date", "to_date"],
            },
        },
    },

]

TOOL_HANDLERS = {
    "get_leave_balance":   handle_get_leave_balance,
    "apply_leave":         handle_apply_leave,
    "get_leave_requests":  handle_get_leave_requests,
    "cancel_leave":        handle_cancel_leave,
}