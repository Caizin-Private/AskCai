# Dashboard (Report Button) — Implementation Plan

## Feature Summary

When an employee clicks the **"Report"** button (defined in the Teams app manifest), the backend
must respond with today's full attendance data rendered as a table inside a **Teams Task Module**
— a native popup/dialog that Teams opens over the chat. The employee closes it with the X button;
the underlying Quick Actions card stays untouched.

This plan covers only the backend: what happens from the moment the button is clicked to the
data appearing in the popup.

---

## What is a Teams Task Module

Teams has a native popup mechanism called a **Task Module**. When a bot returns the right response
type from an `Action.Execute` button click, Teams opens a dialog box over the chat — no page
navigation, no card replacement. The user closes it with the built-in X and lands back exactly
where they were.

Task Modules support two content types:
- **Adaptive Card** (JSON) — what we use here, no HTML/CSS needed
- Web URL — requires hosting a web page, not needed

The popup can be sized (`small` / `medium` / `large`) and given a title. An Adaptive Card
rendered inside a Task Module has more vertical space than an inline card, so the full employee
table fits comfortably.

---

## How It Works — End to End

The "Report" button lives in the Teams manifest. When clicked, Teams sends an `Action.Execute`
invoke to the bot's existing webhook — the same path every other card button already uses.
The only difference is the **response type** the Lambda returns.

```
Employee clicks "Report" button (defined in Teams manifest)
        │
        │  Teams sends Action.Execute invoke to bot webhook
        │  verb = "view_dashboard"
        ▼
lambdas/teams-bot/handler.py  →  lambda_handler()
        │
        │  event has "requestContext"  →  _handle_response()
        ▼
_handle_response()
        │
        │  verb == "view_dashboard"  →  new branch
        ▼
_handle_view_dashboard()
        │
        ├──► db.query_attendance_by_date(today)
        │       SQL: SELECT * FROM attendance WHERE date = %s
        │       returns List[AttendanceRecord] — every record for today
        │
        ├──► db.get_all_active_employees()
        │       SQL: SELECT e.*, o.object_id FROM employees e LEFT JOIN object_id_table o ...
        │       returns List[Employee] — full roster with names
        │
        ├──► build_dashboard_card(records, employees, today)
        │       pure function — builds Adaptive Card JSON (see below)
        │
        └──► _task_module_response(card, title)
                │
                returns HTTP 200 with type:
                "application/vnd.microsoft.activity.taskInfo"
                │
                └─► Teams opens a popup dialog with the dashboard card inside
                        Employee reads data, clicks X to close
                        Quick Actions card underneath is untouched
```

---

## What Gets Built

### 1. `build_dashboard_card(records, employees, date)` — `shared/teams_client.py`

Pure function. Takes DB data, returns Adaptive Card JSON for the popup.

**Inputs:**
- `records: List[AttendanceRecord]` — today's rows from `attendance` table
- `employees: List[Employee]` — all active employees (for name lookup)
- `date: str` — `"YYYY-MM-DD"`

**Processing logic:**

```python
# Step 1 — name lookup
emp_map = {emp.employee_id: emp.name for emp in employees}

# Step 2 — status bucketing (status field + employee_response field together)
Status bucket    DB status values                          employee_response values
─────────────    ──────────────────────────────────────    ──────────────────────────
Office           present, pre_applied_wfh*                office
WFH              pre_applied_wfh                          wfh
Leave            pre_approved_leave,                      leave, floater_leave
                 pre_approved_floater_leave
Client Site      pre_applied_client_site                  client_site
Absent           absent                                   —
Pending          card_sent (employee_response IS NULL)    —

# Step 3 — per-employee row
name        = emp_map.get(record.employee_id, "Unknown")
status_label = human-readable label (reuse _RESPONSE_LABEL for response values)
check_in    = record.check_in_time → convert UTC ISO → IST → "HH:MM", else "—"

# Step 4 — sort rows
office → wfh → client_site → leave → absent → pending
```

**Output card layout inside the popup:**

