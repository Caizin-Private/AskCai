"""
keka/mcp_agent.py
Handles live HR queries using Claude + a generic Keka API tool.

Claude decides which Keka endpoint to call and with what parameters.
We execute the actual HTTP via keka_get / keka_post / keka_delete
(proven transport) so there is no MCP-connector transport dependency.
"""

import json
import asyncio
import logging
from datetime import date
import anthropic
from keka.client import (
    get_access_token, get_employee_id,
    keka_get, keka_post, keka_delete,
    KEKA_BASE_URL,
)

logger = logging.getLogger(__name__)

_CALL_TOOL = {
    "name": "call_keka_api",
    "description": (
        "Execute a Keka HRMS API call and return the JSON response. "
        "Use this to fetch or modify HR data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "DELETE"],
                "description": "HTTP method",
            },
            "path": {
                "type": "string",
                "description": "API path relative to base URL, e.g. /time/leavebalance",
            },
            "params": {
                "type": "object",
                "description": "Query string parameters for GET requests",
            },
            "body": {
                "type": "object",
                "description": "Request body for POST requests",
            },
        },
        "required": ["method", "path"],
    },
}

_SYSTEM = """\
You are Caizin's internal HR assistant. Use the call_keka_api tool to answer HR requests.

Today's date: {today}

Employee context:
  Email    : {employee_email}
  Keka ID  : {employee_id}

Key API endpoints (base: {base_url}):

• GET  /time/leavebalance        params: pageNumber=1, pageSize=100
                                 Response has 'data' array and 'nextPage' boolean.
                                 Find the row where employeeIdentifier == "{employee_id}".
                                 If not found and nextPage is true, call again with pageNumber=2, then 3, etc.
                                 Continue paginating until you find the row OR nextPage is false.
                                 NEVER report generic or estimated leave types — only return actual data from the API.

• GET  /time/leavetypes          → list leave types; use 'identifier' field (NOT 'id') when posting

• GET  /time/leaverequests       params: from=dd-MM-yyyy, to=dd-MM-yyyy
                                 Find rows where employeeIdentifier == "{employee_id}".
                                 Paginate if needed (same pattern as leavebalance).

• POST /time/leaverequests       body: employeeId, requestedBy, fromDate (yyyy-MM-dd),
                                       toDate (yyyy-MM-dd), fromSession=0, toSession=1,
                                       leaveTypeId, reason

• DELETE /time/leaverequests/{{id}}  → cancel leave request by its id

Session values: 0 = FirstHalf, 1 = SecondHalf. Full-day = fromSession:0, toSession:1.

IMPORTANT: Always use real API data. Never guess, estimate, or invent leave types or balances.
If you cannot find the employee's record after paginating all pages, say so clearly.

Respond in clear, friendly Markdown. Do NOT expose internal IDs or raw JSON to the user.\
"""


def _run_keka_api(method: str, path: str, params: dict = None, body: dict = None) -> dict:
    if method == "GET":
        return keka_get(path, params)
    if method == "POST":
        return keka_post(path, body or {})
    if method == "DELETE":
        return keka_delete(path)
    return {"error": f"Unsupported method: {method}"}


async def ask_keka_mcp(question: str, employee_email: str, api_key: str) -> str:
    """Answer a live HR query using Claude + Keka API tool (fully async)."""
    try:
        await asyncio.to_thread(get_access_token)
    except Exception as exc:
        logger.error("[mcp_agent] token fetch failed: %s", exc)
        return f"Sorry, I couldn't connect to Keka right now. _(Error: {exc})_"

    try:
        emp_id = await asyncio.to_thread(get_employee_id, employee_email)
    except Exception as exc:
        logger.warning("[mcp_agent] employee id lookup failed for %s: %s", employee_email, exc)
        emp_id = "unknown"

    system_prompt = _SYSTEM.format(
        today=date.today().isoformat(),
        employee_email=employee_email,
        employee_id=emp_id,
        base_url=KEKA_BASE_URL,
    )

    messages = [{"role": "user", "content": question}]

    try:
        async with anthropic.AsyncAnthropic(api_key=api_key) as client:
            while True:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=2048,
                    system=system_prompt,
                    tools=[_CALL_TOOL],
                    messages=messages,
                )

                if response.stop_reason != "tool_use":
                    break

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use" and block.name == "call_keka_api":
                        inp = block.input
                        result = await asyncio.to_thread(
                            _run_keka_api,
                            inp.get("method", "GET"),
                            inp["path"],
                            inp.get("params"),
                            inp.get("body"),
                        )
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        })

                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

        return next(
            (b.text for b in response.content if getattr(b, "text", None)),
            "I couldn't complete your HR request. Please try again or contact HR.",
        )

    except Exception as exc:
        logger.error("[mcp_agent] error: %s", exc)
        return f"Sorry, I couldn't complete your HR request. _(Error: {exc})_"
