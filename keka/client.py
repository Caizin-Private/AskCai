"""
keka/client.py
Keka API OAuth2 token management and HTTP helpers.

Auth flow (from Postman collection):
  POST https://login.keka.com/connect/token
  Body (urlencoded): grant_type=kekaapi, scope=kekaapi,
                     client_id, client_secret, api_key
  Token expires_in: 86400 seconds (24 hours)
"""

import os
import time
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

KEKA_BASE_URL      = os.getenv("KEKA_BASE_URL",      "https://caizin.keka.com/api/v1")
KEKA_TOKEN_URL     = os.getenv("KEKA_TOKEN_URL",      "https://login.keka.com/connect/token")
KEKA_CLIENT_ID     = os.getenv("KEKA_CLIENT_ID",      "")
KEKA_CLIENT_SECRET = os.getenv("KEKA_CLIENT_SECRET",  "")
KEKA_API_KEY       = os.getenv("KEKA_API_KEY",        "")
TEST_EMPLOYEE_EMAIL = os.getenv("KEKA_TEST_EMAIL", "recruiter@caizin.com")

_token_cache    = {"access_token": None, "expires_at": 0.0}
_employee_cache = {}   # email (lower) → employee UUID


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    resp = requests.post(
        KEKA_TOKEN_URL,
        data={
            "grant_type":    "kekaapi",
            "scope":         "kekaapi",
            "client_id":     KEKA_CLIENT_ID,
            "client_secret": KEKA_CLIENT_SECRET,
            "api_key":       KEKA_API_KEY,
        },
        headers={
            "Accept":       "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent":   "PostmanRuntime/7.43.0",
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"]   = now + data.get("expires_in", 86400)
    logger.info("[keka] access token refreshed")
    return _token_cache["access_token"]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type":  "application/json",
    }


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def keka_get(path: str, params: dict = None) -> dict:
    resp = requests.get(
        f"{KEKA_BASE_URL}{path}",
        headers=_headers(),
        params=params or {},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def keka_post(path: str, payload: dict) -> dict:
    resp = requests.post(
        f"{KEKA_BASE_URL}{path}",
        headers=_headers(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def keka_delete(path: str) -> dict:
    resp = requests.delete(
        f"{KEKA_BASE_URL}{path}",
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Employee lookup
# ---------------------------------------------------------------------------

def get_employee_id(email: str) -> str:
    """Resolve employee work email → Keka employee UUID via direct search."""
    key = email.lower().strip()
    if key in _employee_cache:
        return _employee_cache[key]

    data = keka_post("/hris/employees/search", {"workEmail": key})
    emp = data.get("data")
    if not emp or not emp.get("id"):
        raise ValueError(f"No Keka employee found for email: {email}")

    _employee_cache[key] = emp["id"]
    logger.info(f"[keka] resolved {email} → {emp['id']}")
    return emp["id"]
