# Keka MCP Migration Plan
## Caizin HR Bot — keka-mcp-integration branch

**Date:** 2026-06-04
**Branch:** `keka-mcp-integration`
**Requirement:** Replace direct Keka REST API calls with Keka MCP transport. Use `recruiter@caizin.com` for all leave operations during testing to protect real employee data.

---

## Background

The bot currently calls Keka HRMS directly from Python using the `requests` library (`keka/client.py`). The migration to Keka MCP means all API calls will be routed through the Keka MCP server at `https://developers.keka.com/mcp` using its `execute-request` tool.

### Key discovery during planning
The Keka MCP spec (`Core Hr`) exposes `POST /hris/employees/search` which accepts `{"workEmail": "..."}` and returns the employee profile in a single call. This replaces the current approach of paginating through all employees to find a match — a performance improvement bundled into Phase 2.

### MCP transport protocol (confirmed via curl)
```
POST https://developers.keka.com/mcp
Accept: application/json, text/event-stream
Content-Type: application/json

{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "execute-request",
    "arguments": {
      "title": "Leave",
      "harRequest": {
        "method": "get",
        "url": "https://caizin.keka.com/api/v1/time/leavebalance",
        "headers": [{"name": "Authorization", "value": "Bearer <token>"}],
        "queryString": [{"name": "pageNumber", "value": "1"}]
      }
    }
  }
}

Response (SSE):
event: message
data: {"result": {"content": [{"type": "text", "text": "{...Keka API JSON...}"}]}, "jsonrpc": "2.0", "id": 1}
```

---

## Phase 0 — Switch Deploy Pipeline to `keka-mcp-integration`

### Goal
The live bot currently deploys from the `Keka-Integration` branch. Before any code changes land, redirect CI/CD and the EC2 instance to track the `keka-mcp-integration` branch so all subsequent phases deploy automatically on push.

### Why
Without this step, pushing Phase 1–3 changes to `keka-mcp-integration` will not trigger a deploy, and the live bot will still run from the old branch.

### 1 — GitHub Actions workflow (already done)

**File:** `.github/workflows/deploy.yml`

| Field | Old value | New value |
|---|---|---|
| `on.push.branches` | `Keka-Integration` | `keka-mcp-integration` |
| `git checkout` | `git checkout Keka-Integration` | `git checkout keka-mcp-integration` |
| `git pull` | `git pull origin Keka-Integration` | `git pull origin keka-mcp-integration` |

Commit and push this change to `keka-mcp-integration` to activate the new pipeline.

### 2 — EC2 one-time SSH migration

The EC2 instance currently has `Keka-Integration` checked out. SSH in and run these commands **once** to switch it over:

```bash
cd /home/<EC2_USER>/Caizin-HR-Bot

# Fetch all remote branches (includes keka-mcp-integration)
git fetch origin

# Switch to the new branch
git checkout keka-mcp-integration
git pull origin keka-mcp-integration

# Install any new dependencies (safe to run even if unchanged)
source venv/bin/activate
pip install -r requirements.txt

# Restart the live bot — brief downtime expected (~5 seconds)
sudo systemctl restart caizin-hr-bot

# Confirm it came back up
sudo systemctl status caizin-hr-bot
```

> **Note:** Replace `<EC2_USER>` with the value in your `EC2_USER` GitHub secret (typically `ubuntu` or `ec2-user`). The bot will be offline for a few seconds during `systemctl restart` — do this during low-traffic hours.

### Verification
After the restart, push any trivial commit to `keka-mcp-integration` (e.g., a comment in `deploy.yml`) and confirm the GitHub Actions deploy job runs and completes green. From this point, all pushes to `keka-mcp-integration` auto-deploy.

---

## Phase 1 — Test Email Override

### Goal
All Keka operations use `recruiter@caizin.com` regardless of who is chatting in Teams. No real employee data is read or written during testing.

### Why
The bot is connected to live Keka data. Without this guard, any tester's leave data would be affected by test runs. `recruiter@caizin.com` is the designated test account — all test operations land on that account only.

### Files changed

| File | Change |
|---|---|
| `keka/client.py` | Add `TEST_EMPLOYEE_EMAIL` constant |
| `rag.py` | Override `employee_email` before the tool-use loop (NL path) |
| `main.py` | Override `employee_email` in the Adaptive Card submit block |

### Diff summary

**`keka/client.py`**
```python
# Add after env var declarations
TEST_EMPLOYEE_EMAIL = os.getenv("KEKA_TEST_EMAIL", "recruiter@caizin.com")
```

**`rag.py`** — inside `ask_policy_question()`, before the tool-use loop:
```python
from keka.client import TEST_EMPLOYEE_EMAIL
# ...
employee_email = TEST_EMPLOYEE_EMAIL   # use test account during testing
```

**`main.py`** — inside the `apply_leave_submit` block, replace line 94:
```python
# Before:
employee_email = _get_employee_email(turn_context)

# After:
from keka.client import TEST_EMPLOYEE_EMAIL
employee_email = TEST_EMPLOYEE_EMAIL   # use test account during testing
```