```
┌──────────────────────────────────────────────────────────────────┐
│  Attendance Dashboard — 18 Jun 2026           [Teams popup title] │
├──────────────────────────────────────────────────────────────────┤
│                                                                   │
│   ✅ Office   8     🏠 WFH   5     🏥 Leave   3                  │
│   💻 Client  2     ❌ Absent 1     ⏳ Pending  4                  │
│                                                                   │
│  ─────────────────────────────────────────────────────────────   │
│  Name                  Status            Check-in                 │
│  ─────────────────────────────────────────────────────────────   │
│  Nikhil Negi           Office            09:32                    │
│  Priya Sharma          WFH               —                        │
│  Rahul Mehta           Leave             —                        │
│  Sneha Iyer            Absent            —                        │
│  Arjun Verma           Pending           —                        │
│  ...                                                              │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
                                                         [X close]
```

No Back button — the popup's native X closes it. Quick Actions card is untouched underneath.

**Adaptive Card JSON structure:**
```json
{
  "type": "AdaptiveCard",
  "version": "1.4",
  "body": [
    {
      "type": "TextBlock",
      "text": "Attendance — 18 Jun 2026",
      "weight": "Bolder",
      "size": "Medium"
    },
    {
      "type": "ColumnSet",
      "columns": [
        {"width": "stretch", "items": [
          {"type": "TextBlock", "text": "✅ Office",  "isSubtle": true},
          {"type": "TextBlock", "text": "8", "color": "Good",      "weight": "Bolder"}
        ]},
        {"width": "stretch", "items": [
          {"type": "TextBlock", "text": "🏠 WFH",    "isSubtle": true},
          {"type": "TextBlock", "text": "5", "color": "Accent",    "weight": "Bolder"}
        ]},
        {"width": "stretch", "items": [
          {"type": "TextBlock", "text": "🏥 Leave",  "isSubtle": true},
          {"type": "TextBlock", "text": "3", "color": "Warning",   "weight": "Bolder"}
        ]},
        {"width": "stretch", "items": [
          {"type": "TextBlock", "text": "💻 Client", "isSubtle": true},
          {"type": "TextBlock", "text": "2", "color": "Good",      "weight": "Bolder"}
        ]},
        {"width": "stretch", "items": [
          {"type": "TextBlock", "text": "❌ Absent", "isSubtle": true},
          {"type": "TextBlock", "text": "1", "color": "Attention", "weight": "Bolder"}
        ]},
        {"width": "stretch", "items": [
          {"type": "TextBlock", "text": "⏳ Pending","isSubtle": true},
          {"type": "TextBlock", "text": "4", "color": "Default",   "weight": "Bolder"}
        ]}
      ]
    },
    {"type": "Separator"},
    // Header row
    {"type": "ColumnSet", "columns": [
      {"width": 2, "items": [{"type":"TextBlock","text":"Name","weight":"Bolder","isSubtle":true}]},
      {"width": 2, "items": [{"type":"TextBlock","text":"Status","weight":"Bolder","isSubtle":true}]},
      {"width": 1, "items": [{"type":"TextBlock","text":"Check-in","weight":"Bolder","isSubtle":true}]}
    ]},
    // One ColumnSet per employee (generated in loop)
    {"type": "ColumnSet", "columns": [
      {"width": 2, "items": [{"type":"TextBlock","text":"Nikhil Negi"}]},
      {"width": 2, "items": [{"type":"TextBlock","text":"Office","color":"Good"}]},
      {"width": 1, "items": [{"type":"TextBlock","text":"09:32"}]}
    ]},
    ...
  ]
}
```

---

### 2. `_task_module_response(card, title)` — `lambdas/teams-bot/handler.py`

New helper function alongside existing `_invoke_response()`. Returns the Task Module response
envelope that tells Teams to open a popup:

```python
def _task_module_response(card: dict, title: str = "Attendance Dashboard") -> dict:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "statusCode": 200,
            "type": "application/vnd.microsoft.activity.taskInfo",
            "value": {
                "type": "continue",
                "value": {
                    "title": title,
                    "height": "large",
                    "width": "large",
                    "card": {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": card,
                    },
                },
            },
        }),
    }
```

This is the **only structural difference** from the existing `_invoke_response()` — same HTTP 200,
different `type` field in the body. Teams reads that type and opens a popup instead of replacing
the card.

---

### 3. New verb route + handler — `lambdas/teams-bot/handler.py`

**Verb route** added in `_handle_response()`, in the early-routing block after `show_options`:
```python
if verb == "view_dashboard":
    return _handle_view_dashboard()
```

