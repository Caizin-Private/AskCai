"""
zoho/leave.py
MCP tool handlers for Leave.

apply_leave flow:
  Step 1: email → GET /forms/P_EmployeeView/records  → recordId (returns raw list)
  Step 2: recordId → GET /leave/getLeaveTypeDetails   → leaveTypeId for requested type
  Step 3: POST /forms/json/leave/insertRecord          → apply leave
"""

from datetime import datetime, timedelta
from zoho.client import *
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool: get_leave_balance
# ---------------------------------------------------------------------------

def handle_get_leave_balance(args: dict, employee_email: str) -> str:
    try:
        # Step 1: email → erecno
        erecno = get_employee_erecno(employee_email)

        # Step 2: fetch leave types using erecno
        data    = zoho_get("/leave/getLeaveTypeDetails", {"userId": erecno})
        results = data.get("response", {}).get("result", [])

        if not results:
            return "I couldn't find your leave balance. Please check with HR."

        lines = []
        for r in results:
            name    = r.get("Name", "Unknown")
            balance = r.get("BalanceCount", "?")
            availed = r.get("AvailedCount", "?")
            lines.append(f"• **{name}**: {balance} days remaining ({availed} used)")

        return "Here is your current leave balance:\n\n" + "\n".join(lines)

    except Exception as e:
        return f"Sorry, I couldn't fetch your leave balance. Please try again or contact HR. _(Error: {e})_"


# ---------------------------------------------------------------------------
# Tool: apply_leave
# ---------------------------------------------------------------------------

def handle_apply_leave(args: dict, employee_email: str) -> str:
    """
    Step 1: email → erecno (raw list response)
    Step 2: erecno → leave type ID (response.result list)
    Step 3: POST insertRecord with erecno + leaveTypeId
    """
    try:
        from_date       = args["from_date"]
        to_date         = args["to_date"]
        reason          = args.get("reason", "")
        leave_type_name = args.get("leave_type_name", "Casual Leave")

        # ── Step 1: email → erecno ────────────────────────────────────────
        erecno = get_employee_erecno(employee_email)
        print(f"[apply_leave] Step 1 ✅ erecno: {erecno}")

        # ── Step 2: erecno → leave type ID ───────────────────────────────
        lt_data   = zoho_get("/leave/getLeaveTypeDetails", {"userId": erecno})
        lt_results = lt_data.get("response", {}).get("result", [])

        if not lt_results:
            return "I couldn't fetch leave types from Zoho. Please contact HR."

        leave_type_id = _find_leave_type_id(lt_results, leave_type_name)

        if not leave_type_id:
            available = ", ".join(r.get("Name", "") for r in lt_results)
            return (
                f"I couldn't find leave type **'{leave_type_name}'**.\n\n"
                f"Available types: {available}\n\n"
                f"Please specify one of the above."
            )

        print(f"[apply_leave] Step 2 ✅ '{leave_type_name}' → ID: {leave_type_id}")

        # ── Step 3: Submit leave application ─────────────────────────────
        days    = _build_days_dict(from_date, to_date)
        payload = {
            "Employee_ID": erecno,
            "Leavetype":   leave_type_id,
            "From":        from_date,
            "To":          to_date,
            "days":        days,
        }

        print(f"[apply_leave] Step 3 payload: {payload}")
        data     = zoho_post_form("/forms/json/leave/insertRecord", payload)
        response = data.get("response", {})
        print(f"[apply_leave] Step 3 response: {response}")

        if response.get("status") == 0:
            return (
                f"✅ Your **{leave_type_name}** has been applied successfully!\n\n"
                f"• **From**: {from_date}\n"
                f"• **To**: {to_date}\n"
                f"• **Reason**: {reason or '—'}\n\n"
                f"Your request is pending manager approval. "
                f"You'll receive an email once it's reviewed."
            )
        else:
            errors = response.get("errors", {})
            msg    = errors.get("message", "Unknown error from Zoho.")
            return (
                f"Leave application could not be submitted.\n"
                f"Reason: _{msg}_\n\n"
                f"Please contact HR if this persists."
            )

    except Exception as e:
        return f"Sorry, I couldn't apply your leave. Please try again or contact HR. _(Error: {e})_"

# ---------------------------------------------------------------------------
# Tool: get_leave_requests  (V2 API)
# ---------------------------------------------------------------------------