### How to test

1. In Teams, click **My Leave Balance**
   - Expected: shows `recruiter@caizin.com`'s balance, NOT your own
2. Type **"show my leave history"**
   - Expected: recruiter's leave history appears
3. Apply a leave via the **📝 Apply Leave** form
   - Expected: leave request created on recruiter's Keka account
4. Verify in Keka portal (logged in as recruiter) — the leave request is visible there
5. Type **"cancel my leave from YYYY-MM-DD to YYYY-MM-DD"** (use the date from step 3)
   - Expected: recruiter's leave request is cancelled

### Rollback
Remove the three added lines and redeploy. Zero risk to real employee data.

---

## Phase 2 — Employee Lookup Optimization

### Goal
Replace the paginated `GET /hris/employees` loop with `POST /hris/employees/search`. Single API call, no pagination, faster response.

### Why
The current `get_employee_id()` pages through all employees (100 per page) until it finds a match. With 100–200 employees this can take 1–2 API calls, but it's brittle and slow. The Keka MCP spec exposes a direct email search endpoint that was not used in the original implementation.

### Files changed

| File | Change |
|---|---|
| `keka/client.py` | Rewrite `get_employee_id()` only — no other changes |

### Diff summary

**`keka/client.py`** — replace `get_employee_id()`:
```python
# Before: paginates through GET /hris/employees
def get_employee_id(email: str) -> str:
    key = email.lower().strip()
    if key in _employee_cache:
        return _employee_cache[key]
    page = 1
    while True:
        data = keka_get("/hris/employees", {"pageNumber": page, "pageSize": 100})
        employees = data.get("data", [])
        for emp in employees:
            emp_email = (emp.get("email") or "").lower().strip()
            if emp_email == key:
                _employee_cache[key] = emp["id"]
                return emp["id"]
        if not data.get("nextPage"):
            break
        page += 1
    raise ValueError(f"No Keka employee found for email: {email}")

# After: single POST /hris/employees/search call
def get_employee_id(email: str) -> str:
    key = email.lower().strip()
    if key in _employee_cache:
        return _employee_cache[key]

    data = keka_post("/hris/employees/search", {"workEmail": key})
    emp = data.get("data")
    if not emp or not emp.get("id"):
        raise ValueError(f"No Keka employee found for email: {email}")

    _employee_cache[key] = emp["id"]
    logger.info(f"[keka] resolved {email} → {emp['id']}")
    return emp["id"]
```

> **Note:** `workEmail` (camelCase) is the correct field name per Keka's OpenAPI spec.
> The old code used `emp.get("email")` which is only available in the paginated list response.

### How to test

1. In Teams, check leave balance
   - Expected: same result as Phase 1, but faster
2. Check application logs — should see exactly ONE log line:
   `[keka] resolved recruiter@caizin.com → <uuid>`
   instead of multiple `[keka] fetching page N` lines
3. Apply and cancel a leave — both still work correctly

### Rollback
Revert `get_employee_id()` to the paginated version.

---

## Phase 3 — Keka MCP Transport

### Goal
Replace the `requests.get/post/delete` HTTP helpers in `keka/client.py` with calls routed through the Keka MCP server. **Only `keka/client.py` changes.** All other files — `leave.py`, `tool_registry.py`, `rag.py`, `teams_bot.py`, `main.py` — are untouched.

### Architecture after this phase

```
Before:                              After:
keka/client.py                       keka/client.py
  │                                    │
  └── requests.get/post/delete         └── POST developers.keka.com/mcp
           │                                    │  JSON-RPC: execute-request (SSE)
           ▼                                    ▼
  caizin.keka.com/api/v1              Keka MCP server
                                               │
                                               └── caizin.keka.com/api/v1
```

### Files changed

| File | Change |
|---|---|
| `keka/client.py` | Add `_mcp_request()` helper; replace `keka_get`, `keka_post`, `keka_delete` bodies |

### Diff summary

**`keka/client.py`** — add `_mcp_request()` and rewrite HTTP helpers:

