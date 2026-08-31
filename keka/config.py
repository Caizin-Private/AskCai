"""
keka/config.py
Non-secret configuration for the Keka integration: timesheet policy and cache TTLs.

**This module does not read credentials.** Everything about connecting to Keka —
the three secrets and the two URLs — lives in keka/client.py, read from the
environment via os.getenv exactly as it always was:

    KEKA_CLIENT_ID  KEKA_CLIENT_SECRET  KEKA_API_KEY  KEKA_BASE_URL  KEKA_TOKEN_URL

Nothing here re-exports them either — callers that need a URL or a credential check
import keka.client, so there is exactly one place to look. If a secret appears in
config/keka.yaml it is dropped and a warning is logged, because a credential in a
file on disk is one `git add -f` from being committed.

What this file DOES resolve — timesheet policy and cache TTLs:
  1. environment variable
  2. config/keka.yaml
  3. built-in default
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.getenv(
    "KEKA_CONFIG_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "keka.yaml"),
)

# Names that must never be honoured from the YAML file.
_SECRET_KEYS = ("client_id", "client_secret", "api_key")

_DEFAULTS = {
    "keka": {
        "cache_ttl": {
            "employee": 3600,
            "projects": 900,
            "allocations": 900,
            "holidays": 86400,
            "leave": 300,
            "time_entries": 60,
        },
    },
    "timesheet": {
        "daily_cap_hours": 8.0,
        "working_days": ["mon", "tue", "wed", "thu", "fri"],
        "week_starts_on": "mon",
        "timezone": "Asia/Kolkata",
        "treat_floater_holidays_as_closed": False,
    },
}

_lock = threading.Lock()
_loaded = None


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def _load_file() -> dict:
    if not os.path.isfile(_CONFIG_PATH):
        logger.info("[keka.config] no %s — using env vars and defaults", _CONFIG_PATH)
        return {}
    try:
        import yaml
    except ImportError:
        logger.warning("[keka.config] pyyaml not installed — ignoring %s", _CONFIG_PATH)
        return {}
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            logger.warning("[keka.config] %s is not a mapping — ignoring", _CONFIG_PATH)
            return {}
        # Strip any secret someone added to the file, and say so loudly.
        stray = [k for k in _SECRET_KEYS if (data.get("keka") or {}).get(k)]
        if stray:
            logger.warning(
                "[keka.config] IGNORING %s in %s — credentials are read from the "
                "environment only (KEKA_CLIENT_ID / KEKA_CLIENT_SECRET / KEKA_API_KEY). "
                "Remove them from the file.",
                ", ".join(stray), _CONFIG_PATH,
            )
        for k in _SECRET_KEYS:
            (data.get("keka") or {}).pop(k, None)

        logger.info("[keka.config] loaded %s", _CONFIG_PATH)
        return data
    except Exception as exc:
        logger.error("[keka.config] could not read %s: %s", _CONFIG_PATH, exc)
        return {}


def _cfg() -> dict:
    global _loaded
    if _loaded is None:
        with _lock:
            if _loaded is None:
                _loaded = _deep_merge(_DEFAULTS, _load_file())
    return _loaded


def reload() -> None:
    """Drop the cached config so the next read re-reads the file."""
    global _loaded
    with _lock:
        _loaded = None


# ── Keka credentials ──────────────────────────────────────────────────────────

def cache_ttl(name: str) -> int:
    env = os.getenv("KEKA_CACHE_TTL_" + name.upper())
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return int(_cfg()["keka"]["cache_ttl"].get(name, 300))


# ── Timesheet policy ──────────────────────────────────────────────────────────

def daily_cap_hours() -> float:
    env = os.getenv("TIMESHEET_DAILY_CAP_HOURS")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return float(_cfg()["timesheet"]["daily_cap_hours"])


def working_days() -> list:
    env = os.getenv("TIMESHEET_WORKING_DAYS")
    if env:
        return [d.strip().lower() for d in env.split(",") if d.strip()]
    return list(_cfg()["timesheet"]["working_days"])


def week_starts_on() -> str:
    return (os.getenv("TIMESHEET_WEEK_STARTS_ON") or _cfg()["timesheet"]["week_starts_on"]).lower()


def timezone_name() -> str:
    return os.getenv("TIMESHEET_TIMEZONE") or _cfg()["timesheet"]["timezone"]


def floaters_are_closed() -> bool:
    env = os.getenv("TIMESHEET_FLOATERS_CLOSED")
    if env is not None:
        return env not in ("0", "false", "False", "")
    return bool(_cfg()["timesheet"]["treat_floater_holidays_as_closed"])
