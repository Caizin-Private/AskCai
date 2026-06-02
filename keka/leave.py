"""
keka/leave.py
Leave operation handlers for Keka HRMS.

API reference (from Postman collection):
  GET  /api/v1/time/leavetypes          → all leave type names + identifiers
  GET  /api/v1/time/leavebalance        → org-wide balances, filter by employeeIdentifier
  GET  /api/v1/time/leaverequests       → org-wide requests, filter by employeeIdentifier
  POST /api/v1/time/leaverequests       → create leave request
  DELETE /api/v1/time/leaverequests/{id} → cancel leave request

Date formats (from Postman):
  POST body  : "fromDate": "2023-11-03"   (yyyy-MM-dd)
  GET params : "from": "12-02-2022"       (dd-MM-yyyy)
  GET response: "fromDate": "2023-09-14T00:00:00Z" (ISO 8601)

Session values:
  0 = FirstHalf  |  1 = SecondHalf
  Full-day leave → fromSession=0, toSession=1
"""

import logging
from datetime import datetime

from keka.client import get_employee_id, keka_get, keka_post, keka_delete
from keka.holidays import validate_leave_dates, count_working_days

logger = logging.getLogger(__name__)

STATUS_MAP = {
    0: ("⏳", "Pending"),
    1: ("✅", "Approved"),
    2: ("🚫", "Rejected"),
    3: ("❌", "Cancelled"),
    4: ("🔄", "In Approval"),
}


# ---------------------------------------------------------------------------
# Tool: get_leave_balance
# ---------------------------------------------------------------------------

def handle_get_leave_balance(args: dict, employee_email: str) -> str:
    try:
        emp_id = get_employee_id(employee_email)
        logger.info(f"[balance] employee_id: {emp_id}")

        # GET /time/leavebalance returns ALL employees; filter by id
        data = keka_get("/time/leavebalance")
        all_records = data.get("data", [])

        emp_record = next(
            (r for r in all_records if r.get("employeeIdentifier") == emp_id),
            None,
        )

        if not emp_record:
            # Try page 2+ (if org has >100 employees)
            next_page = data.get("nextPage")
            page = 2
            while next_page and not emp_record:
                paged = keka_get("/time/leavebalance", {"pageNumber": page, "pageSize": 100})
                for r in paged.get("data", []):
                    if r.get("employeeIdentifier") == emp_id:
                        emp_record = r
                        break
                next_page = paged.get("nextPage")
                page += 1

        if not emp_record:
            return "I couldn't find your leave balance in Keka. Please contact HR."

        balances = emp_record.get("leaveBalance", [])
        if not balances:
            return "No leave balance data found for your account. Please contact HR."

        lines = []
        for b in balances:
            name      = b.get("leaveTypeName", "Unknown")
            available = b.get("availableBalance", 0)
            consumed  = b.get("consumedAmount", 0)
            annual    = b.get("annualQuota", 0)
            lines.append(
                f"• **{name}**: {available} day(s) remaining "
                f"({consumed} used / {annual} annual quota)"
            )

        return "Here is your current leave balance:\n\n" + "\n".join(lines)

    except Exception as e:
        logger.error(f"[balance] error: {e}")
        return f"Sorry, I couldn't fetch your leave balance. Please try again or contact HR. _(Error: {e})_"


# ---------------------------------------------------------------------------
# Tool: apply_leave
# ---------------------------------------------------------------------------

