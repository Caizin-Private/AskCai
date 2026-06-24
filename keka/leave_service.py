"""
keka/leave_service.py
Facade over Keka leave APIs.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum

import requests

from keka.client import get_access_token, KEKA_BASE_URL

logger = logging.getLogger(__name__)

_employee_id_cache: dict = {}


@dataclass
class LeaveBalance:
    leave_type_name: str
    leave_type_id: str
    total: float
    used: float
    available: float


@dataclass
class LeaveType:
    id: str
    name: str


@dataclass
class LeaveApplicationResult:
    success: bool
    message: str
    request_id: str = field(default=None)


class SessionType(str, Enum):
    FULL_DAY    = "full_day"
    FIRST_HALF  = "first_half"
    SECOND_HALF = "second_half"


class KekaServiceError(Exception):
    pass


class EmployeeNotFoundError(Exception):
    pass


def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Accept": "application/json",
    }


def _resolve_employee_id(email: str) -> str:
    if email in _employee_id_cache:
        return _employee_id_cache[email]

    page = 1
    while True:
        resp = requests.get(
            f"{KEKA_BASE_URL}/hris/employees",
            headers=_auth_headers(),
            params={"pageNumber": page, "pageSize": 100},
            timeout=10,
        )
        if resp.status_code >= 500:
            raise KekaServiceError(f"Keka employee lookup failed: {resp.status_code}")

        body = resp.json()
        for emp in body.get("data", []):
            if (emp.get("email") or "").lower() == email.lower():
                emp_id = emp["id"]
                _employee_id_cache[email] = emp_id
                logger.info("[keka] resolved employee id for %s", email)
                return emp_id

        if page >= (body.get("totalPages") or 1):
            break
        page += 1

    raise EmployeeNotFoundError(f"No employee found for {email}")


class KekaLeaveService:

    async def get_leave_types(self) -> list:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_leave_types)

    def _sync_get_leave_types(self) -> list:
        resp = requests.get(
            f"{KEKA_BASE_URL}/time/leavetypes",
            headers=_auth_headers(),
            timeout=10,
        )
        if resp.status_code >= 500:
            raise KekaServiceError(f"Keka leave types fetch failed: {resp.status_code}")
        data = resp.json().get("data", [])
        return [LeaveType(id=lt["identifier"], name=lt["name"]) for lt in data]

    async def get_leave_balance(self, email: str) -> tuple:
        """Returns (employee_name, list[LeaveBalance])."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_leave_balance, email)

    def _sync_get_leave_balance(self, email: str) -> tuple:
        emp_id = _resolve_employee_id(email)
        resp = requests.get(
            f"{KEKA_BASE_URL}/time/leavebalance",
            headers=_auth_headers(),
            params={"employeeIds": emp_id},
            timeout=10,
        )
        if resp.status_code >= 500:
            raise KekaServiceError(f"Keka leave balance fetch failed: {resp.status_code}")
        data = resp.json().get("data", [])
        if not data:
            return ("", [])
        rec = data[0]
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

        headers = _auth_headers()
        headers["Content-Type"] = "application/json"

        resp = requests.post(
            f"{KEKA_BASE_URL}/time/leaverequests",
            headers=headers,
            json=payload,
            timeout=10,
        )

        if resp.status_code >= 500:
            raise KekaServiceError(f"Keka apply leave failed: {resp.status_code}")

        if resp.status_code >= 400:
            try:
                msg = resp.json().get("message") or resp.text
            except Exception:
                msg = resp.text
            return LeaveApplicationResult(success=False, message=msg)

        try:
            data = resp.json().get("data", {})
            req_id = str(data.get("id", "")) if data else None
        except Exception:
            req_id = None

        return LeaveApplicationResult(success=True, message="Leave applied successfully.", request_id=req_id)


leave_service = KekaLeaveService()