def handle_get_leave_requests(args: dict, employee_email: str) -> str:
    """
    GET /api/v2/leavetracker/leaves/records
    Returns a dict keyed by Zoho.ID, each value is a leave record.
    """
    try:
        erecno = get_employee_erecno(employee_email)

        year  = datetime.now().year
        sdate = args.get("from_date", f"01-Jan-{year}")
        edate = args.get("to_date",   f"31-Dec-{year}")

        data = zoho_get_v2("/leavetracker/leaves/records", {
            "from": sdate,
            "to":   edate,
        })

        logger.info(f"[get_leave_requests] raw record count: {len(data.get('records', {}))}")

        all_records = data.get("records", {})

        # Filter to only this employee's records (employee filter param doesn't work in API)
        records = {
            zoho_id: r for zoho_id, r in all_records.items()
            if str(r.get("Employee.ID", "")) == str(erecno)
        }

        if not records:
            return (
                f"You have no leave requests found between "
                f"**{sdate}** and **{edate}**."
            )

        lines = []
        for zoho_id, r in records.items():
            leave_type = r.get("Leavetype",      "Unknown")
            from_date  = r.get("From",           "?")
            to_date    = r.get("To",             "?")
            raw_status = r.get("ApprovalStatus", "")
            # Empty string means pending in Zoho
            status     = raw_status if raw_status else "Pending"
            days_dict  = r.get("Days", {})
            days_count = round(sum(
                float(v.get("LeaveCount", 0)) for v in days_dict.values()
            ), 1)

            status_emoji = {
                "Approved":  "✅",
                "Pending":   "⏳",
                "Cancelled": "❌",
                "Rejected":  "🚫",
            }.get(status, "⏳")

            lines.append(
                f"{status_emoji} **{leave_type}** | "
                f"{from_date} → {to_date} | "
                f"{days_count} day(s) | _{status}_"
            )

        return (
            f"Here are your leave requests ({sdate} to {edate}):\n\n"
            + "\n".join(lines)
            + "\n\n_To cancel a leave, say: 'Cancel my leave from DD-MMM-YYYY to DD-MMM-YYYY'_"
        )

    except Exception as e:
        return f"Sorry, I couldn't fetch your leave requests. Please try again or contact HR. _(Error: {e})_"


# ---------------------------------------------------------------------------
# Tool: cancel_leave  (V2 API)
# ---------------------------------------------------------------------------

def handle_cancel_leave(args: dict, employee_email: str) -> str:
    try:
        from_date = args["from_date"]
        to_date   = args["to_date"]

        # Step 1: Get Employee Record Number
        erecno = get_employee_erecno(employee_email)

        # Step 2: Fetch records
        data = zoho_get_v2("/leavetracker/leaves/records", {
            "from": from_date,
            "to":   to_date,
        })

        all_records = data.get("records", {})

        # Filter by Employee ID
        records = {
            zoho_id: r for zoho_id, r in all_records.items()
            if str(r.get("Employee.ID", "")) == str(erecno)
        }

        if not records:
            return f"I couldn't find any leave request from **{from_date}** to **{to_date}**."

        # --- LOGIC FIX: Find the 'Active' leave first ---
        target_record = None
        target_id = None

        # Look for a record that is NOT 'Cancelled' or 'Rejected'
        for zid, r in records.items():
            status = r.get("ApprovalStatus", "").lower()
            if status not in ("cancelled", "rejected"):
                target_id, target_record = zid, r
                break

        # Fallback: If no active leave is found, pick the first one (to show the "Already Cancelled" message)
        if not target_record:
            target_id, target_record = next(iter(records.items()))

        zoho_id = target_id
        leave = target_record
        # ------------------------------------------------

        leave_type  = leave.get("Leavetype", "Leave")
        status      = leave.get("ApprovalStatus", "")
        actual_from = leave.get("From", from_date)
        actual_to   = leave.get("To", to_date)

        # Guard: If the selected leave is already cancelled, stop.
        if status.lower() in ("cancelled", "rejected"):
            return (
                f"Your **{leave_type}** from **{actual_from}** to **{actual_to}** "
                f"is already **{status}** — no action needed."
            )

        # Step 3: Cancel Request
        payload = {"reason": args.get("reason", "Cancelled via Assistant")}

        data = zoho_patch_v2(f"/leavetracker/leaves/records/cancel/{zoho_id}", params=payload)

        status_val = data.get("status", "")
        msg = data.get("message", "")

        if str(status_val).lower() == "success" or "successfully" in msg.lower():
            return (
                f"✅ Your **{leave_type}** from **{actual_from}** to **{actual_to}** "
                f"has been cancelled successfully."
            )
        else:
            return f"Could not cancel your leave. Reason: {msg}"

    except Exception as e:
        return f"Sorry, I couldn't cancel your leave. _(Error: {e})_"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_leave_type_id(leave_types: list, requested_name: str) -> str | None:
    """Match leave type name — exact first, then partial."""
    req = requested_name.lower().strip()

    for lt in leave_types:
        if lt.get("Name", "").lower().strip() == req:
            return lt.get("Id")

    for lt in leave_types:
        if req in lt.get("Name", "").lower():
            return lt.get("Id")

    return None


def _build_days_dict(from_date: str, to_date: str) -> dict:
    """Build per-day breakdown Zoho's insertRecord requires."""
    fmt     = "%d-%b-%Y"
    start   = datetime.strptime(from_date, fmt)
    end     = datetime.strptime(to_date,   fmt)
    days    = {}
    current = start
    while current <= end:
        days[current.strftime(fmt)] = {"LeaveCount": 1, "Session": 1}
        current += timedelta(days=1)
    return days