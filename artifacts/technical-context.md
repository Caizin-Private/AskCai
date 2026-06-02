# Technical Context Document
## Caizin HR Assistant Bot — Keka Integration

---

## 1. System Overview

The Caizin HR Bot is a Python FastAPI application deployed on Azure App Service. It exposes a single HTTP endpoint (`/api/messages`) that receives all activity from Microsoft Teams via the Azure Bot Service channel. The app handles two independent workflows: RAG-based policy Q&A and live leave self-service via Keka HRMS.

The Keka integration replaces a previous Zoho People integration using an identical module interface. The bot's Teams-facing behaviour is unchanged — only the HRMS backend is different.

---

## 2. Tech Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.11, Azure App Service (Linux) |
| Web Framework | FastAPI + uvicorn |
| Bot Protocol | Microsoft Bot Framework SDK (`botbuilder-core`) |
| HRMS Integration | Keka HRMS REST API (OAuth2, `requests`) |
| AI / Intent | Anthropic Claude API (Haiku) — intent classification, tool use, answer generation |
| RAG Search | Azure Cognitive Search — hybrid vector + BM25 keyword |
| Embeddings | Azure OpenAI `text-embedding-3-small` |
| Policy Storage | Azure Blob Storage (source PDFs) |
| CI/CD | GitHub Actions → Azure Web App deploy |
| Tests | pytest + unittest.mock (all HTTP calls mocked) |

---

## 3. Architecture Overview

```
Microsoft Teams
      │
      │  HTTPS (Bot Framework protocol)
      ▼
Azure Bot Service  ──►  POST /api/messages
      │
      ▼
FastAPI App (Azure App Service)
      │
      ├── teams_bot.py  ──────────────────────────────────────────────
      │     │  plain text message                                       │
      │     │                                                           │  Adaptive Card
      │     ▼                                                           │  form submit
      │   rag.py                                                        │
      │     ├── Intent: greeting / list_policies ──► direct reply       │
      │     └── Intent: leave / tool use ──────────► Claude tool loop ◄─┘
      │                                                     │
      │                                                     ▼
      │                                            tool_registry.py
      │                                                     │
      │                                                     ▼
      │                                              keka/leave.py
      │                                                     │
      │                                                     ▼
      │                                              keka/client.py
      │                                                     │
      │                 ┌───────────────────────────────────┘
      │                 ▼
      │           Keka HRMS API
      │
      └── rag.py (policy Q&A path)
            │
            ├── Azure OpenAI  →  query embedding
            └── Azure Cognitive Search  →  vector + keyword retrieval
                      └── Claude Haiku  →  grounded answer generation
```

**Module responsibilities:**

| File | Responsibility |
|---|---|
| `main.py` | FastAPI server, `/api/messages` endpoint, Bot Framework adapter |
| `teams_bot.py` | Activity routing, Adaptive Card rendering, Teams UPN resolution |
| `rag.py` | Intent classification, document retrieval, answer generation |
| `tool_registry.py` | Claude tool schema definitions and handler dispatch map |
| `keka/client.py` | OAuth2 token management, HTTP helpers, employee ID resolution |
| `keka/leave.py` | Leave business logic: balance, apply, list, cancel |
| `keka/holidays.py` | Working-day validation, public holiday data, date logic |

---

## 4. Execution Model

### Request Flow — Leave Tool Use

```
User: "Apply Casual Leave from 10-Mar-2026 to 12-Mar-2026"
       │
       ▼
teams_bot.py → rag.py (intent: not greeting, not list_policies)
       │
       ▼
Claude tool use loop:
  1. Claude receives TOOL_DEFINITIONS + user message
  2. Claude selects: apply_leave { leave_type_name, from_date, to_date, reason }
  3. tool_registry.TOOL_HANDLERS["apply_leave"] → keka/leave.handle_apply_leave()
  4. leave.py:
       a. validate_leave_dates() — weekends, holidays, past dates, zero working days
       b. keka_client.get_employee_id() — Teams UPN email → Keka UUID (paginated)
       c. keka_get("/time/leavetypes") — resolve leave type name → identifier
       d. keka_post("/time/leaverequests") — submit
  5. Result string returned to Claude → Claude formats final reply
       │
       ▼
teams_bot.py → Teams
```

