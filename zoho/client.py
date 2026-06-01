"""
zoho/client.py
Shared Zoho OAuth2 token management + HTTP helpers.
"""

import json
import os
import time

import requests

ZOHO_CLIENT_ID     = os.getenv("ZOHO_CLIENT_ID", "")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET", "")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN", "")
ZOHO_ACCOUNTS_URL  = os.getenv("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in/oauth/v2/token")
ZOHO_BASE_URL      = os.getenv("ZOHO_BASE_URL",     "https://people.zoho.in/people/api")

_token_cache = {"access_token": None, "expires_at": 0.0}


def get_access_token() -> str:
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    resp = requests.post(
        ZOHO_ACCOUNTS_URL,
        data={
            "grant_type":    "refresh_token",
            "refresh_token": ZOHO_REFRESH_TOKEN,
            "client_id":     ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"]   = now + data.get("expires_in", 3600)
    return _token_cache["access_token"]


def _headers() -> dict:
    return {"Authorization": f"Zoho-oauthtoken {get_access_token()}"}


def zoho_get(path: str, params: dict = None) -> dict:
    resp = requests.get(
        f"{ZOHO_BASE_URL}{path}",
        headers=_headers(),
        params=params or {},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def zoho_post_form(path: str, input_data: dict) -> dict:
    resp = requests.post(
        f"{ZOHO_BASE_URL}{path}",
        headers=_headers(),
        data={"inputData": json.dumps(input_data)},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_employee_erecno(email: str) -> str:
    """
    Resolve employee email → recordId.
    /forms/P_EmployeeView/records returns a raw LIST (not wrapped in response{}).
    """
    data = zoho_get(
        "/forms/P_EmployeeView/records",
        {"searchColumn": "EMPLOYEEMAILALIAS", "searchValue": email},
    )

    # Response is a plain list: [ { "recordId": "...", ... } ]
    if isinstance(data, list):
        if not data:
            raise ValueError(f"No employee found for email: {email}")
        return data[0]["recordId"]

    # Fallback: wrapped format just in case
    if isinstance(data, dict):
        results = data.get("response", {}).get("result", [])
        if not results:
            raise ValueError(f"No employee found for email: {email}")
        return results[0]["recordId"]

    raise ValueError(f"Unexpected response format from employee lookup: {type(data)}")


# V2 API base (different base URL for newer endpoints)
ZOHO_BASE_URL_V2 = os.getenv("ZOHO_BASE_URL_V2", "https://people.zoho.in/api/v2")


def zoho_get_v2(path: str, params: dict = None) -> dict:
    """GET request to Zoho People V2 API."""
    resp = requests.get(
        f"{ZOHO_BASE_URL_V2}{path}",
        headers=_headers(),
        params=params or {},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def zoho_patch_v2(path: str, params: dict = None) -> dict:
    """PATCH request for Zoho V2 - uses params for 'cancel' endpoint."""
    headers = _headers().copy()
    headers["Accept"] = "application/json"
    # Note: We don't necessarily need Content-Type if there's no body, 
    # but keeping it doesn't hurt.

    resp = requests.patch(
        f"{ZOHO_BASE_URL_V2}{path}",
        headers=headers,
        params=params or {}, # Pass data as query params (?reason=...)
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()