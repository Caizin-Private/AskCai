"""
keka/dao/employee_dao.py
Raw HRIS employee API access — no business logic, no mapping.
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


def find_by_email(email: str) -> dict | None:
    """
    Paginate through /hris/employees until a record matching email is found.
    Returns the raw employee dict, or None if not found.
    Raises KekaServiceError on HTTP 5xx.
    """
    page = 1
    while True:
        resp = requests.get(
            f"{KEKA_BASE_URL}/hris/employees",
            headers=_headers(),
            params={"pageNumber": page, "pageSize": 100},
            timeout=10,
        )
        if resp.status_code >= 500:
            raise KekaServiceError(f"Employee lookup failed: HTTP {resp.status_code}")

        body = resp.json()
        for emp in body.get("data", []):
            if (emp.get("email") or "").lower() == email.lower():
                logger.info("[employee_dao] found employee for %s on page %d", email, page)
                return emp

        if page >= (body.get("totalPages") or 1):
            return None
        page += 1


def find_by_email_indexed(email: str) -> dict | None:
    """
    Resolve an employee from an email using Keka's own search, then verify the match
    exactly.

    /hris/employees has no email filter, but it does take `searchKey` (minimum 3
    characters), which is far cheaper than find_by_email()'s walk through every page
    of the directory. searchKey is a fuzzy text match, so the exact email comparison
    below is what actually decides — searchKey only narrows the page.

    Returns the raw employee dict (id, email, displayName, holidayCalendarId,
    weeklyOffPolicyInfo, shiftPolicyInfo, ...) or None.
    """
    email = (email or "").strip().lower()
    if not email:
        return None

    def _lookup():
        rows = get_all(
            "/hris/employees",
            {"searchKey": email, "employmentStatus": "Working"},
            what="GET /hris/employees",
        )
        for emp in rows:
            if (emp.get("email") or "").strip().lower() == email:
                return emp
        # searchKey did not surface it (aliases, formatting). Fall back to the
        # exhaustive walk rather than reporting the employee as missing.
        logger.info("[employee_dao] searchKey missed %s — falling back to full scan", email)
        return find_by_email(email)

    return cached("employee", email, _lookup)
