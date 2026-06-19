import json
import logging
import os

logger = logging.getLogger(__name__)

LOG_PATH = os.path.join(os.path.dirname(__file__), "attendance_log.json")

_log: dict = {}


def _load():
    global _log
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH, "r", encoding="utf-8") as f:
                _log = json.load(f)
        except Exception as e:
            logger.warning(f"[attendance_log] could not load: {e}")
            _log = {}


def _save():
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(_log, f, indent=2)
    except Exception as e:
        logger.error(f"[attendance_log] could not save: {e}")


_load()

STATUS_LABELS = {
    "office":      "In Office",
    "wfh":         "WFH",
    "leave":       "On Leave",
    "client_site": "Client Site",
}


def log_attendance(email: str, name: str, status: str, timestamp: str):
    key = (email or "").strip().lower()
    if not key:
        return
    _log[key] = {
        "name":      name or key,
        "status":    STATUS_LABELS.get(status, status.replace("_", " ").title()),
        "timestamp": timestamp,
    }
    _save()
    logger.info(f"[attendance_log] {key} → {status}")


def get_attendance_log() -> list:
    """Return list of {email, name, status, timestamp} sorted by name."""
    return sorted(
        [{"email": k, **v} for k, v in _log.items()],
        key=lambda r: r["name"].lower(),
    )
