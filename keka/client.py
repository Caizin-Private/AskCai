"""
keka/client.py
Keka OAuth2 token management.

Auth flow:
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

KEKA_TOKEN_URL      = os.getenv("KEKA_TOKEN_URL",     "https://login.keka.com/connect/token")
KEKA_BASE_URL       = os.getenv("KEKA_BASE_URL",      "https://caizin.keka.com/api/v1")
KEKA_CLIENT_ID      = os.getenv("KEKA_CLIENT_ID",     "")
KEKA_CLIENT_SECRET  = os.getenv("KEKA_CLIENT_SECRET", "")
KEKA_API_KEY        = os.getenv("KEKA_API_KEY",       "")
KEKA_MCP_URL        = "https://developers.keka.com/mcp"
# TEST_EMPLOYEE_EMAIL = os.getenv("KEKA_TEST_EMAIL",    "recruiter@caizin.com")

_token_cache = {"access_token": None, "expires_at": 0.0}


def get_access_token() -> str:
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    logger.info("[keka] fetching token with client_id=%s api_key=%s",
                KEKA_CLIENT_ID[:4] + "…" if KEKA_CLIENT_ID else "MISSING",
                KEKA_API_KEY[:4]   + "…" if KEKA_API_KEY   else "MISSING")
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
