"""
keka/mcp_agent.py
Pure MCP integration using Anthropic's MCP connector to Keka HRMS.

Auth is handled entirely by authorization_token in mcp_servers — never in the system prompt.
Enabled tools: search-endpoints (discovery) + execute-request (API calls).
Disabled: get-endpoint, list-endpoints, list-specs, get-server-variables.
Anthropic's API runs the full tool-execution loop server-side; one API call is enough.
"""

import asyncio
import logging
from datetime import date
import anthropic
from keka.client import get_access_token, KEKA_MCP_URL, KEKA_BASE_URL

logger = logging.getLogger(__name__)

# Enabled: search-endpoints (discovery) + execute-request (API calls).
# get-endpoint is disabled — it returns the full OpenAPI spec and is never
# needed at runtime. list-specs, list-endpoints, get-server-variables are also
# disabled as they add no value for answering HR questions.
_MCP_TOOLSET = {
    "type": "mcp_toolset",
    "mcp_server_name": "keka",
    "default_config": {"enabled": False},
    "configs": {
        "search-endpoints": {"enabled": True},
        "execute-request": {"enabled": True},
    },
}

_SYSTEM = """\
You are Caizin's internal HR assistant.
Today's date: {today}
Employee email: {email}
Keka API base URL: {base_url}

Use the execute-request tool to fetch live HR data from Keka and answer the user's question.
When the user's own data is required (leave, payroll, attendance), first call execute-request
on GET /hris/employees to find the record where email matches {email}, then use that
employee's ID in subsequent calls.

Report only what the API actually returns. Never invent data.
Respond in clear, friendly Markdown.\
"""


async def ask_keka_mcp(question: str, employee_email: str, api_key: str) -> str:
    """Answer a live HR query using the Anthropic MCP connector to Keka."""
    try:
        token = await asyncio.to_thread(get_access_token)
    except Exception as exc:
        logger.error("[mcp_agent] token fetch failed: %s", exc)
        return f"Sorry, I couldn't connect to Keka right now. _(Error: {exc})_"

    system_prompt = _SYSTEM.format(
        today=date.today().isoformat(),
        email=employee_email,
        base_url=KEKA_BASE_URL,
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        response = await client.beta.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": question}],
            mcp_servers=[
                {
                    "type": "url",
                    "url": KEKA_MCP_URL,
                    "name": "keka",
                    "authorization_token": token,
                }
            ],
            tools=[_MCP_TOOLSET],
            betas=["mcp-client-2025-11-20"],
        )
        logger.info("[mcp_agent] stop_reason=%s", response.stop_reason)
        for b in response.content:
            logger.info("[mcp_agent] block type=%s %s", b.type, vars(b))
        text_blocks = [b.text for b in response.content if getattr(b, "text", None)]
        return (
            text_blocks[-1]
            if text_blocks
            else "I couldn't complete your HR request. Please try again or contact HR."
        )

    except Exception as exc:
        logger.error("[mcp_agent] error: %s", exc)
        return f"Sorry, I couldn't complete your HR request. _(Error: {exc})_"
