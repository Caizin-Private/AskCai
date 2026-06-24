"""
keka/dao/employee_dao.py
Raw HRIS employee API access — no business logic, no mapping.
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