### Sync vs Async

The app is an async FastAPI application (uvicorn) but all Keka API calls are **synchronous** (`requests` library). This blocks the event loop during each HRMS call. At Caizin's current scale (~100–200 employees) concurrent bot usage is rare, so this is tolerable. Migrating to `httpx` with `await` is the planned path if concurrency becomes a concern.

### Adaptive Card Path (Apply Leave Form)

The Adaptive Card submit bypasses Claude's tool use loop entirely — `teams_bot.py` reads `activity.value` directly and calls `keka/leave.handle_apply_leave()` with pre-parsed arguments from the form.

---

## 5. Data & State Management

### Statefulness

The application is **stateless** at the persistence layer — no database is owned by this app.

| Data | Owner | Persistence |
|---|---|---|
| Leave records | Keka HRMS | Permanent (external) |
| Policy documents | Azure Cognitive Search | Permanent, re-indexed manually via `ingest.py` |
| OAuth2 access token | In-process (`_token_cache` dict) | Process-scoped, auto-refreshed (24h TTL) |
| Employee UUID cache | In-process (`_employee_cache` dict) | Process-scoped, lost on restart/scale-out |
| Conversation history | None | Stateless — no multi-turn memory |

### Token & Cache Behaviour

- **Token cache:** Module-level dict. A new token is fetched automatically when the cached one is within 60 seconds of expiry. On Azure App Service with multiple workers or instances, each process maintains its own cache — no shared token store.
- **Employee cache:** Maps `email.lower()` → Keka UUID. Avoids re-paginating on repeated calls within the same process lifetime. Cache is cold on every deployment or scale-out event.

### Leave Data Access Pattern

All four leave operations follow the same two-step pattern:

```
Teams UPN email
    → get_employee_id()  →  Keka UUID
    → Keka leave API call with UUID
```

Keka is queried live on every request — no leave data is cached locally.

---

## 6. External Dependencies

| Service | Purpose | Auth | Notes |
|---|---|---|---|
| **Keka HRMS** | Source of truth for all leave data | OAuth2 `kekaapi` grant (client_id + client_secret + api_key) | Token valid 24h; `login.keka.com/connect/token` |
| **Anthropic Claude API** | Intent classification, tool use dispatch, RAG answer generation | API key (`ANTHROPIC_API_KEY`) | Currently Claude Haiku |
| **Azure Cognitive Search** | Policy document vector + keyword index | Admin key | Index populated by `ingest.py`; not touched by Keka integration |
| **Azure OpenAI** | `text-embedding-3-small` for query embeddings | API key + endpoint | Only used in RAG path; not touched by Keka integration |
| **Azure Bot Service** | Teams ↔ App routing, Bot Framework protocol | App ID + Secret | One registration per environment (live / test) |
| **Azure Blob Storage** | Optional — source policy PDF download links | Storage account name | Only affects RAG citations; not HRMS |

### Keka API Endpoints Used

| Operation | Method | Path |
|---|---|---|
| Employee lookup | GET | `/hris/employees` (paginated) |
| Leave types | GET | `/time/leavetypes` |
| Leave balance | GET | `/time/leavebalance` |
| Leave requests | GET | `/time/leaverequests?from=&to=` |
| Apply leave | POST | `/time/leaverequests` |
| Cancel leave | DELETE | `/time/leaverequests/{id}` |

---

## 7. Key Technical Decisions

### Decision 1 — Parallel modules, not an abstraction layer

`zoho/` and `keka/` are structurally identical modules (client.py, leave.py) with no shared interface or base class. `tool_registry.py` is the single changeover point — swapping one import line switches the entire HRMS backend.

**Trade-off:** Simpler than introducing a provider abstraction; acceptable because the migration is a one-time hard cutover. Once Keka is validated in production, the `zoho/` module will be deleted.

### Decision 2 — Claude tool use for natural language leave operations

Leave intents expressed in natural language are routed through Claude's tool use loop rather than hard-coded regex or intent classification rules. Claude selects the correct tool and extracts structured arguments (dates, leave type) from freeform text.

