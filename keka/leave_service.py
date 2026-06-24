"""
keka/leave_service.py
Facade over Keka leave operations.
Orchestrates DAOs and maps raw API responses to typed models.
"""

import asyncio
import logging

from keka.dao.employee_dao import find_by_email
from keka.dao.leave_dao import fetch_leave_types, fetch_leave_balance, post_leave_request
from keka.models import (
    LeaveBalance,
    LeaveType,
    LeaveApplicationResult,
    SessionType,
    EmployeeNotFoundError,
)

logger = logging.getLogger(__name__)

_employee_id_cache: dict = {}


def _resolve_employee_id(email: str) -> str:
    if email in _employee_id_cache:
        return _employee_id_cache[email]

    emp = find_by_email(email)
    if not emp:
        raise EmployeeNotFoundError(f"No employee found for {email}")

    emp_id = emp["id"]
    _employee_id_cache[email] = emp_id
    logger.info("[leave_service] resolved employee id for %s", email)
    return emp_id


class KekaLeaveService:

    async def get_leave_types(self) -> list:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_leave_types)

    def _sync_get_leave_types(self) -> list:
        raw = fetch_leave_types()
        return [LeaveType(id=lt["identifier"], name=lt["name"]) for lt in raw]

    async def get_leave_balance(self, email: str) -> tuple:
        """Returns (employee_name, list[LeaveBalance])."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_leave_balance, email)

    def _sync_get_leave_balance(self, email: str) -> tuple:
        emp_id = _resolve_employee_id(email)
        rec = fetch_leave_balance(emp_id)
        if not rec:
            return ("", [])

        employee_name = rec.get("employeeName", "")
        balances = [
            LeaveBalance(
                leave_type_name=lb.get("leaveTypeName", ""),
                leave_type_id=lb.get("leaveTypeId", ""),
                total=float(lb.get("accruedAmount", 0)),
                used=float(lb.get("consumedAmount", 0)),
                available=float(lb.get("availableBalance", 0)),
            )
            for lb in rec.get("leaveBalance", [])
        ]
        return (employee_name, balances)

    async def apply_leave(
        self,
        email: str,
        leave_type_id: str,
        from_date: str,
        to_date: str,
        session_type: SessionType,
        reason: str,
    ) -> LeaveApplicationResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self._sync_apply_leave,
            email, leave_type_id, from_date, to_date, session_type, reason,
        )

    def _sync_apply_leave(
        self,
        email: str,
        leave_type_id: str,
        from_date: str,
        to_date: str,
        session_type: SessionType,
        reason: str,
    ) -> LeaveApplicationResult:
        emp_id = _resolve_employee_id(email)

        if session_type == SessionType.SECOND_HALF:
            from_session, to_session = 1, 1
        elif session_type == SessionType.FIRST_HALF:
            from_session, to_session = 0, 0
        else:
            from_session, to_session = 0, 1

        payload = {
            "employeeId":  emp_id,
            "leaveTypeId": leave_type_id,
            "from":        from_date,
            "to":          to_date,
            "fromSession": from_session,
            "toSession":   to_session,
            "note":        reason,
        }

        result = post_leave_request(payload)

        if not result["ok"]:
            return LeaveApplicationResult(success=False, message=result["message"])

        return LeaveApplicationResult(
            success=True,
            message="Leave applied successfully.",
            request_id=result.get("request_id"),
        )


leave_service = KekaLeaveService()
