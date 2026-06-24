# Apply Leave — Implementation Plan

## Architecture Decision

Apply leave follows the same two-phase MCP pattern as leave balance:

```
Phase 1 (search-endpoints + get-endpoint)
  → Claude discovers POST /time/leaverequests + body schema

Python (pre-Phase 2, no direct API)
  → get_leave_type_id(leave_type_name)  [new, mirrors get_employee_id]
  → compute fromSession / toSession from card input

Phase 2 (execute-request only)
  → Claude receives all resolved values as plain text
  → Claude builds HAR (including postData) and calls execute-request
```

**No HAR built in Python. No direct API calls for leave types. Claude owns the POST.**

---

## What Python resolves (before Phase 2)

| Value | Source |
|---|---|
| `employeeId` | `get_employee_id(email)` — already exists |
| `leaveTypeId` | `get_leave_type_id(leave_type_name)` — **new** |
| `fromSession` / `toSession` | `_SESSION_MAP[session_choice]` — **new**, pure math |
| `fromDate` / `toDate` | from card form data |
| `reason` | from card form data |

---

## File-by-file changes

---

### 1. `teams_bot.py` — Add session field to leave form card

**Change:** Add an `Input.ChoiceSet` for duration between `to_date` and `reason` in `send_apply_leave_form`.

```json
{
  "type": "Input.ChoiceSet",
  "id": "session",
  "label": "Duration",
  "isRequired": true,
  "choices": [
    { "title": "Full Day",     "value": "full" },
    { "title": "First Half",   "value": "first_half" },
    { "title": "Second Half",  "value": "second_half" }
  ],
  "value": "full",
  "style": "compact"
}
```

**Note:** If `session == "first_half"` or `"second_half"`, Python must set `to_date = from_date` (half-day is always a single day).

---

### 2. `keka/client.py` — Add `get_leave_type_id`

Mirrors `get_employee_id` exactly: direct GET, process-level cache, no MCP involved.

```python
_leave_type_cache: dict[str, str] = {}   # "Casual Leave" → UUID

def get_leave_type_id(leave_type_name: str) -> str | None:
    """Return Keka leaveTypeId UUID for a given leave type name, or None."""
    key = leave_type_name.lower().strip()
    if key in _leave_type_cache:
        return _leave_type_cache[key]

    token = get_access_token()
    resp = requests.get(
        f"{KEKA_BASE_URL}/time/leavetypes",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data if isinstance(data, list) else data.get("data", [])

    for lt in items:
        name = lt.get("name") or lt.get("displayName") or ""
        uid  = lt.get("id") or lt.get("leaveTypeId") or lt.get("identifier")
        if uid:
            _leave_type_cache[name.lower().strip()] = str(uid)

    result = _leave_type_cache.get(key)
    if not result:
        logger.warning("[keka] leave type not found: %s", leave_type_name)
    return result
```

**Also export** `get_leave_type_id` from `keka/__init__.py` if needed.

---

### 3. `keka/mcp_agent.py` — Four sub-changes

#### 3a. Update `_PHASE1_SYSTEM` — extract body schema for POST endpoints

Add one instruction after the existing params extraction rule:

```python
_PHASE1_SYSTEM = """\
You are an API discovery assistant for Keka HRMS.
Step 1: Call search-endpoints ONCE with a focused keyword to find the right path.
Step 2: Call get-endpoint for that exact path to get its full spec including parameters and request body.
After both tool calls, reply with ONLY a single JSON object on one line — no markdown, no explanation:
{"title": "<spec title>", "path": "<endpoint path>", "method": "<http method>", "params": {"<param_name>": "<description>"}, "body": {"<field_name>": "<description>"}}
For "params", include only query parameters relevant to filtering by employee or user. If none, use {}.
For "body", include all request body fields from the schema. If none (e.g. GET), use {}.\
"""
```

#### 3b. Update `_extract_endpoint` — parse optional `body` key

```python
def _extract_endpoint(text: str) -> dict | None:
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
        if all(k in data for k in ("title", "path", "method")):
            data.setdefault("params", {})
            data.setdefault("body", {})   # ← new
            return data
    except json.JSONDecodeError:
        pass
    return None
```

#### 3c. Add `_SESSION_MAP` constant

```python
_SESSION_MAP = {
    "full":        (1, 2),   # fromSession=1 (FirstHalf start), toSession=2 (SecondHalf end)
    "first_half":  (1, 1),
    "second_half": (2, 2),
}
```

> **Confirm these integer values** from the `get-endpoint` response for `/time/leaverequests`
> before going live. Keka may use 0-indexed or different enum names.