def handle_apply_leave(args: dict, employee_email: str) -> str:
    try:
        from_date       = args["from_date"]       # yyyy-MM-dd
        to_date         = args["to_date"]          # yyyy-MM-dd
        reason          = args.get("reason", "")
        leave_type_name = args.get("leave_type_name", "Casual Leave")

        # ── Validate dates (weekends, holidays, past dates) ──────────────
        error = validate_leave_dates(from_date, to_date)
        if error:
            return error

        working_days = count_working_days(from_date, to_date)

        # ── Get employee ID ───────────────────────────────────────────────
        emp_id = get_employee_id(employee_email)
        logger.info(f"[apply] Step 1 ✅ employee_id: {emp_id}")

        # ── Resolve leave type identifier ─────────────────────────────────
        lt_data   = keka_get("/time/leavetypes")
        lt_list   = lt_data.get("data", [])
        leave_type_id = _find_leave_type_id(lt_list, leave_type_name)

        if not leave_type_id:
            available = ", ".join(lt.get("name", "") for lt in lt_list)
            return (
                f"I couldn't find leave type **'{leave_type_name}'**.\n\n"
                f"Available types in Keka: {available}\n\n"
                "Please specify one of the above."
            )
        logger.info(f"[apply] Step 2 ✅ '{leave_type_name}' → ID: {leave_type_id}")

        # ── Submit leave request ──────────────────────────────────────────
        payload = {
            "employeeId":   emp_id,
            "requestedBy":  emp_id,
            "fromDate":     from_date,
            "toDate":       to_date,
            "fromSession":  0,         # 0 = FirstHalf (start of day)
            "toSession":    1,         # 1 = SecondHalf (end of day) → full day
            "leaveTypeId":  leave_type_id,
            "reason":       reason,
            "note":         reason,
        }
        logger.info(f"[apply] Step 3 payload: {payload}")

        response = keka_post("/time/leaverequests", payload)
        logger.info(f"[apply] Step 3 response: {response}")

        if response.get("succeeded"):
            from_fmt = datetime.strptime(from_date, "%Y-%m-%d").strftime("%d %b %Y")
            to_fmt   = datetime.strptime(to_date,   "%Y-%m-%d").strftime("%d %b %Y")
            return (
                f"✅ Your **{leave_type_name}** has been applied successfully!\n\n"
                f"• **From**: {from_fmt}\n"
                f"• **To**: {to_fmt}\n"
                f"• **Working days**: {working_days}\n"
                f"• **Reason**: {reason or '—'}\n\n"
                "Your request is pending manager approval. "
                "You'll receive a notification once it's reviewed."
            )
        else:
            errors = response.get("errors") or [response.get("message", "Unknown error")]
            msg = "; ".join(errors) if isinstance(errors, list) else str(errors)
            return (
                f"Leave application could not be submitted.\n"
                f"Reason: _{msg}_\n\n"
                "Please contact HR if this persists."
            )

    except Exception as e:
        logger.error(f"[apply] error: {e}")
        return f"Sorry, I couldn't apply your leave. Please try again or contact HR. _(Error: {e})_"


# ---------------------------------------------------------------------------
# Tool: get_leave_requests
# ---------------------------------------------------------------------------

def handle_get_leave_requests(args: dict, employee_email: str) -> str:
    try:
        emp_id = get_employee_id(employee_email)

        year  = datetime.now().year
        # Keka query params use dd-MM-yyyy format (confirmed from Postman example)
        s_raw = args.get("from_date", f"01-01-{year}")   # dd-MM-yyyy
        e_raw = args.get("to_date",   f"31-12-{year}")   # dd-MM-yyyy

        data = keka_get("/time/leaverequests", {"from": s_raw, "to": e_raw})
        all_records = data.get("data", [])

        # Filter to this employee only
        records = [r for r in all_records if r.get("employeeIdentifier") == emp_id]

        # Collect further pages if needed
        next_page = data.get("nextPage")
        page = 2
        while next_page:
            paged = keka_get("/time/leaverequests", {
                "from": s_raw, "to": e_raw, "pageNumber": page, "pageSize": 100
            })
            for r in paged.get("data", []):
                if r.get("employeeIdentifier") == emp_id:
                    records.append(r)
            next_page = paged.get("nextPage")
            page += 1

        if not records:
            return (
                f"You have no leave requests between "
                f"**{s_raw}** and **{e_raw}**."
            )

        lines = []
        for r in records:
            raw_from = r.get("fromDate", "?")
            raw_to   = r.get("toDate", "?")
            status_code = r.get("status", 0)
            emoji, status_label = STATUS_MAP.get(status_code, ("⏳", "Unknown"))

            # Parse ISO dates for display
            try:
                from_disp = datetime.fromisoformat(raw_from.replace("Z", "+00:00")).strftime("%d %b %Y")
                to_disp   = datetime.fromisoformat(raw_to.replace("Z",   "+00:00")).strftime("%d %b %Y")
            except Exception:
                from_disp, to_disp = raw_from, raw_to

            # Leave type from selection array
            selection = r.get("selection", [])
            leave_name = selection[0].get("leaveTypeName", "Leave") if selection else "Leave"
            days_count = sum(s.get("count", 0) for s in selection)

            lines.append(
                f"{emoji} **{leave_name}** | "
                f"{from_disp} → {to_disp} | "
                f"{days_count} day(s) | _{status_label}_"
            )

        return (
            f"Here are your leave requests ({s_raw} to {e_raw}):\n\n"
            + "\n".join(lines)
            + "\n\n_To cancel a leave, say: 'Cancel my leave from YYYY-MM-DD to YYYY-MM-DD'_"
        )

    except Exception as e:
        logger.error(f"[requests] error: {e}")
        return f"Sorry, I couldn't fetch your leave requests. Please try again or contact HR. _(Error: {e})_"


