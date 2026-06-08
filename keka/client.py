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
import json
import time
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

KEKA_BASE_URL       = os.getenv("KEKA_BASE_URL",      "https://caizin.keka.com/api/v1")
KEKA_TOKEN_URL      = os.getenv("KEKA_TOKEN_URL",     "https://login.keka.com/connect/token")
KEKA_CLIENT_ID      = os.getenv("KEKA_CLIENT_ID",     "")
KEKA_CLIENT_SECRET  = os.getenv("KEKA_CLIENT_SECRET", "")
KEKA_API_KEY        = os.getenv("KEKA_API_KEY",       "")
TEST_EMPLOYEE_EMAIL = os.getenv("KEKA_TEST_EMAIL",    "recruiter@caizin.com")
KEKA_MCP_URL        = "https://developers.keka.com/mcp"

_mcp_req_id = 0

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


# ---------------------------------------------------------------------------
# MCP transport helpers
# ---------------------------------------------------------------------------

def _spec_for(path: str) -> str:
    return "Core Hr" if path.startswith("/hris") else "Leave"


def _mcp_request(spec_title: str, method: str, url: str,
                 params: dict = None, body: dict = None) -> dict:
    global _mcp_req_id
    _mcp_req_id += 1

    har = {
        "method": method.lower(),
        "url": url,
        "headers": [
            {"name": "Authorization", "value": f"Bearer {get_access_token()}"},
            {"name": "Content-Type",  "value": "application/json"},
        ],
        "queryString": [
            {"name": k, "value": str(v)} for k, v in (params or {}).items()
        ],
    }
    if body is not None:
        har["postData"] = {"mimeType": "application/json", "text": json.dumps(body)}

    payload = {
        "jsonrpc": "2.0",
        "id": _mcp_req_id,
        "method": "tools/call",
        "params": {
            "name": "execute-request",
            "arguments": {"title": spec_title, "harRequest": har},
        },
    }

    resp = requests.post(
        KEKA_MCP_URL,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        timeout=30,
    )
    resp.raise_for_status()

    for line in resp.text.splitlines():
        if not line.startswith("data:"):
            continue
        rpc = json.loads(line[5:].strip())
        if "error" in rpc:
            raise ValueError(f"MCP error: {rpc['error']}")
        content = rpc.get("result", {}).get("content", [])
        if not content:
            raise ValueError(f"Empty MCP result for {method} {url}")
        raw = content[0].get("text", "")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            if "{" in raw:
                try:
                    return json.loads(raw[raw.find("{"):])
                except json.JSONDecodeError:
                    pass
            return {"succeeded": False, "message": raw, "data": None}

    raise ValueError(f"No SSE data line in MCP response for {method} {url}")


def keka_get(path: str, params: dict = None) -> dict:
    return _mcp_request(_spec_for(path), "GET",
                        f"{KEKA_BASE_URL}{path}", params=params)


def keka_post(path: str, payload: dict) -> dict:
    return _mcp_request(_spec_for(path), "POST",
                        f"{KEKA_BASE_URL}{path}", body=payload)


def keka_delete(path: str) -> dict:
    return _mcp_request(_spec_for(path), "DELETE",
                        f"{KEKA_BASE_URL}{path}")


# ---------------------------------------------------------------------------
# Employee lookup
# ---------------------------------------------------------------------------

def get_employee_id(email: str) -> str:
    """Resolve employee work email → Keka employee UUID via paginated search."""
    key = email.lower().strip()
    if key in _employee_cache:
        return _employee_cache[key]

    page = 1
    while True:
        data = keka_get("/hris/employees", {"pageNumber": page, "pageSize": 100})
        employees = data.get("data", [])
        for emp in employees:
            emp_email = (emp.get("email") or "").lower().strip()
            if emp_email == key:
                _employee_cache[key] = emp["id"]
                logger.info(f"[keka] resolved {email} → {emp['id']}")
                return emp["id"]
        if not data.get("nextPage"):
            break
        page += 1

    raise ValueError(f"No Keka employee found for email: {email}")
