"""
keka/mcp_agent.py

Two-phase MCP integration with the Keka HRMS MCP server.

Phase 1 — search-endpoints only
  Claude discovers the correct spec title, path, and HTTP method for the question.
  Max tokens kept low (2000) to avoid ballooning context from large search results.

Phase 2 — execute-request only
  Python injects the Bearer token into the HAR headers in the user message
  (not in system_prompt, which is cached and reused across sessions).
  Claude calls execute-request with the pre-built HAR and follows up as needed,
  always including the Authorization header supplied in the message.

Root causes addressed:
  RC-1  search-endpoints + execute-request together exceed the 200k-token context
        limit because search-endpoints tool results embed the full OpenAPI spec.
        → Isolated into separate phases so only one tool schema is loaded per call.
  RC-2  execute-request returns 401 because authorization_token authenticates
        Claude→MCP server, NOT MCP server→Keka API.
        → Bearer token injected explicitly in the HAR headers array (Option C).
"""

import asyncio
import json
import logging
import re
from datetime import date

import anthropic
from keka.client import get_access_token, get_employee_id, KEKA_MCP_URL, KEKA_BASE_URL

logger = logging.getLogger(__name__)

_TOOLSET_SEARCH = {
    "type": "mcp_toolset",
    "mcp_server_name": "keka",
    "default_config": {"enabled": False},
    "configs": {
        "search-endpoints": {"enabled": True},
        "get-endpoint": {"enabled": True},
    },
}


_TOOLSET_EXEC = {
    "type": "mcp_toolset",
    "mcp_server_name": "keka",
    "default_config": {"enabled": False},
    "configs": {"execute-request": {"enabled": True}},
}

# Phase 1: search then get full spec to extract query params.
_PHASE1_SYSTEM = """\
You are an API discovery assistant for Keka HRMS.
Step 1: Call search-endpoints ONCE with a focused keyword to find the right path.
Step 2: Call get-endpoint for that exact path to get its full spec including query parameters.
After both tool calls, reply with ONLY a single JSON object on one line — no markdown, no explanation:
{"title": "<spec title>", "path": "<endpoint path>", "method": "<http method>", "params": {"<param_name>": "<description>"}}
For "params", include only query parameters relevant to filtering by employee or user. If none, use {}.\
"""

# Phase 2: execution — token injected via user message, not here.
_PHASE2_SYSTEM = """\
You are Caizin's internal HR assistant.
Today's date: {today}
Employee email: {email}
Employee ID: {employee_id}
Keka API base URL: {base_url}

Use execute-request to fetch live HR data from Keka and answer the user's question.
The employee's ID is provided above — use it directly as a query parameter when calling data endpoints.
Pass it as ?employeeId=<id> or as the identifier the endpoint requires.
Do NOT call /hris/employees to look up the ID — it is already known.
IMPORTANT: The user message contains an Authorization header — include it in EVERY execute-request call you make.
Report only what the API actually returns. Never invent data.
Respond in clear, friendly Markdown.\
"""


def _extract_endpoint(text: str) -> dict | None:
    """Parse the {title, path, method, params} JSON object from Phase 1's response."""
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
        if all(k in data for k in ("title", "path", "method")):
            data.setdefault("params", {})
            return data
    except json.JSONDecodeError:
        pass
    return None


def _first_text(response) -> str:
    texts = [b.text for b in response.content if getattr(b, "text", None)]
    return texts[-1] if texts else ""


async def ask_keka_mcp(question: str, employee_email: str, api_key: str) -> str:
    """Answer a live HR query using the two-phase Keka MCP approach."""
    try:
        token = await asyncio.to_thread(get_access_token)
    except Exception as exc:
        logger.error("[mcp_agent] token fetch failed: %s", exc)
        return f"Sorry, I couldn't connect to Keka right now. _(Error: {exc})_"

    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=90.0)
    mcp_server = {
        "type": "url",
        "url": KEKA_MCP_URL,
        "name": "keka",
        "authorization_token": token,
    }

    # ── Phase 1: endpoint discovery ───────────────────────────────────────────
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
        for b in p1.content:
            if b.type == "mcp_tool_use":
                logger.info("[mcp_agent] Phase 1 tool_use: tool=%s input=%s", b.name, json.dumps(b.input))
            elif b.type == "mcp_tool_result":
                content = b.content if isinstance(b.content, str) else [
                    (c.text if hasattr(c, "text") else str(c)) for c in b.content
                ]
                logger.info("[mcp_agent] Phase 1 tool_result: %s", content)
        p1_text = _first_text(p1)
        logger.info("[mcp_agent] Phase 1 response: %s", p1_text)
        endpoint = _extract_endpoint(p1_text)
        if endpoint:
            logger.info("[mcp_agent] Phase 1 endpoint: %s", endpoint)
        else:
            logger.warning("[mcp_agent] Phase 1 returned no parseable endpoint — proceeding without pre-built HAR")
    except Exception as exc:
        logger.warning("[mcp_agent] Phase 1 failed (%s) — proceeding to Phase 2 without endpoint hint", exc)

    # ── Employee ID lookup (Python, not Claude) ──────────────────────────────
    employee_id = await asyncio.to_thread(get_employee_id, employee_email)
    if employee_id:
        logger.info("[mcp_agent] resolved employee_id=%s for %s", employee_id, employee_email)
    else:
        logger.warning("[mcp_agent] could not resolve employee_id for %s", employee_email)

    # ── Phase 2: execution ────────────────────────────────────────────────────
    logger.info("[mcp_agent] Phase 2 — execution (endpoint=%s)", endpoint)
    system2 = _PHASE2_SYSTEM.format(
        today=date.today().isoformat(),
        email=employee_email,
        employee_id=employee_id or "unknown — ask the user for their employee ID",
        base_url=KEKA_BASE_URL,
    )

    auth_header = f"Authorization: Bearer {token}"

    if endpoint and employee_id:
        # Use the param name discovered from get-endpoint; fall back to "employeeId"
        params = endpoint.get("params") or {}
        id_param = next(iter(params), "employeeId")
        target_url = f"{KEKA_BASE_URL}{endpoint['path']}?{id_param}={employee_id}"
        logger.info("[mcp_agent] Phase 2 target_url=%s (param=%s)", target_url, id_param)
        user_content = (
            f"{question}\n\n"
            f"Call execute-request: spec='{endpoint['title']}', {endpoint['method'].upper()} {target_url}\n"
            f"Include this header in the HAR headers array: {auth_header}"
        )
    elif endpoint:
        user_content = (
            f"{question}\n\n"
            f"Use spec='{endpoint['title']}', {endpoint['method'].upper()} {KEKA_BASE_URL}{endpoint['path']}.\n"
            f"Include this header in the HAR headers array of every execute-request call: {auth_header}"
        )
    else:
        user_content = (
            f"{question}\n\n"
            f"Include this header in the HAR headers array of every execute-request call: {auth_header}"
        )

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
        for b in p2.content:
            logger.info("[mcp_agent] Phase 2 block type=%s", b.type)
        text = _first_text(p2)
        return text or "I couldn't complete your HR request. Please try again or contact HR."
    except Exception as exc:
        logger.error("[mcp_agent] Phase 2 failed: %s", exc)
        return f"Sorry, I couldn't complete your HR request. _(Error: {exc})_"
