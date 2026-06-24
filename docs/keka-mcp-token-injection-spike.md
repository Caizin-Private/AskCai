# SPIKE: Two-Phase MCP Call — Token Injection for `execute-request`

**Type:** Spike  
**Priority:** High  
**Branch:** `keka-mcp-integration`  
**File:** `keka/mcp_agent.py`

---

## Problem

The agent uses Anthropic's MCP connector (`betas=["mcp-client-2025-11-20"]`) to talk to the Keka MCP server at `developers.keka.com/mcp`. Two tools are enabled: `search-endpoints` and `execute-request`. Both are broken in production.

### Root Cause 1 — `search-endpoints` blows context

`search-endpoints` returns the full Keka OpenAPI spec in one tool result (~200k tokens). With `max_tokens=4096` on `claude-haiku-4-5-20251001` this saturates or exceeds the model's usable context. Observed in logs as a 300-second timeout:

```
mcp_tool_result: 'An error occurred while executing the MCP tool:
Timed out while waiting for response to ClientRequest. Waited 300.0 seconds.'
```

### Root Cause 2 — `execute-request` always returns 401

The `authorization_token` field in the MCP server block:

```python
mcp_servers=[{
    "type": "url",
    "url": KEKA_MCP_URL,
    "name": "keka",
    "authorization_token": token,   # authenticates to MCP server only
}]
```

...authenticates Claude's connection **to the MCP server** (`developers.keka.com/mcp`). It is never forwarded as an `Authorization` header on outbound calls the MCP server makes to the Keka API (`caizin.keka.com/api/v1`).

Claude constructs the HAR object without auth headers, so every `execute-request` call fails:

```
Error in tool call execute-request: This endpoint requires OAuth authentication.
Please authenticate via your MCP client settings.
```

**The token is available in Python** — `keka.client.get_access_token()` succeeds on every run. The gap is architectural: there is no supported MCP config field that causes the token to flow into the HAR headers Claude constructs.

### Observed failure pattern (every run)

```
1. Claude calls execute-request("Keka HRIS API", GET /hris/employees)
   → spec not found (hallucinated title)

2. Claude calls list-specs or search-endpoints to discover correct spec
   → returns "Leave" spec title

3. Claude calls execute-request("Leave", GET /time/leavebalance)
   → 401: OAuth authentication required

4. Claude gives up, tells user to log in to Keka manually
```

---

## Proposed Approach

Split `ask_keka_mcp` into two sequential `client.beta.messages.create` calls. Between them, Python intercepts Phase 1's output and injects the Bearer token before Phase 2 runs. Neither call puts the token in `system_prompt`.

### Phase 1 — Endpoint Discovery

Enable only `search-endpoints`, disable `execute-request`:

```python
_MCP_TOOLSET_SEARCH = {
    "type": "mcp_toolset",
    "mcp_server_name": "keka",
    "default_config": {"enabled": False},
    "configs": {
        "search-endpoints": {"enabled": True},
    },
}
```

Claude calls `search-endpoints`, returns `{title, path, method}`. Python captures the result.

### Phase 2 — Execution

Enable only `execute-request`, disable `search-endpoints`. Token must reach the HAR headers. Three options to test:

---

#### Option A — Per-tool config headers (test first)

Check if `_MCP_TOOLSET.configs["execute-request"]` accepts a `headers` field that the MCP server merges into every HAR it executes:

```python
_MCP_TOOLSET_EXEC = {
    "type": "mcp_toolset",
    "mcp_server_name": "keka",
    "default_config": {"enabled": False},
    "configs": {
        "execute-request": {
            "enabled": True,
            "headers": {"Authorization": f"Bearer {token}"},  # does this work?
        },
    },
}
```

No Anthropic docs confirm this field exists. Test empirically.

---

#### Option B — MCP server `authorization_token` forwarding (test second)

The `authorization_token` we pass today is the Keka API Bearer token. Test whether `developers.keka.com/mcp` is designed to forward it as the `Authorization` header on downstream API calls when `execute-request` is invoked.

We confirmed manually (via Claude Code's `mcp__keka__execute-request` tool) that passing `Authorization: Bearer <token>` inside the HAR `headers` array produces a `200`. The question is: does the MCP server do this injection automatically when `authorization_token` is set?

If yes, this is a zero-code-change fix — the current `mcp_agent.py` would already work, pointing to a different bug (token not being passed correctly at runtime vs. in `.mcp.json`).

---

#### Option C — Pre-built HAR injection in message history (fallback)

After Phase 1 returns the endpoint, Python constructs the complete HAR with auth headers and injects it into the Phase 2 messages array so Claude receives it pre-filled and just needs to pass it through to `execute-request`:

```python
# After Phase 1 returns endpoint = {"title": "Leave", "path": "/time/leavebalance", "method": "GET"}
phase2_messages = [
    {"role": "user", "content": question},
    # ... Phase 1 assistant turn + tool results ...
    {
        "role": "user",
        "content": (
            "Call execute-request with exactly this HAR:\n"
            + json.dumps({
                "title": endpoint["title"],
                "harRequest": {
                    "method": endpoint["method"].lower(),
                    "url": f"{KEKA_BASE_URL}{endpoint['path']}",
                    "headers": [{"name": "Authorization", "value": f"Bearer {token}"}],
                },
            })
        ),
    },
]
```

This does not put the token in `system_prompt`. It is not a direct API call. It is not a custom tool. The token lives only in the constructed message turn.

---

## Constraints

| Constraint | Why |
|---|---|
| Token NOT in `system_prompt` | Leaks into model context across all turns |
| No direct `httpx`/`requests` calls to Keka API | Hybrid approach, bypasses MCP entirely |
| No custom tools wrapping Keka endpoints | Defeats the purpose of the MCP integration |

---

## Acceptance Criteria

- Phase 1 returns correct `{title, path, method}` for a given HR question
- Phase 2 returns `200` from `caizin.keka.com/api/v1`
- No OAuth error on `execute-request`
- Total token usage per call stays within `max_tokens=4096`
- If all three options fail → proceed to drop both MCP tools (see fallback below)

---

## Fallback

If Options A, B, C all fail: disable both tools in `_MCP_TOOLSET`, and track endpoint discovery + execution separately outside the MCP layer. Tracked as a follow-up ticket.

---

## Files in Scope

- [`keka/mcp_agent.py`](../keka/mcp_agent.py) — split single call into Phase 1 + Phase 2, inject token
- [`keka/client.py`](../keka/client.py) — token fetch (working, unaffected)
- [`.mcp.json`](../.mcp.json) — MCP server config reference
