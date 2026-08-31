"""
keka/dao/leave_dao.py
Raw Keka leave API access — no business logic, no mapping.
"""

import logging

import requests

from keka.client import get_access_token, KEKA_BASE_URL
from keka.dao._http import cached, get_all
from keka.models import KekaServiceError

logger = logging.getLogger(__name__)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Accept": "application/json",
    }


def fetch_leave_types() -> list:
    """
    GET /time/leavetypes
    Returns raw list of leave type dicts.
    Raises KekaServiceError on HTTP 5xx.
    """
    resp = requests.get(
        f"{KEKA_BASE_URL}/time/leavetypes",
        headers=_headers(),
        timeout=10,
    )
    if resp.status_code >= 500:
        raise KekaServiceError(f"Leave types fetch failed: HTTP {resp.status_code}")
    return resp.json().get("data", [])


def fetch_leave_balance(employee_id: str) -> dict | None:
    """
    GET /time/leavebalance?employeeIds={employee_id}
    Returns the raw balance record dict (with employeeName + leaveBalance[]), or None.
    Raises KekaServiceError on HTTP 5xx.
    """
    resp = requests.get(
        f"{KEKA_BASE_URL}/time/leavebalance",
        headers=_headers(),
        params={"employeeIds": employee_id},
        timeout=10,
    )
    if resp.status_code >= 500:
        raise KekaServiceError(f"Leave balance fetch failed: HTTP {resp.status_code}")
    data = resp.json().get("data", [])
    return data[0] if data else None


def post_leave_request(payload: dict) -> dict:
    """
    POST /time/leaverequests
    Returns {"ok": True, "request_id": str} on success,
            {"ok": False, "message": str} on 4xx.
    Raises KekaServiceError on HTTP 5xx.
    """
    h = _headers()
    h["Content-Type"] = "application/json"

    logger.info("[leave_dao] POST /time/leaverequests payload: %s", payload)

    resp = requests.post(
        f"{KEKA_BASE_URL}/time/leaverequests",
        headers=h,
        json=payload,
        timeout=10,
    )

    logger.info("[leave_dao] Keka response %d: %s", resp.status_code, resp.text[:500])

    if resp.status_code >= 500:
        raise KekaServiceError(f"Apply leave failed: HTTP {resp.status_code} — {resp.text[:200]}")

    if resp.status_code >= 400:
        try:
            body = resp.json()
            errors = body.get("errors") or []
            message = errors[0] if errors else (body.get("message") or resp.text)
        except Exception:
            message = resp.text
        return {"ok": False, "message": message}

    try:
        data = resp.json().get("data", {})
        request_id = str(data.get("id", "")) if data else None
    except Exception:
        request_id = None

    return {"ok": True, "request_id": request_id}


def fetch_leave_requests(employee_id: str, from_date: str, to_date: str) -> list:
    """
    GET /time/leaverequests?employeeIds=...&from=...&to=...

    Used by the timesheet dashboard to mark leave days. Returns raw rows:
      {id, employeeIdentifier, employeeNumber, fromDate, toDate,
       fromSession, toSession, requestedOn, note, cancelRejectReason,
       status, selection: [{leaveTypeIdentifier, leaveTypeName, count, duration}],
       lastActionTakenOn}

    status  0 Pending · 1 Approved · 2 Rejected · 3 Cancelled · 4 InApprovalProcess
    session 0 first half · 1 second half — a full day is fromSession 0 to toSession 1

    Keka rejects a from/to span wider than 90 days. Callers filter to approved
    requests; this returns every status so the caller decides.
    """
    return cached(
        "leave",
        f"{employee_id}|{from_date}|{to_date}",
        lambda: get_all(
            "/time/leaverequests",
            {
                "employeeIds": employee_id,
                "from": f"{from_date}T00:00:00",
                "to": f"{to_date}T23:59:59",
            },
            what="GET /time/leaverequests",
        ),
    )
