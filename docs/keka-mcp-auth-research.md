# Keka MCP Server — Auth Token Research & Solutions

**Context:** Caizin HR Bot integrates with the Keka HRMS via the Anthropic MCP beta (`mcp-client-2025-11-20`). The MCP server lives at `https://developers.keka.com/mcp`. When Claude calls `execute-request`, the downstream Keka HRMS API returns **401 Unauthorized** unless an `Authorization: Bearer <token>` header is explicitly included in the HAR request object.

---

## Table of Contents

1. [How the Auth Flow Works](#1-how-the-auth-flow-works)
2. [Root Cause of the 401](#2-root-cause-of-the-401)
3. [Why authorization_token Alone Is Not Enough](#3-why-authorization_token-alone-is-not-enough)
4. [What the Anthropic MCP Beta Supports](#4-what-the-anthropic-mcp-beta-supports)
5. [Possible Solutions](#5-possible-solutions)
6. [Current Implementation](#6-current-implementation)
7. [Recommendation & Trade-offs](#7-recommendation--trade-offs)
8. [References](#8-references)

---

## 1. How the Auth Flow Works

There are **two separate authentication hops** in this integration:

```
Claude (Anthropic API)
    │
    │  authorization_token: <keka_oauth_token>
    ▼
Keka MCP Server  (developers.keka.com/mcp)
    │
    │  ← execute-request tool call with HAR object
    ▼
Keka HRMS REST API  (caizin.keka.com/api/v1/*)
    │  needs: Authorization: Bearer <keka_oauth_token>
    ▼
Response data
```

- **Hop 1 (Claude → MCP server):** Authenticated via `authorization_token` in the MCP server config. This is sent as `Authorization: Bearer <token>` in the HTTP handshake to `developers.keka.com/mcp`.
- **Hop 2 (MCP server → Keka HRMS API):** The `execute-request` tool makes a raw HTTP call to the Keka REST API. This call needs its own `Authorization: Bearer <token>` header inside the HAR object.

These are **two separate auth checks**. Passing `authorization_token` to the MCP server config satisfies Hop 1 but does **nothing** for Hop 2.

---

## 2. Root Cause of the 401

The `execute-request` tool on the Keka MCP server builds and sends an HTTP request exactly as specified in the HAR object. It does not add any auth headers by itself. So when Claude calls:

```json
{
  "harRequest": {
    "method": "GET",
    "url": "https://caizin.keka.com/api/v1/leave/leavebalance",
    "queryString": [{"name": "employeeId", "value": "abc123"}]
  },
  "title": "Leave"
}
```

...the request goes to Keka's API **with no Authorization header**, which returns 401.

---

## 3. Why `authorization_token` Alone Is Not Enough

This is **by design in the MCP specification**, not a Keka bug.

The MCP spec intentionally prevents MCP servers from forwarding client tokens to downstream APIs. This is to prevent the **"confused deputy" attack** — where a malicious or compromised MCP server abuses a client's credential to call third-party services on their behalf without consent.

From the MCP security specification:
> MCP servers must not automatically forward received authorization credentials to downstream services. Each downstream service call must be independently authorized.

**Sources:**
- [MCP Authorization Spec (2025-06-18)](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)
- [Solo.io — MCP Authorization Patterns for Upstream API Calls](https://www.solo.io/blog/mcp-authorization-patterns-for-upstream-api-calls)

---

## 4. What the Anthropic MCP Beta Supports

The `mcp-client-2025-11-20` beta URL-type server config accepts **only these fields**:

```python
{
    "type":                "url",
    "url":                 "https://developers.keka.com/mcp",
    "name":                "keka",
    "authorization_token": "<token>",   # only auth mechanism supported
}
```

**There is no `headers` field, no `env` field, no custom config mechanism.** A feature request exists but is unimplemented as of June 2025:

- [anthropic-sdk-python Issue #989 — Support custom headers for MCP server calls](https://github.com/anthropics/anthropic-sdk-python/issues/989)

---

## 5. Possible Solutions

### Solution A — Token in System Prompt *(Current Implementation)*

The Bearer token is embedded as a literal string in the system prompt. Claude is instructed to include it in the HAR headers array of every `execute-request` call.

```python
_SYSTEM = """
...
AUTHORIZATION — include in EVERY execute-request call:
  {"name": "Authorization", "value": "Bearer <actual-token-here>"}
...
"""
```

**Pros:**
- Works today with the remote MCP server
- Single-phase, no extra code
- Token has 24 h TTL (low rotation risk)

**Cons:**
- Token appears in Anthropic API request body (logged, potentially cached)
- Token is visible to the LLM — a prompt injection attack could exfiltrate it
- Not considered best practice for credential handling

**Verdict:** Pragmatic workaround; acceptable for internal-only bots, not for production user-facing systems.

---

### Solution B — Custom Python Tool Intercept *(Hybrid Approach)*

Split the toolset: MCP tools handle discovery only (`list-specs`, `list-endpoints`, `get-endpoint`). A regular Python tool (`execute_keka_request`) replaces `execute-request`. The agentic loop intercepts calls to this tool and injects the auth header at the Python layer before calling the Keka REST API directly.

```python
# MCP handles discovery (auto-executed, no token needed)
_DISCOVERY_TOOLSET = {
    "type": "mcp_toolset",
    "configs": {"list-specs": ..., "list-endpoints": ..., "get-endpoint": ...},
}

# Python handles execution (token injected here, LLM never sees it)
_EXECUTE_TOOL = {
    "name": "execute_keka_request",
    "description": "Execute an authenticated Keka API request",
    "input_schema": {...},
}

# In the agentic loop:
if block.name == "execute_keka_request":
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, ...)
```

**Pros:**
- Token never appears in LLM context
- Still uses MCP tools for discovery
- Secure by design

**Cons:**
- Not "pure MCP" — bypasses `execute-request` MCP tool
- Requires a manual agentic loop (more code)
- Slightly higher latency (extra Python → Keka HTTP call outside MCP)

**Verdict:** Most secure option available today without infrastructure changes.

---

### Solution C — Self-Hosted MCP Server via `stdio` *(True Pure-MCP Secure Solution)*

Run the Keka MCP server yourself as a local subprocess. Keka credentials are passed as environment variables at startup. The server handles OAuth token acquisition internally. Claude connects via `stdio` — no token in the system prompt, no token in the LLC context at all.

```python
mcp_server = {
    "type":    "stdio",
    "command": "npx",
    "args":    ["-y", "@keka/mcp-server"],   # official Keka MCP npm package
    "env": {
        "KEKA_CLIENT_ID":     os.getenv("KEKA_CLIENT_ID"),
        "KEKA_CLIENT_SECRET": os.getenv("KEKA_CLIENT_SECRET"),
        "KEKA_API_KEY":       os.getenv("KEKA_API_KEY"),
    },
}
```

With this setup:
- The subprocess starts with Keka credentials already in its environment
- It fetches OAuth tokens internally when `execute-request` is called
- The HAR Authorization header is injected by the server, not by Claude
- Zero credentials in LLM context

**Pros:**
- True pure-MCP approach
- Token completely invisible to the LLM
- Credentials managed via env vars (standard practice)
- Works with the existing `execute-request` tool unchanged

**Cons:**
- Requires Keka to publish an official npm/pip MCP server package (unconfirmed as of June 2025)
- Adds process management complexity (subprocess lifecycle, restarts)
- `stdio` server type may not be available on all deployment environments (serverless, containers)
- Unofficial repo found: `KaranThink41/Keka_Official_MCP` — not vetted

**Verdict:** Best long-term solution if an official Keka MCP server package exists.

---

### Solution D — Self-Hosted URL MCP Server *(Infrastructure Approach)*

Deploy the Keka MCP server as a persistent service in your own infrastructure. Configure it with Keka credentials as environment variables. Connect Claude to your hosted URL instead of `developers.keka.com/mcp`.

```
Your Server (e.g., api.caizin.com/keka-mcp)
  ↑ configured with KEKA_CLIENT_ID, KEKA_CLIENT_SECRET, KEKA_API_KEY
  ← Claude connects with a simple shared secret for server access
  → Handles all Keka OAuth internally
```

```python
mcp_server = {
    "type":                "url",
    "url":                 "https://api.caizin.com/keka-mcp",
    "name":                "keka",
    "authorization_token": "simple-shared-secret",   # not the Keka OAuth token
}
```

**Pros:**
- Full control over the server, its auth logic, and token refresh
- Token invisible to LLM
- Centralized credential management
- Works with `execute-request` unchanged

**Cons:**
- Significant infrastructure overhead (host, maintain, monitor a service)
- Requires Keka MCP server source to be available and deployable
- Overkill for a single internal bot

**Verdict:** Best for multi-tenant or production scenarios where credential security is a hard requirement.

---

### Solution E — Wait for Anthropic SDK `headers` Support

Upvote and track [Issue #989](https://github.com/anthropics/anthropic-sdk-python/issues/989). Once custom headers are supported in the MCP URL server config, you could pass:

```python
mcp_server = {
    "type":    "url",
    "url":     "https://developers.keka.com/mcp",
    "name":    "keka",
    "authorization_token": mcp_access_token,
    "headers": {                                  # not yet supported
        "X-Keka-Api-Token": keka_oauth_token,
    },
}
```

This would require the Keka MCP server to read `X-Keka-Api-Token` and use it for downstream API calls — which it may or may not support.

**Verdict:** Future option; nothing to implement today.

---

## 6. Current Implementation

`keka/mcp_agent.py` uses **Solution A** (token in system prompt).

**Tool flow:**

```
list-specs  →  list-endpoints  →  get-endpoint  →  execute-request
   (find spec)   (find path)     (get params)     (call Keka API)
```

**Why `search-endpoints` is excluded:**
`search-endpoints` returns the full OpenAPI spec in its tool result, which can exceed the 200k token context limit when combined with `execute-request`. The lightweight trio (`list-specs` → `list-endpoints` → `get-endpoint`) covers the same discovery workflow without the bloat.

**Token caching:** The Keka OAuth token has a 24 h TTL. `get_access_token()` in `keka/client.py` caches it in-process with a 60-second refresh buffer. The token in the system prompt is always fresh for each `ask_keka_mcp` call.

---

## 7. Recommendation & Trade-offs

| Solution | Secure | Pure MCP | Effort | Works Today |
|----------|--------|----------|--------|-------------|
| A — System prompt | No | Yes | None | Yes |
| B — Python intercept | Yes | Partially | Medium | Yes |
| C — stdio subprocess | Yes | Yes | Medium | Maybe* |
| D — Self-hosted URL | Yes | Yes | High | Maybe* |
| E — SDK headers | Yes | Yes | None | No |

*Depends on availability of an official Keka MCP server package.

**Immediate recommendation:**
- If internal-only bot with Anthropic-managed infrastructure: **Solution A** is acceptable.
- If security is a hard requirement today: **Solution B** is the most practical.
- Long-term: Investigate whether Keka publishes an official MCP server package (**Solution C**) or wait for Anthropic SDK `headers` support (**Solution E**).

---

## 8. References

| Source | Relevance |
|--------|-----------|
| [Anthropic MCP connector docs](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector) | URL server config spec, supported fields |
| [anthropic-sdk-python Issue #989](https://github.com/anthropics/anthropic-sdk-python/issues/989) | Feature request: custom headers for MCP server calls |
| [MCP Authorization Spec (2025-06-18)](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization) | Why MCP servers don't forward client tokens |
| [Solo.io — MCP Auth Patterns](https://www.solo.io/blog/mcp-authorization-patterns-for-upstream-api-calls) | Confused deputy explanation, token exchange patterns |
| [Stytch — MCP Auth Guide](https://stytch.com/blog/MCP-authentication-and-authorization-guide/) | MCP authentication implementation guide |
| [KaranThink41/Keka_Official_MCP](https://github.com/KaranThink41/Keka_Official_MCP) | Unofficial Keka MCP server (uses env var credentials) |
| [Docker MCP Gateway Issue #97](https://github.com/docker/mcp-gateway/issues/97) | Similar auth forwarding problem in Docker MCP Gateway |
| [Curity — Design MCP Authorization](https://curity.io/resources/learn/design-mcp-authorization-apis/) | Authoritative reference on MCP API authorization design |