```python
KEKA_MCP_URL = "https://developers.keka.com/mcp"
_mcp_req_id  = 0


def _spec_for(path: str) -> str:
    """Map API path to Keka MCP spec title."""
    return "Core Hr" if path.startswith("/hris") else "Leave"


def _mcp_request(spec_title: str, method: str, url: str,
                 params: dict = None, body: dict = None) -> dict:
    """Route a Keka API call through the MCP execute-request tool (SSE transport)."""
    global _mcp_req_id
    _mcp_req_id += 1

    har = {
        "method": method.lower(),
        "url": url,
        "headers": [
            {"name": "Authorization", "value": f"Bearer {get_access_token()}"},
            {"name": "Content-Type",  "value": "application/json"},
        ],
        "queryString": [
            {"name": k, "value": str(v)} for k, v in (params or {}).items()
        ],
    }
    if body is not None:
        har["postData"] = {"mimeType": "application/json", "text": json.dumps(body)}

    payload = {
        "jsonrpc": "2.0",
        "id": _mcp_req_id,
        "method": "tools/call",
        "params": {
            "name": "execute-request",
            "arguments": {"title": spec_title, "harRequest": har},
        },
    }

    resp = requests.post(
        KEKA_MCP_URL,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        timeout=30,
    )
    resp.raise_for_status()

    for line in resp.text.splitlines():
        if not line.startswith("data:"):
            continue
        rpc = json.loads(line[5:].strip())
        if "error" in rpc:
            raise ValueError(f"MCP error: {rpc['error']}")
        content = rpc.get("result", {}).get("content", [])
        if not content:
            raise ValueError(f"Empty MCP result for {method} {url}")
        raw = content[0].get("text", "")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            if "{" in raw:
                try:
                    return json.loads(raw[raw.find("{"):])
                except json.JSONDecodeError:
                    pass
            return {"succeeded": False, "message": raw, "data": None}

    raise ValueError(f"No SSE data line in MCP response for {method} {url}")


def keka_get(path: str, params: dict = None) -> dict:
    return _mcp_request(_spec_for(path), "GET",
                        f"{KEKA_BASE_URL}{path}", params=params)


def keka_post(path: str, payload: dict) -> dict:
    return _mcp_request(_spec_for(path), "POST",
                        f"{KEKA_BASE_URL}{path}", body=payload)


def keka_delete(path: str) -> dict:
    return _mcp_request(_spec_for(path), "DELETE",
                        f"{KEKA_BASE_URL}{path}")
```

### How to test

1. **Leave balance** — Teams: "What is my leave balance?"
   - Expected: recruiter's balance returned as before
2. **Leave history** — Teams: "Show my leave history"
   - Expected: recruiter's leave list
3. **Apply leave** — Teams: fill the Apply Leave form
   - Expected: confirmation message; leave appears in Keka portal under recruiter
4. **Cancel leave** — Teams: "Cancel my leave from YYYY-MM-DD to YYYY-MM-DD"
   - Expected: cancellation confirmation
5. **Check logs** — you should NOT see `requests.get https://caizin.keka.com`.
   All Keka traffic now goes through MCP

### Debugging tip
Add a temporary log line in `_mcp_request` to see the raw SSE response:
```python
logger.debug(f"[mcp] raw response: {resp.text[:500]}")
```
Remove before committing.

### Rollback
Restore the original `keka_get/keka_post/keka_delete` bodies that use `requests` directly.

---

## Phase 4 — Production Cutover

### Goal
Remove the `recruiter@caizin.com` override and let each employee's own Teams identity drive Keka operations. Roll out to all employees.

### Pre-conditions (all must be true before proceeding)
- [ ] All 4 leave operations pass end-to-end tests in Teams (Phase 1–3 complete)
- [ ] Date validation (weekends, public holidays, past dates) correctly blocks invalid submissions
- [ ] Leave balance format is correct and readable in Teams
- [ ] No unhandled errors observed in application logs during test period
- [ ] HR team has signed off on the test results

### Files changed

| File | Change |
|---|---|
| `rag.py` | Remove `employee_email = TEST_EMPLOYEE_EMAIL` line |
| `main.py` | Restore `employee_email = _get_employee_email(turn_context)` |
| `keka/client.py` | Optionally remove `TEST_EMPLOYEE_EMAIL` constant (or leave for future use) |

### How to test

1. You (as yourself in Teams) check leave balance — you should see YOUR OWN balance
2. A second employee tests — they should see their own balance
3. Apply a test leave as yourself — verify it appears on YOUR Keka account

---

## Summary

| Phase | Files changed | Risk | Independent test |
|---|---|---|---|
| 0 — Deploy pipeline switch | `.github/workflows/deploy.yml` + EC2 SSH | Low (brief restart) | Push to branch triggers green deploy |
| 1 — Email override | `keka/client.py`, `rag.py`, `main.py` | None | Balance shows recruiter's data |
| 2 — Employee search | `keka/client.py` (`get_employee_id` only) | Low | Single lookup in logs |
| 3 — MCP transport | `keka/client.py` (HTTP helpers only) | Medium | All 4 ops via MCP |
| 4 — Production cutover | `rag.py`, `main.py` | Low (tested code) | Your own balance appears |

### Keka MCP endpoints used

| Operation | Spec | Method | Path |
|---|---|---|---|
| Employee lookup | Core Hr | POST | `/hris/employees/search` |
| Leave types | Leave | GET | `/time/leavetypes` |
| Leave balance | Leave | GET | `/time/leavebalance` |
| Leave history | Leave | GET | `/time/leaverequests` |
| Apply leave | Leave | POST | `/time/leaverequests` |
| Cancel leave | Leave | DELETE | `/time/leaverequests/{id}` |

---

*Plan generated: 2026-06-04*
*Branch: keka-mcp-integration*