#### 3d. Add `ask_keka_mcp_apply_leave` function

New entry point for the apply-leave flow. Lives alongside `ask_keka_mcp`.

```python
async def ask_keka_mcp_apply_leave(
    leave_params: dict,   # keys: leave_type, from_date, to_date, session, reason
    employee_email: str,
    api_key: str,
) -> str:
    """Apply leave via the two-phase Keka MCP approach."""

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
    employee_id    = await asyncio.to_thread(get_employee_id, employee_email)
    leave_type_id  = await asyncio.to_thread(get_leave_type_id, leave_params["leave_type"])

    if not employee_id:
        return "I couldn't identify your employee profile. Please contact HR."
    if not leave_type_id:
        return f"Leave type '{leave_params['leave_type']}' was not found in Keka. Please contact HR."

    # -- Compute session values ----------------------------------------------
    from_session, to_session = _SESSION_MAP.get(leave_params.get("session", "full"), (1, 2))

    # -- Half-day: to_date must equal from_date ------------------------------
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

    post_url   = f"{KEKA_BASE_URL}{endpoint['path']}"
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

Authorization header: Bearer {auth_header}

Build the execute-request call with:
- method: POST
- url: {post_url}
- headers: [{{"name": "Authorization", "value": "Bearer {auth_header}"}}, {{"name": "Content-Type", "value": "application/json"}}]
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
```

#### 3e. Add `_PHASE2_APPLY_SYSTEM`

```python
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
```

---

### 4. `main.py` — Update `apply_leave_submit` handler

Replace the current natural-language query with structured params:

```python
if action == "apply_leave_submit":
    await turn_context.send_activity(Activity(type=ActivityTypes.typing))

    leave_type = form_data.get("leave_type", "Casual Leave")
    from_date  = form_data.get("from_date", "")
    to_date    = form_data.get("to_date", "")
    session    = form_data.get("session", "full")        # new field from card
    reason     = form_data.get("reason", "")

    if not from_date or not to_date:
        await turn_context.send_activity(
            "⚠️ Please fill in both **From** and **To** dates before submitting."
        )
        return

    from keka.client import TEST_EMPLOYEE_EMAIL
    from keka.mcp_agent import ask_keka_mcp_apply_leave   # ← new import
    from rag import _get_anthropic_api_key

    leave_params = {
        "leave_type": leave_type,
        "from_date":  from_date,
        "to_date":    to_date,
        "session":    session,
        "reason":     reason,
    }

    result = await ask_keka_mcp_apply_leave(
        leave_params,
        TEST_EMPLOYEE_EMAIL,
        _get_anthropic_api_key(),
    )
    await turn_context.send_activity(result)
    return
```

---

## Implementation order

1. `teams_bot.py` — add session field to card
2. `keka/client.py` — add `get_leave_type_id`
3. `keka/mcp_agent.py` — update `_PHASE1_SYSTEM`, `_extract_endpoint`, add `_SESSION_MAP`, `_PHASE2_APPLY_SYSTEM`, `ask_keka_mcp_apply_leave`
4. `main.py` — update submit handler

---

## Open items before go-live

| Item | Action |
|---|---|
| Confirm `fromSession` / `toSession` integer enum values | Check Keka OpenAPI spec for `/time/leaverequests` — run Phase 1 manually and log `get-endpoint` response |
| Confirm `/time/leavetypes` response shape | Log the raw response from `get_leave_type_id` on first call |
| `requestedBy` field | Confirm whether Keka requires it, and whether it equals `employeeId` or a separate manager UUID |
| Error messages from Keka API | Test with an invalid date range to see what 4xx response looks like; surface it clearly to user |
| Replace `TEST_EMPLOYEE_EMAIL` with real user email | Already done for leave balance path — use same `_get_employee_email(turn_context)` from `teams_bot.py` |

---

## Data flow summary

```
User submits card
  │
  ├─ form_data: { leave_type, from_date, to_date, session, reason }
  │
  └─ main.py (apply_leave_submit)
       │
       ├─ Phase 1 → Claude → search-endpoints + get-endpoint → body schema
       │
       ├─ Python: get_employee_id(email)     → employeeId UUID
       ├─ Python: get_leave_type_id(name)    → leaveTypeId UUID
       ├─ Python: _SESSION_MAP[session]      → fromSession, toSession integers
       ├─ Python: half-day guard             → to_date = from_date if needed
       │
       └─ Phase 2 → Claude → execute-request (POST /time/leaverequests)
                                │
                                └─ HAR: url + headers + postData (JSON body)
```