**Handler function:**
```python
def _handle_view_dashboard() -> dict:
    today     = datetime.now(IST).strftime("%Y-%m-%d")
    records   = db.query_attendance_by_date(today)
    employees = db.get_all_active_employees()
    card      = build_dashboard_card(records, employees, today)
    title     = f"Attendance — {datetime.strptime(today, '%Y-%m-%d').strftime('%d %b %Y')}"
    print(f"[TeamsBot] dashboard popup: {len(records)} records for {today}")
    return _task_module_response(card, title)
```

Read-only — zero DB writes.

---

## Files Changed

| File | Change |
|---|---|
| `shared/teams_client.py` | Add `build_dashboard_card()` function |
| `lambdas/teams-bot/handler.py` | Add `_task_module_response()` helper |
| `lambdas/teams-bot/handler.py` | Add `view_dashboard` verb route in `_handle_response()` |
| `lambdas/teams-bot/handler.py` | Add `_handle_view_dashboard()` function |

**Import addition in `teams-bot/handler.py`:**
```python
from shared.teams_client import (
    build_ack_card, build_options_card, build_quick_actions_card,
    build_dashboard_card,   # new
    get_teams_client,
)
```

---

## Existing Code Reused (zero changes needed)

| Function | File | Role |
|---|---|---|
| `db.query_attendance_by_date(date)` | `shared/db_client.py` | All today's attendance records |
| `db.get_all_active_employees()` | `shared/db_client.py` | Employee names for the table |
| `_RESPONSE_LABEL` | `shared/teams_client.py` | Human-readable status labels |
| `_RESPONSE_COLOR` | `shared/teams_client.py` | Adaptive Card color values |
| `_handle_response()` routing | `teams-bot/handler.py` | Entry point, no structural change |

---

## Why Task Module over Inline Card Replacement

| | Inline replacement (old plan) | Task Module popup (this plan) |
|---|---|---|
| Quick Actions card | Gets replaced — lost until re-sent | Stays untouched underneath |
| Back button | Required — adds complexity | Not needed — X closes popup natively |
| Screen space | Limited to chat bubble width | Full large dialog — entire table visible |
| Re-open | Employee must re-click Report | Same — click Report again anytime |
| Code change | `_invoke_response(card)` | `_task_module_response(card, title)` — one new helper |

---

## What the Manifest Does vs What the Backend Does

| Concern | Where handled |
|---|---|
| "Report" button appearing in the app | Teams manifest — already done |
| Button sending `verb=view_dashboard` to webhook | Teams manifest `Action.Execute` definition |
| Querying DB and building the dashboard | Backend — this plan |
| Opening the popup | Teams reads `taskInfo` response type from backend |
| Closing the popup | Teams native X button — no backend needed |

---

## Verification

**Step 1 — Card builder (no AWS):**
```python
from shared.teams_client import build_dashboard_card
from shared.models import AttendanceRecord, Employee

records = [
    AttendanceRecord(date="2026-06-18", employee_id="EMP-001", status="present",
                     check_in_time="2026-06-18T04:02:00Z"),
    AttendanceRecord(date="2026-06-18", employee_id="EMP-002", status="card_sent"),
]
employees = [
    Employee(employee_id="EMP-001", name="Nikhil Negi"),
    Employee(employee_id="EMP-002", name="Priya Sharma"),
]
card = build_dashboard_card(records, employees, "2026-06-18")
assert "18 Jun 2026" in card["body"][0]["text"]
assert any("Nikhil" in str(col) for col in card["body"])
```

**Step 2 — Lambda mock invoke (verify Task Module response shape):**
```json
{
  "requestContext": {},
  "body": "{\"type\":\"invoke\",\"name\":\"adaptiveCard/action\",\"value\":{\"action\":{\"verb\":\"view_dashboard\"}}}"
}
```
Expected response body:
```json
{
  "statusCode": 200,
  "type": "application/vnd.microsoft.activity.taskInfo",
  "value": { "type": "continue", "value": { "title": "Attendance — 18 Jun 2026", ... } }
}
```

**Step 3 — End-to-end (real Teams):**
1. Click **Report** → Teams opens popup dialog with attendance table
2. Verify counts match DB records for today
3. Verify check-in times are in IST
4. Click X → popup closes, Quick Actions card is unchanged
