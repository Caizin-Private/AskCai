"""
keka/dao/leave_dao.py
Raw Keka leave API access — no business logic, no mapping.
"""

import logging

import requests

from keka.client import get_access_token, KEKA_BASE_URL
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