# ---------------------------------------------------------------------------
# Tool: cancel_leave
# ---------------------------------------------------------------------------

def handle_cancel_leave(args: dict, employee_email: str) -> str:
    try:
        from_date = args["from_date"]   # yyyy-MM-dd
        to_date   = args["to_date"]     # yyyy-MM-dd

        emp_id = get_employee_id(employee_email)

        # Convert to Keka GET query format (dd-MM-yyyy)
        from_q = datetime.strptime(from_date, "%Y-%m-%d").strftime("%d-%m-%Y")
        to_q   = datetime.strptime(to_date,   "%Y-%m-%d").strftime("%d-%m-%Y")

        data    = keka_get("/time/leaverequests", {"from": from_q, "to": to_q})
        records = [r for r in data.get("data", []) if r.get("employeeIdentifier") == emp_id]

        if not records:
            return f"I couldn't find any leave request between **{from_date}** and **{to_date}**."

        # Find first active (non-cancelled, non-rejected) record
        target = None
        for r in records:
            status = r.get("status", -1)
            if status not in (2, 3):   # not Rejected (2) and not Cancelled (3)
                target = r
                break

        if not target:
            first = records[0]
            _, label = STATUS_MAP.get(first.get("status", 0), ("", "Unknown"))
            return (
                f"Your leave is already **{label}** — no action needed."
            )

        request_id  = target["id"]
        selection   = target.get("selection", [])
        leave_name  = selection[0].get("leaveTypeName", "Leave") if selection else "Leave"
        raw_from    = target.get("fromDate", "")
        raw_to      = target.get("toDate",   "")

        try:
            from_disp = datetime.fromisoformat(raw_from.replace("Z", "+00:00")).strftime("%d %b %Y")
            to_disp   = datetime.fromisoformat(raw_to.replace("Z",   "+00:00")).strftime("%d %b %Y")
        except Exception:
            from_disp, to_disp = from_date, to_date

        # Attempt cancel via DELETE
        # If Keka uses a different endpoint, update here.
        response = keka_delete(f"/time/leaverequests/{request_id}")
        logger.info(f"[cancel] response: {response}")

        if response.get("succeeded") or response.get("data") is True:
            return (
                f"✅ Your **{leave_name}** from **{from_disp}** to **{to_disp}** "
                "has been cancelled successfully."
            )
        else:
            msg = response.get("message", "Unknown error from Keka.")
            return f"Could not cancel your leave. Reason: _{msg}_"

    except Exception as e:
        logger.error(f"[cancel] error: {e}")
        return f"Sorry, I couldn't cancel your leave. _(Error: {e})_"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_leave_type_id(leave_types: list, requested_name: str) -> str | None:
    """
    Match leave type name against Keka's list.
    Keka uses 'identifier' (not 'id') for leave types.
    Tries exact match first, then partial.
    """
    req = requested_name.lower().strip()

    for lt in leave_types:
        if lt.get("name", "").lower().strip() == req:
            return lt.get("identifier")

    for lt in leave_types:
        if req in lt.get("name", "").lower():
            return lt.get("identifier")

    return None
