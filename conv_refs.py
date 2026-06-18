import json
import logging
import os
from botbuilder.core import TurnContext

logger = logging.getLogger(__name__)

CONV_REFS_PATH = os.path.join(os.path.dirname(__file__), "conv_refs.json")

# In-memory cache — loaded once at startup, kept in sync on every write
_refs: dict = {}


def _load():
    global _refs
    if os.path.exists(CONV_REFS_PATH):
        try:
            with open(CONV_REFS_PATH, "r", encoding="utf-8") as f:
                _refs = json.load(f)
            logger.info(f"[conv_refs] loaded {len(_refs)} references from disk")
        except Exception as e:
            logger.warning(f"[conv_refs] could not load {CONV_REFS_PATH}: {e}")
            _refs = {}


def _save():
    try:
        with open(CONV_REFS_PATH, "w", encoding="utf-8") as f:
            json.dump(_refs, f, indent=2)
    except Exception as e:
        logger.error(f"[conv_refs] could not save: {e}")


# Load at import time so the scheduler has refs immediately on startup
_load()


def save_ref(email: str, turn_context: TurnContext):
    """Persist the conversation reference for this pilot user."""
    if not email:
        return
    key = email.strip().lower()
    ref = TurnContext.get_conversation_reference(turn_context.activity)
    ref_dict = ref.serialize()
    if _refs.get(key) == ref_dict:
        return  # no change — skip disk write
    _refs[key] = ref_dict
    _save()
    logger.info(f"[conv_refs] saved ref for {key}")


def get_all_refs() -> dict:
    """Return a shallow copy of all stored references."""
    return dict(_refs)
