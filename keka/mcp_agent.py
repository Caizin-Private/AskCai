"""
keka/mcp_agent.py
Handles live HR queries via Anthropic's native MCP connector to Keka HRMS.

The Anthropic API connects to Keka's MCP server and lets Claude call Keka
tools (list-endpoints, execute-request, etc.) directly — no Python handler
layer needed for text queries.
"""

import asyncio
import logging
import anthropic
from keka.client import get_access_token, get_employee_id, KEKA_MCP_URL, KEKA_BASE_URL

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are Caizin's internal HR assistant. Use the Keka HRMS API to answer HR-related requests.

Employee context — use these for all API calls:
  Email    : {employee_email}
  Keka ID  : {employee_id}

For every execute-request HAR call include these headers:
  Authorization  : Bearer {token}
  Content-Type   : application/json

Key Keka API endpoints (base: {base_url}):
• GET  /time/leavebalance          pageNumber=1&pageSize=100 → filter rows where employeeIdentifier == {employee_id}
• GET  /time/leavetypes            → list leave types; use the 'identifier' field (NOT 'id') when posting
• GET  /time/leaverequests         from=dd-MM-yyyy  to=dd-MM-yyyy → filter by employeeIdentifier
• POST /time/leaverequests         body: employeeId, requestedBy, fromDate (yyyy-MM-dd), toDate (yyyy-MM-dd),
                                         fromSession=0, toSession=1, leaveTypeId, reason
• DELETE /time/leaverequests/{{id}} → cancel a leave request by its id

Session values: 0 = FirstHalf, 1 = SecondHalf. Full-day leave → fromSession=0, toSession=1.

Respond in clear, friendly Markdown. Do NOT expose the raw token, internal IDs, or raw JSON to the user.\
"""


async def ask_keka_mcp(question: str, employee_email: str, api_key: str) -> str:
    """Answer a live HR query using the Anthropic MCP connector to Keka.

    Fully async — uses AsyncAnthropic so the event loop is never blocked.
    All synchronous Keka I/O (token + employee lookup) runs in a thread pool.
    """
    try:
        token = await asyncio.to_thread(get_access_token)
    except Exception as exc:
        logger.error("[mcp_agent] token fetch failed: %s", exc)
        return f"Sorry, I couldn't connect to Keka right now. Please try again. _(Error: {exc})_"

    try:
        emp_id = await asyncio.to_thread(get_employee_id, employee_email)
    except Exception as exc:
        logger.warning("[mcp_agent] employee id lookup failed for %s: %s", employee_email, exc)
        emp_id = "unknown"

    system_prompt = _SYSTEM.format(
        employee_email=employee_email,
        employee_id=emp_id,
        token=token,
        base_url=KEKA_BASE_URL,
    )

    try:
        async with anthropic.AsyncAnthropic(api_key=api_key) as client:
            response = await client.beta.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                system=system_prompt,
                messages=[{"role": "user", "content": question}],
                mcp_servers=[
                    {
                        "type": "url",
                        "url": KEKA_MCP_URL,
                        "name": "keka",
                    }
                ],
                tools=[{"type": "mcp_toolset", "mcp_server_name": "keka"}],
                betas=["mcp-client-2025-11-20"],
            )
        return next(
            (b.text for b in response.content if getattr(b, "text", None)),
            "I couldn't complete that HR request. Please try again or contact HR.",
        )
    except Exception as exc:
        logger.error("[mcp_agent] MCP call error: %s", exc)
        return f"Sorry, I couldn't complete your HR request. _(Error: {exc})_"
