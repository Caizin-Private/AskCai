# Business Context Document
## Caizin HR Assistant Bot

---

## 1. Why This System Exists

Caizin employees (100–200 people) work primarily within Microsoft Teams. Before this bot, getting answers to HR policy questions required digging through documents, filing internal requests, or messaging the HR team directly — adding friction and delay. Separately, performing leave operations (checking balances, applying for leave, cancelling requests) required employees to log into the Keka HRMS portal, context-switching out of their daily workflow.

This bot eliminates both friction points by embedding HR self-service directly inside Teams — the place employees already spend their day. The goal is to reduce HR team query load, give employees instant access to accurate policy answers, and make leave management frictionless.

---

## 2. Business Problem & Impact

| Problem | Without the Bot | With the Bot |
|---|---|---|
| Employee has a policy question | Searches documents, messages HR, waits for reply | Types in Teams chat, gets a cited answer in seconds |
| Employee wants to check leave balance | Logs into Keka portal, navigates to leave section | Asks the bot in Teams — instant response |
| Employee wants to apply for leave | Fills out Keka portal form | Fills a Teams Adaptive Card form or types a natural-language request |
| Employee wants to cancel a leave | Logs into Keka portal, finds the record | Says "cancel my leave from X to Y" in Teams |

**Without this system:** HR staff handle a higher volume of repetitive questions; employees break workflow to check an external portal. As the team scales from 100 to 200, this gap widens.

**Failure mode:** If the bot is unavailable, employees fall back to the old manual process — no data is lost (Keka is the source of truth), but the self-service benefit disappears.

---

## 3. Users & Stakeholders

| Actor | Role in the System |
|---|---|
| **Caizin Employees** | Primary users — ask policy questions and perform leave self-service via Teams |
| **HR Team** | Policy owners — their documents are indexed into the RAG system; they benefit from reduced repetitive queries |
| **Engineering / Bot Team** | Builds and maintains the bot; owns the integration between Keka and Teams |
| **Managers** | Indirect stakeholders — they receive leave requests in Keka and approve/reject them; they do **not** interact with the bot |
| **Microsoft Teams Admin** | Deploys and manages the bot app manifest in the Teams tenant |
| **Keka HRMS** | External HR system — source of truth for all employee leave data |

**Important scope boundary:** Managers approve and reject leave requests exclusively through the Keka portal. The bot is employee-facing only and does not expose manager-side workflows.

---

## 4. Value Creation Points

1. **Instant policy answers with citations** — Employees get accurate answers grounded in actual HR documents, with links to the source policy. This reduces miscommunication about leave entitlements, reimbursement limits, and HR rules.

2. **Leave self-service without portal logins** — Four leave operations (balance, apply, history, cancel) are accessible directly in Teams. This reduces the number of times an employee needs to context-switch to the Keka portal for routine operations.

