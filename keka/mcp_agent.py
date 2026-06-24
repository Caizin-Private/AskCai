"""
keka/mcp_agent.py

Two-phase MCP integration with the Keka HRMS MCP server.

Phase 1 — search-endpoints + get-endpoint
  Claude discovers the correct spec title, path, HTTP method, and query params.

Phase 2 — execute-request only
  Python injects the Bearer token and the resolved employee UUID into the user
  message. Claude calls execute-request with the pre-built URL.

Root causes addressed:
  RC-1  search-endpoints + execute-request together exceed the 200k-token context
        limit because search-endpoints tool results embed the full OpenAPI spec.
        → Isolated into separate phases so only one tool schema is loaded per call.

  RC-2  execute-request returns 401 because authorization_token authenticates
        Claude→MCP server, NOT MCP server→Keka API.
        → Bearer token injected explicitly in the HAR headers array.

  RC-3  Routing employee UUID lookup through execute-request requires knowing the
        correct spec title ("Core Hr") for /hris/employees — discovered via an
        extra search-endpoints call that caused timeouts and token bloat.
        → UUID lookup stays in Python (get_employee_id already has a process cache).
"""

import asyncio
import json
import logging
import re
from datetime import date

import anthropic
from keka.client import get_access_token, get_employee_id, get_leave_type_id, KEKA_BASE_URL, KEKA_MCP_URL

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MCP toolsets — each phase gets only the tools it needs (RC-1)
# ---------------------------------------------------------------------------

_TOOLSET_SEARCH = {
    "type": "mcp_toolset",
    "mcp_server_name": "keka",
    "default_config": {"enabled": False},
    "configs": {
        "search-endpoints": {"enabled": True},
        "get-endpoint":     {"enabled": True},
    },
}

_TOOLSET_EXEC = {
    "type": "mcp_toolset",
    "mcp_server_name": "keka",
    "default_config": {"enabled": False},
    "configs": {
        "execute-request": {"enabled": True},
    },
}


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_PHASE1_SYSTEM = """\
You are an API discovery assistant for Keka HRMS.
Step 1: Call search-endpoints ONCE with a focused keyword to find the right path.
Step 2: Call get-endpoint for that exact path to get its full spec including parameters and request body.
After both tool calls, reply with ONLY a single JSON object on one line — no markdown, no explanation:
{"title": "<spec title>", "path": "<endpoint path>", "method": "<http method>", "params": {"<param_name>": "<description>"}, "body": {"<field_name>": "<description>"}}
For "params", include only query parameters relevant to filtering by employee or user. If none, use {}.
For "body", include all request body fields from the schema. If none (e.g. GET), use {}.\
"""

_PHASE2_SYSTEM = """\
You are Caizin's internal HR assistant.
Today's date: {today}
Employee email: {email}
Employee ID: {employee_id}
Keka API base URL: {base_url}

Use execute-request to fetch live HR data from Keka and answer the user's question.
The employee's ID is provided above — use it directly as a query parameter when calling data endpoints.
Do NOT call /hris/employees to look up the ID — it is already known.
IMPORTANT: The user message contains an Authorization header — include it in EVERY execute-request call you make.
Report only what the API actually returns. Never invent data.
Respond in clear, friendly Markdown.\
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_endpoint(text: str) -> dict | None:
    """Extract the {title, path, method, params, body} JSON from Phase 1's text reply."""
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
        if all(k in data for k in ("title", "path", "method")):
            data.setdefault("params", {})
            data.setdefault("body", {})
            return data
    except json.JSONDecodeError:
        pass
    return None


def _first_text(response) -> str:
    """Return the last text block from an Anthropic response (skips tool-call blocks)."""
    texts = [b.text for b in response.content if getattr(b, "text", None)]
    return texts[-1] if texts else ""


