import json
import logging
import os
import re

import anthropic

logger = logging.getLogger(__name__)

CLAUDE_MODEL          = "claude-haiku-4-5"
ANTHROPIC_SECRET_NAME = os.getenv("ANTHROPIC_SECRET_NAME", "caizin/anthropic-api-key")
AWS_REGION            = os.getenv("AWS_REGION")

_anthropic_client = None


def _get_anthropic_api_key() -> str:
    env_key = os.getenv("ANTHROPIC_API_KEY")
    if env_key:
        return env_key
    import boto3
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    response = client.get_secret_value(SecretId=ANTHROPIC_SECRET_NAME)
    data = json.loads(response["SecretString"])
    return data["ANTHROPIC_API_KEY"]


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=_get_anthropic_api_key())
    return _anthropic_client


_WORK_LOC_KEYWORDS = (
    "work location",
    "working from",
    "work status",
    "where is",
    "where am i",
    "working today",
    "in office today",
    "wfh today",
    "work from home today",
    "working remotely",
)


def extract_work_location_query(text: str) -> dict | None:
    """
    Detect if the user is asking about a work location / work status.

    Returns:
      {"target": "self"}                          — asking about themselves
      {"target": "other", "name": "Priya Sharma"} — asking about someone else
      None                                        — not a work location query
    """
    _tl = text.strip().lower()
    if not any(kw in _tl for kw in _WORK_LOC_KEYWORDS):
        return None

    try:
        response = _get_anthropic_client().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=80,
            temperature=0,
            messages=[{
                "role": "user",
                "content": (
                    f'Analyze this message: "{text}"\n\n'
                    "Is the user asking about work location or work status for today?\n"
                    "Rules:\n"
                    '- Asking about themselves → {"target": "self"}\n'
                    '- Asking about someone else → {"target": "other", "name": "<full name as mentioned>"}\n'
                    '- Not a work location question → {"target": null}\n'
                    "Reply with JSON only, no explanation."
                ),
            }],
        )
        raw = next((b.text for b in response.content if b.type == "text"), "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
        logger.info("[extract_work_location_query] raw=%s", raw)
        data = json.loads(raw)
        return data if data.get("target") else None
    except Exception as e:
        logger.info("[extract_work_location_query] failed: %s", e)
        return None