3. **HR query deflection** — Repetitive policy questions (how many sick days do I have? what's the travel reimbursement limit?) are answered by the bot, freeing HR staff for higher-value work.

4. **Guardrail enforcement at submission time** — The bot validates weekend/holiday dates and past dates before ever reaching Keka, preventing invalid leave submissions that would otherwise require manual HR correction.

5. **Scalable HR support** — As Caizin grows from 100 to 200+ employees, the bot serves each new employee equally without additional HR headcount for basic queries.

---

## 5. Core Business Domains

### Domain 1: Policy Knowledge Management
HR documents (leave policy, fitness reimbursement, travel, referrals, POSH) are chunked, embedded, and indexed in Azure Cognitive Search. Employees access this knowledge through natural-language questions. Policy content is maintained by HR offline; the bot reflects whatever documents have been ingested.

### Domain 2: Leave Self-Service
The bot is a frontend for Keka's leave module. It allows employees to:
- **Check balance** — current remaining and used days per leave type
- **Apply for leave** — submit a leave request by leave type, date range, and reason
- **View history** — see all leave requests in a date range with their approval status
- **Cancel a leave** — withdraw a pending or approved request

Leave data lives in Keka and is the single source of truth. The bot reads from and writes to Keka on behalf of the employee.

### Domain 3: Employee Identity Resolution
The bot resolves who the employee is using their Microsoft Teams login (email / UPN). That email is used to look up the corresponding Keka employee record (UUID). This lookup is cached in-process for performance. No separate login is required — Teams SSO is the only authentication layer the employee sees.

---

## 6. Critical Business Workflows

### Workflow A — Policy Question (Live)
```
Employee asks policy question in Teams chat
  → Bot classifies intent (greeting / list policies / RAG query)
  → If RAG: embeds question → searches Azure Cognitive Search → top 15 document chunks
  → Claude generates grounded answer with source citations
  → Employee receives answer with link to original policy document
```

### Workflow B — Check Leave Balance (In Test)
```
Employee asks "what is my leave balance" (or clicks button)
  → Claude detects tool intent → calls get_leave_balance tool
  → Bot resolves Teams email → Keka employee UUID
  → Fetches /time/leavebalance from Keka API
  → Formats and returns: leave type | remaining | used | annual quota
```

### Workflow C — Apply for Leave (In Test)
```
Employee clicks "Apply Leave" button OR types a natural-language request
  → Bot presents Adaptive Card form: leave type, from date, to date, reason
  → On submit: validates dates (past, weekend, holiday, working-day count)
  → If valid: resolves employee ID, resolves leave type identifier from Keka
  → Posts leave request to Keka → returns confirmation with days count
  → Leave appears as Pending in Keka for manager approval
```

### Workflow D — Cancel a Leave (In Test)
```
Employee says "cancel my leave from X to Y"
  → Claude extracts dates → calls cancel_leave tool
  → Fetches leave requests from Keka in that date range → filters to this employee
  → Finds first active (non-cancelled, non-rejected) record
  → Calls DELETE /time/leaverequests/{id} → returns confirmation
```

---

## 7. Business Rules & Constraints

| Rule | Enforced Where | Source |
|---|---|---|
| Cannot apply leave for past dates | `holidays.py: validate_leave_dates()` | Proactive guard |
| Cannot start leave on a weekend (Sat/Sun) | `holidays.py: validate_leave_dates()` | Standard Mon–Fri work week |
| Cannot start leave on a public holiday | `holidays.py: validate_leave_dates()` | Indian public holidays 2026 |
| Zero working-day ranges are rejected | `holidays.py: count_working_days()` | Date range quality check |
| Leave type must exist in Keka | `leave.py: _find_leave_type_id()` | Keka as source of truth |
| Employee must exist in Keka | `client.py: get_employee_id()` | Keka as source of truth |
| Manager approval happens outside the bot | Architecture (Keka portal only) | Scope decision — bot is employee self-service only |
| Policy answers must be grounded in source documents | `rag.py: _generate_answer()` system prompt | Accuracy / no hallucination |
| Bot only answers from retrieved context | RAG system prompt: "Answer ONLY from the context provided" | Prevents incorrect policy guidance |

**Leave types supported (from Keka):**
- Casual Leave
- Sick Leave
- Earned Leave
- Compensatory Off
- Maternity Leave
- Paternity Leave
- Loss of Pay

---

## 8. Success Metrics

Since metrics are not yet tracked, the following represent what success looks like in business terms:

| Metric | What Success Looks Like |
|---|---|
| **Policy query accuracy** | Employees receive correct, cited answers without needing to contact HR |
| **Leave operation completion rate** | Employees can complete balance/apply/cancel without errors or HR intervention |
| **HR query deflection** | HR team notices reduction in repetitive Teams messages or email about leave and policies |
| **Portal login frequency** | Employees log into Keka portal less frequently for basic leave operations |
| **Leave submission error rate** | Invalid leave applications (wrong dates, weekends, holidays) are blocked at the bot layer, not by HR manually |
| **Onboarding speed** | New employees can self-serve policy questions on Day 1 without needing HR walkthrough |

> **Note:** Analytics are not currently implemented in the codebase. Adding structured logging or event tracking would be needed to measure these metrics.

---

## 9. Explicit Non-Goals

The following are **not** in scope for this system:

- **Manager leave approval via the bot** — Managers approve and reject in the Keka portal. The bot does not surface manager-facing workflows.
- **Leave policy enforcement beyond date validation** — The bot does not check leave balance before submission (balance check is left to Keka's API response). Balance validation is documented as a future addition.
- **Multi-company / multi-tenant use** — This bot is built specifically for Caizin. It is not a generic HR bot product.
- **Performance reviews, payroll, or onboarding** — The bot is scoped to policy Q&A and leave management only.
- **Email or push notification to employees** — The bot responds reactively; it does not proactively notify employees about leave status changes.
- **Offline or mobile-native access** — The bot runs in Microsoft Teams only. No standalone mobile app.
- **Policy document management** — HR manages source documents externally; the bot does not support uploading or editing policies through the Teams interface.
- **Custom leave type creation or quota management** — Leave types and quotas are managed entirely in Keka; the bot reflects them as-is.

---

## 10. Assumptions & Open Questions

### Confirmed Assumptions
- Caizin has 100–200 employees, all with Microsoft Teams accounts
- All employees follow a standard Monday–Friday work week
- Keka HRMS is the company's primary HR system and source of truth for all leave data
- Microsoft Teams is the primary internal communication platform
- Policy documents are managed offline by HR and ingested manually into Azure Cognitive Search

### Open Questions / Known Uncertainties

| Question | Impact |
|---|---|
| Which policy documents are currently ingested? | Determines scope of Q&A coverage; employees may ask about policies not yet indexed |
| Is the public holiday list (holidays.py) confirmed with HR? | Several dates are marked "confirm exact date" — incorrect holidays could block valid leave applications |
| Will balance validation be added before Keka goes live? | Without it, an employee can submit leave they don't have balance for — Keka may reject at API level, but the error message may be unclear |
| What is the planned cutover date from test to live bot? | Determines urgency of any remaining testing and validation |
| Will analytics or query logging be added? | Without it, there is no visibility into which policies are queried, what errors occur, or how often leave operations fail |
| Are there employees on non-standard schedules (e.g., weekend shifts)? | Weekend validation currently blocks all Saturday/Sunday leave applications — edge cases for non-standard roles are not handled |

---

*Document generated from codebase analysis of the `Keka-Integration` branch.*
*Last updated: 2026-06-02*
