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

# Environment only. load_dotenv() above means a local .env works, which is where
# credentials belong on a developer machine; production uses real app settings.
# config/keka.yaml is deliberately NOT consulted for any of these.
KEKA_TOKEN_URL      = os.getenv("KEKA_TOKEN_URL",     "https://login.keka.com/connect/token")
KEKA_BASE_URL       = os.getenv("KEKA_BASE_URL",      "https://caizin.keka.com/api/v1")
KEKA_CLIENT_ID      = os.getenv("KEKA_CLIENT_ID",     "")
KEKA_CLIENT_SECRET  = os.getenv("KEKA_CLIENT_SECRET", "")
KEKA_API_KEY        = os.getenv("KEKA_API_KEY",       "")
KEKA_MCP_URL        = "https://developers.keka.com/mcp"
# TEST_EMPLOYEE_EMAIL = os.getenv("KEKA_TEST_EMAIL",    "recruiter@caizin.com")

_SECRET_ENV = ("KEKA_CLIENT_ID", "KEKA_CLIENT_SECRET", "KEKA_API_KEY")


def base_url() -> str:
    """Current base URL, read live so a late env change is honoured."""
    return os.getenv("KEKA_BASE_URL", KEKA_BASE_URL).rstrip("/")


def token_url() -> str:
    return os.getenv("KEKA_TOKEN_URL", KEKA_TOKEN_URL)


def missing_secrets() -> list:
    """Which credential environment variables are unset."""
    return [name for name in _SECRET_ENV if not (os.getenv(name) or "").strip()]


def is_configured() -> bool:
    """True when all three credentials are present in the environment."""
    return not missing_secrets()

_token_cache = {"access_token": None, "expires_at": 0.0}


def get_access_token() -> str:
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    _cid = os.getenv("KEKA_CLIENT_ID", KEKA_CLIENT_ID)
    _key = os.getenv("KEKA_API_KEY", KEKA_API_KEY)
    logger.info("[keka] fetching token with client_id=%s api_key=%s",
                _cid[:4] + "…" if _cid else "MISSING",
                _key[:4] + "…" if _key else "MISSING")
    resp = requests.post(
        token_url(),
        data={
            "grant_type":    "kekaapi",
            "scope":         "kekaapi",
            "client_id":     os.getenv("KEKA_CLIENT_ID", KEKA_CLIENT_ID),
            "client_secret": os.getenv("KEKA_CLIENT_SECRET", KEKA_CLIENT_SECRET),
            "api_key":       os.getenv("KEKA_API_KEY", KEKA_API_KEY),
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