def _log_content_blocks(prefix: str, blocks) -> None:
    """Log every content block returned by Claude for debugging."""
    for b in blocks:
        if b.type == "mcp_tool_use":
            logger.info("[mcp_agent] %s tool_use: tool=%s input=%s", prefix, b.name, json.dumps(b.input))
        elif b.type == "mcp_tool_result":
            content = (
                b.content if isinstance(b.content, str)
                else [(c.text if hasattr(c, "text") else str(c)) for c in b.content]
            )
            logger.info("[mcp_agent] %s tool_result: %s", prefix, content)
        else:
            logger.info("[mcp_agent] %s block type=%s", prefix, b.type)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def ask_keka_mcp(question: str, employee_email: str, api_key: str) -> str:
    """Answer a live HR query using the two-phase Keka MCP approach."""

    # -- Auth token ----------------------------------------------------------
    try:
        token = await asyncio.to_thread(get_access_token)
    except Exception as exc:
        logger.error("[mcp_agent] token fetch failed: %s", exc)
        return f"Sorry, I couldn't connect to Keka right now. _(Error: {exc})_"

    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=90.0)
    mcp_server = {
        "type":                "url",
        "url":                 KEKA_MCP_URL,
        "name":                "keka",
        "authorization_token": token,       # authenticates Claude → MCP server
    }

    # -- Phase 1: endpoint discovery -----------------------------------------
    logger.info("[mcp_agent] Phase 1 — endpoint discovery")
    endpoint = None
    try:
        p1 = await client.beta.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=_PHASE1_SYSTEM,
            messages=[{"role": "user", "content": question}],
            mcp_servers=[mcp_server],
            tools=[_TOOLSET_SEARCH],
            betas=["mcp-client-2025-11-20"],
        )
        logger.info("[mcp_agent] Phase 1 stop_reason=%s", p1.stop_reason)
        _log_content_blocks("Phase 1", p1.content)

        p1_text  = _first_text(p1)
        endpoint = _extract_endpoint(p1_text)

        logger.info("[mcp_agent] Phase 1 response: %s", p1_text)
        if endpoint:
            logger.info("[mcp_agent] Phase 1 endpoint: %s", endpoint)
        else:
            logger.warning("[mcp_agent] Phase 1 returned no parseable endpoint")
    except Exception as exc:
        logger.warning("[mcp_agent] Phase 1 failed (%s) — no endpoint hint for Phase 2", exc)

    # -- Employee ID lookup (Python, cached — see RC-3) ----------------------
    employee_id = await asyncio.to_thread(get_employee_id, employee_email)
    if employee_id:
        logger.info("[mcp_agent] resolved employee_id=%s for %s", employee_id, employee_email)
    else:
        logger.warning("[mcp_agent] could not resolve employee_id for %s", employee_email)

    # -- Phase 2: execution --------------------------------------------------
    logger.info("[mcp_agent] Phase 2 — execution (endpoint=%s)", endpoint)

    system2 = _PHASE2_SYSTEM.format(
        today=date.today().isoformat(),
        email=employee_email,
        employee_id=employee_id or "unknown — ask the user for their employee ID",
        base_url=KEKA_BASE_URL,
    )
    auth_header = f"Authorization: Bearer {token}"   # injected into HAR (RC-2)

    if endpoint and employee_id:
        params     = endpoint.get("params") or {}
        id_param   = next((k for k in params if "employee" in k.lower()), "employeeId")
        target_url = f"{KEKA_BASE_URL}{endpoint['path']}?{id_param}={employee_id}"
        logger.info("[mcp_agent] Phase 2 target_url=%s (param=%s)", target_url, id_param)
        user_content = (
            f"{question}\n\n"
            f"Call execute-request: spec='{endpoint['title']}', "
            f"{endpoint['method'].upper()} {target_url}\n"
            f"Include this header in the HAR headers array: {auth_header}"
        )
    elif endpoint:
        user_content = (
            f"{question}\n\n"
            f"Use spec='{endpoint['title']}', "
            f"{endpoint['method'].upper()} {KEKA_BASE_URL}{endpoint['path']}.\n"
            f"Include this header in the HAR headers array of every execute-request call: {auth_header}"
        )
    else:
        return "I couldn't identify the right HR data source for your question. Please try rephrasing or contact HR."

    try:
        p2 = await client.beta.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=system2,
            messages=[{"role": "user", "content": user_content}],
            mcp_servers=[mcp_server],
            tools=[_TOOLSET_EXEC],
            betas=["mcp-client-2025-11-20"],
        )
        logger.info("[mcp_agent] Phase 2 stop_reason=%s", p2.stop_reason)
        _log_content_blocks("Phase 2", p2.content)

        text = _first_text(p2)
        return text or "I couldn't complete your HR request. Please try again or contact HR."
    except Exception as exc:
        logger.error("[mcp_agent] Phase 2 failed: %s", exc)
        return f"Sorry, I couldn't complete your HR request. _(Error: {exc})_"


# ---------------------------------------------------------------------------
# Apply-leave constants
# ---------------------------------------------------------------------------

_SESSION_MAP = {
    "full":        (1, 2),  # fromSession=FirstHalf start, toSession=SecondHalf end
    "first_half":  (1, 1),
    "second_half": (2, 2),
}

_PHASE2_APPLY_SYSTEM = """\
You are Caizin's internal HR assistant.
Today's date: {today}
Employee email: {email}
Employee ID: {employee_id}
Keka API base URL: {base_url}

Your only job is to call execute-request ONCE with the POST body the user provides.
Do NOT call any other endpoints. Do NOT look up leave types or employee IDs — they are already resolved.
For the HAR postData: set mimeType to "application/json" and text to a JSON string of the body fields.
Include the Authorization header in every execute-request call.
After the call, report the result clearly — confirm success or explain any API error.\
"""