**Trade-off:** More resilient to varied phrasing; adds one Claude API round-trip per leave operation. Acceptable given the small user base and low request volume.

### Decision 3 — Client-side date and balance validation before API call

`keka/holidays.py` validates weekends, public holidays, date ordering, and zero working days before the Keka API is ever called. This gives the user a clear, instant error message rather than surfacing a cryptic API rejection.

**Trade-off:** Validation logic is maintained in two places if Keka's own rules change (e.g. holiday list). The current list is hardcoded for 2026; plan to migrate to `GET /time/holidayscalendar` in a future iteration.

### Decision 4 — Employee ID resolution via full pagination

Keka's `/hris/employees` endpoint does not expose an email filter query parameter. The implementation pages through all employees (100 per page) to find a match by email, then caches the result in-process.

**Known risk:** This approach and the response field names (`email`, `id`) are based on the Postman collection and have **not yet been validated against the live Keka API**. This is the highest-priority item to verify before end-to-end testing.

### Decision 5 — Hardcoded holiday list

Public holidays are stored as a Python set in `keka/holidays.py`. Confirmed Keka exposes a holiday calendar API; migration to dynamic fetching is planned but not part of the current scope.

### Known Issue — Date format mismatch in tool use path

`tool_registry.py` instructs Claude to produce dates in `dd-MMM-yyyy` format (e.g. `10-Mar-2026`), but `keka/leave.py` parses incoming dates as `yyyy-MM-dd` (ISO 8601). When a user applies or cancels leave via natural language (Claude tool use path), the date passed by Claude will fail `validate_leave_dates()` silently.

The Adaptive Card path is unaffected because `teams_bot.py` parses the form input directly.

**Resolution needed:** Either update `tool_registry.py` tool descriptions to request `yyyy-MM-dd`, or add a date normalisation step at the entry point of each leave handler.

---

## 8. Security & Access Control

| Concern | Approach |
|---|---|
| Bot identity | Azure Bot Service App ID + Secret (environment variables) |
| Keka API credentials | OAuth2 client credentials; `client_id`, `client_secret`, `api_key` stored as Azure App Service env vars — never in code |
| Employee data scoping | Employee email is resolved from the authenticated Teams activity context (`_resolve_email()` in `teams_bot.py`) — users can only query their own leave data |
| No manager-facing operations | The bot exposes no approve/reject endpoints; those remain exclusively in the Keka portal |
| Secret rotation | No automated rotation in place; manual rotation via Azure App Service configuration |

---

## 9. Observability & Operations

| Aspect | Current State |
|---|---|
| Logging | Python `logging` module; INFO-level logs for each major step (token refresh, employee ID resolution, leave apply/cancel). No centralised log aggregation configured. |
| Error handling | Each leave handler wraps its logic in a `try/except` and returns a user-friendly message with the raw error appended. |
| Retries | None — HTTP helpers (`keka_get`, `keka_post`, `keka_delete`) do not retry on transient failures. A 5xx or network timeout surfaces as an error message to the user. |
| Alerting | Not configured. |
| Deployment | GitHub Actions deploys on push to `version1.0` (live) or `Keka-Integration` (test, not yet configured). |
| Test environment | Test Azure Bot Service and App Service not yet created. All current testing is unit tests only (mocked HTTP). |

---

## 10. Assumptions & Open Questions

| Item | Status |
|---|---|
| Keka `/hris/employees` response field names (`email`, `id`) | Unverified — assumed from Postman collection |
| Keka leave balance endpoint field names (`availableBalance`, `consumedAmount`, `annualQuota`) | Unverified |
| DELETE `/time/leaverequests/{id}` as the cancel mechanism | Unverified — may be a PATCH to a status field instead |
| `leaveTypeId` vs `identifier` field name in apply leave payload | Assumed `leaveTypeId`; Postman collection used `identifier` for type lookup |
| Date format expected by Keka POST `/time/leaverequests` | Assumed `yyyy-MM-dd`; confirmed from Postman but not validated with a real submission |
| Whether Keka returns `succeeded: true/false` on all responses | Assumed consistent shape; may vary by endpoint |