# ---------------------------------------------------------------------------
# Apply-leave entry point
# ---------------------------------------------------------------------------

async def ask_keka_mcp_apply_leave(
    leave_params: dict,   # keys: leave_type, from_date, to_date, session, reason
    employee_email: str,
    api_key: str,
) -> str:
    """Apply leave via the two-phase Keka MCP approach."""

    try:
        token = await asyncio.to_thread(get_access_token)
    except Exception as exc:
        logger.error("[mcp_agent] token fetch failed: %s", exc)
        return f"Sorry, I couldn't connect to Keka right now. _(Error: {exc})_"

    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=90.0)
    mcp_server = {
        "type":                "url",
        "url":                 KEKA_MCP_URL,
        "name":                "keka",
        "authorization_token": token,
    }

    # -- Phase 1: discover POST /time/leaverequests body schema --------------
    logger.info("[mcp_agent] ApplyLeave Phase 1 — endpoint discovery")
    endpoint = None
    try:
        p1 = await client.beta.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=_PHASE1_SYSTEM,
            messages=[{"role": "user", "content": "Find the endpoint to submit a new leave request (POST leave request / apply leave)"}],
            mcp_servers=[mcp_server],
            tools=[_TOOLSET_SEARCH],
            betas=["mcp-client-2025-11-20"],
        )
        _log_content_blocks("ApplyLeave Phase 1", p1.content)
        endpoint = _extract_endpoint(_first_text(p1))
        logger.info("[mcp_agent] ApplyLeave Phase 1 endpoint: %s", endpoint)
    except Exception as exc:
        logger.warning("[mcp_agent] ApplyLeave Phase 1 failed: %s", exc)

    if not endpoint:
        return "I couldn't identify the leave request endpoint. Please contact HR."

    # -- Resolve employee ID and leave type UUID (Python, cached) ------------
    employee_id   = await asyncio.to_thread(get_employee_id, employee_email)
    leave_type_id = await asyncio.to_thread(get_leave_type_id, leave_params["leave_type"])

    if not employee_id:
        return "I couldn't identify your employee profile. Please contact HR."
    if not leave_type_id:
        return f"Leave type '{leave_params['leave_type']}' was not found in Keka. Please contact HR."

    # -- Compute session values and half-day guard ---------------------------
    from_session, to_session = _SESSION_MAP.get(leave_params.get("session", "full"), (1, 2))
    from_date = leave_params["from_date"]
    to_date   = leave_params["to_date"]
    if leave_params.get("session") in ("first_half", "second_half"):
        to_date = from_date

    # -- Phase 2: Claude builds HAR and calls execute-request ----------------
    logger.info("[mcp_agent] ApplyLeave Phase 2 — execution")

    system2 = _PHASE2_APPLY_SYSTEM.format(
        today=date.today().isoformat(),
        email=employee_email,
        employee_id=employee_id,
        base_url=KEKA_BASE_URL,
    )

    post_url    = f"{KEKA_BASE_URL}{endpoint['path']}"
    auth_header = f"Bearer {token}"

    user_content = f"""\
Apply leave for the employee.

POST {post_url}
Spec: '{endpoint['title']}'

Resolved values (use exactly as-is):
  employeeId:    {employee_id}
  requestedBy:   {employee_id}
  leaveTypeId:   {leave_type_id}
  fromDate:      {from_date}
  toDate:        {to_date}
  fromSession:   {from_session}
  toSession:     {to_session}
  reason:        {leave_params.get('reason') or ''}

Build the execute-request call with:
- method: POST
- url: {post_url}
- headers: [{{"name": "Authorization", "value": "{auth_header}"}}, {{"name": "Content-Type", "value": "application/json"}}]
- postData: {{"mimeType": "application/json", "text": "<JSON string of the body above>"}}

Call execute-request now.\
"""

    try:
        p2 = await client.beta.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=system2,
            messages=[{"role": "user", "content": user_content}],
            mcp_servers=[mcp_server],
            tools=[_TOOLSET_EXEC],
            betas=["mcp-client-2025-11-20"],
        )
        _log_content_blocks("ApplyLeave Phase 2", p2.content)
        text = _first_text(p2)
        return text or "Leave submitted — please verify in Keka."
    except Exception as exc:
        logger.error("[mcp_agent] ApplyLeave Phase 2 failed: %s", exc)
        return f"Sorry, I couldn't submit your leave request. _(Error: {exc})_"
