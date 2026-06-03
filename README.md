# Caizin HR Assistant Bot

A Microsoft Teams bot that gives Caizin employees instant, self-service access to HR policy answers and leave management — without leaving Teams or logging into an external portal.
---

## Table of Contents

- [Overview](#overview)
- [Business Context](#business-context)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Features](#features)
- [How It Works](#how-it-works)
  - [Policy Q&A (RAG)](#policy-qa-rag)
  - [Leave Self-Service](#leave-self-service)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Setup & Installation](#setup--installation)
- [Environment Variables](#environment-variables)
- [Running Locally](#running-locally)
- [Ingesting Policy Documents](#ingesting-policy-documents)
- [Testing](#testing)
- [Deployment](#deployment)
- [Security & Access Control](#security--access-control)
- [Business Rules](#business-rules)
- [Known Issues](#known-issues)
- [Out of Scope](#out-of-scope)

---

## Overview

Caizin HR Bot is a Python FastAPI application deployed on Azure App Service. It exposes a single endpoint (`POST /api/messages`) that receives all activity from Microsoft Teams via Azure Bot Service. The bot handles two independent workflows:

1. **RAG-based Policy Q&A** — Answers natural-language HR policy questions using retrieved document chunks from Azure Cognitive Search, grounded by Claude (Anthropic).
2. **Leave Self-Service via Keka HRMS** — Lets employees check leave balance, apply for leave, view leave history, and cancel leave directly in Teams, backed by the Keka HRMS REST API.

---

## Business Context

### Why This System Exists

Caizin has 100–200 employees, all working inside Microsoft Teams. Before this bot:

- Getting an HR policy answer meant searching documents, messaging HR directly, or waiting for a reply.
- Leave operations (balance check, apply, cancel) required logging into the Keka HRMS portal — a context switch out of daily workflow.

This bot eliminates both friction points by embedding HR self-service inside Teams.

### Impact

| Problem | Without the Bot | With the Bot |
|---|---|---|
| Policy question | Searches docs, messages HR, waits | Types in Teams — cited answer in seconds |
| Check leave balance | Logs into Keka portal | Asks the bot — instant response |
| Apply for leave | Fills Keka portal form | Teams Adaptive Card form or natural language |
| Cancel leave | Logs into Keka, finds the record | Says "cancel my leave from X to Y" |

### Users & Stakeholders

| Actor | Role |
|---|---|
| Caizin Employees | Primary users — policy Q&A and leave self-service |
| HR Team | Policy owners — their documents power the RAG system |
| Engineering / Bot Team | Builds and maintains the integration |
| Managers | Approve/reject leave in Keka portal (out of bot scope) |
| Keka HRMS | External HR system — single source of truth for leave data |

---

## Architecture

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
      ├── teams_bot.py
      │     ├── plain text message ──────────────────────────────────────────────
      │     │                                                                    │
      │     │                                                                    │ Adaptive Card
      │     ▼                                                                    │ form submit
      │   rag.py                                                                 │
      │     ├── Intent: greeting / list_policies ──► direct reply               │
      │     └── Intent: leave / tool use ──────────► Claude tool loop ◄─────────┘
      │                                                      │
      │                                                      ▼
      │                                             tool_registry.py
      │                                                      │
      │                                                      ▼
      │                                               keka/leave.py
      │                                                      │
      │                                                      ▼
      │                                               keka/client.py
      │                                                      │
      │                          ┌───────────────────────────┘
      │                          ▼
      │                    Keka HRMS API
      │
      └── rag.py (policy Q&A path)
            ├── Azure OpenAI  →  query embedding
            └── Azure Cognitive Search  →  vector + keyword retrieval
                      └── Claude (Anthropic)  →  grounded answer generation
```

### Module Responsibilities

| File | Responsibility |
|---|---|
| `main.py` | FastAPI server, `/api/messages` endpoint, Bot Framework adapter |
| `teams_bot.py` | Activity routing, Adaptive Card rendering, Teams UPN resolution |
| `rag.py` | Intent classification, document retrieval, answer generation |
| `tool_registry.py` | Claude tool schema definitions and handler dispatch map |
| `keka/client.py` | OAuth2 token management, HTTP helpers, employee ID resolution |
| `keka/leave.py` | Leave business logic: balance, apply, list, cancel |
| `keka/holidays.py` | Working-day validation, public holiday data, date logic |
| `ingest.py` | PDF/TXT ingestion, chunking, embedding, Azure Search indexing |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.11, Azure App Service (Linux) |
| Web Framework | FastAPI + uvicorn |
| Bot Protocol | Microsoft Bot Framework SDK (`botbuilder-core`) |
| HRMS Integration | Keka HRMS REST API (OAuth2, `requests`) |
| AI / LLM | Anthropic Claude (Haiku) — intent classification, tool use, answer generation |
| RAG Search | Azure Cognitive Search — hybrid vector + BM25 keyword |
| Embeddings | Azure OpenAI `text-embedding-3-small` |
| Policy Storage | Azure Blob Storage (source PDFs) |
| CI/CD | GitHub Actions → Azure Web App |
| Tests | pytest + unittest.mock |

---

## Features

### Policy Q&A
- Natural-language questions answered from indexed HR documents
- Hybrid search (vector + keyword) for high recall
- Answers are grounded — Claude only uses retrieved context, never hallucinates policy
- Every answer includes a citation link and an "verify with HR" disclaimer
- Supports: Leave Policy, Fitness Policy, Travel Policy, Referral Policy, POSH Policy

### Leave Self-Service (Keka)
- **Check Leave Balance** — remaining and used days per leave type
- **Apply for Leave** — via Adaptive Card form or natural language
- **View Leave History** — all requests in a date range with approval status
- **Cancel Leave** — withdraw any pending or approved request

### Teams UX
- Welcome card with quick-action buttons on first message
- Adaptive Card form for leave applications (leave type dropdown, date pickers, reason field)
- Suggested question chips for common policy topics

---

## How It Works

### Policy Q&A (RAG)

```
Employee asks a policy question
  ↓
Intent classification (Claude)
  ├── "greeting"       → direct friendly reply
  ├── "list_policies"  → list all indexed policy document names
  └── "rag"            → proceed to retrieval
           ↓
  Query embedding (Azure OpenAI text-embedding-3-small)
           ↓
  Hybrid search: vector (k=15) + BM25 keyword (top 15) in Azure Cognitive Search
           ↓
  Top chunks + source metadata passed to Claude
           ↓
  Claude generates grounded answer ("Answer ONLY using the provided context")
           ↓
  Response + source link + "Verify with HR" disclaimer sent to employee
```

### Leave Self-Service

**Path A — Natural Language**

```
Employee: "Apply sick leave from June 10 to June 11"
  ↓
Intent classification → RAG path → Claude tool use loop
  ↓
Claude calls: apply_leave { leave_type_name, from_date, to_date, reason }
  ↓
keka/leave.py:
  1. validate_leave_dates()  → weekend / holiday / past date / zero working days check
  2. get_employee_id(email)  → Teams UPN → Keka UUID (paginated lookup, cached)
  3. GET /time/leavetypes    → resolve leave type name to Keka identifier
  4. POST /time/leaverequests → submit leave
  ↓
Claude formats result → Teams
```

**Path B — Adaptive Card Form**

```
Employee clicks "Apply Leave" button
  ↓
teams_bot.py renders Adaptive Card (leave type, from/to date pickers, reason)
  ↓
Employee submits form
  ↓
teams_bot.py reads activity.value directly (bypasses Claude tool loop)
  ↓
keka/leave.py → same validation + Keka API flow as Path A
  ↓
Confirmation message sent to employee
```

**Leave Balance**

```
Employee: "What is my leave balance?" or clicks "My Leave Balance"
  ↓
Claude calls: get_leave_balance { }
  ↓
get_employee_id(email) → Keka UUID
GET /time/leavebalance → org-wide list, filtered to employee UUID
  ↓
Formatted table: leave type | remaining | used | annual quota
```

**Cancel Leave**

```
Employee: "Cancel my leave from June 10 to 12"
  ↓
Claude calls: cancel_leave { from_date, to_date }
  ↓
get_employee_id(email) → Keka UUID
GET /time/leaverequests?from=...&to=... → filter to employee → find first active record
DELETE /time/leaverequests/{id}
  ↓
Confirmation: "Casual Leave from 10 Jun to 12 Jun cancelled"
```

---

## Project Structure

```
caizin-hr-bot/
├── main.py                        # FastAPI server, /api/messages endpoint
├── teams_bot.py                   # Teams activity router, Adaptive Cards
├── rag.py                         # RAG pipeline, intent classification, Claude integration
├── tool_registry.py               # Claude tool definitions and handler dispatch
├── ingest.py                      # Document ingestion into Azure Cognitive Search
├── requirements.txt
├── pytest.ini
│
├── keka/                          # Keka HRMS integration
│   ├── __init__.py
│   ├── client.py                  # OAuth2 token management, HTTP helpers, employee lookup
│   ├── leave.py                   # Leave handlers: balance, apply, history, cancel
│   └── holidays.py                # 2026 public holidays, working-day validation
│
├── zoho/                          # Previous Zoho People integration (deprecated)
│   ├── client.py
│   ├── leave.py
│   └── __init__.py
│
├── tests/
│   ├── conftest.py                # Shared fixtures and mock data
│   ├── test_client.py             # Keka OAuth2 and HTTP helper tests
│   ├── test_leave.py              # 130+ leave handler tests
│   └── test_holidays.py          # Date validation logic tests
│
├── static/
│   ├── privacy.html               # Privacy Policy page
│   └── terms.html                 # Terms of Use page
│
├── artifacts/
│   ├── business-context.md        # Business requirements and workflows
│   └── technical-context.md      # Technical architecture and decisions
│
└── .github/
    └── workflows/
        └── deploy.yml             # GitHub Actions CI/CD → Azure App Service
```

---

## Prerequisites

- Python 3.11+
- An Azure subscription with:
  - Azure App Service (Linux)
  - Azure Bot Service registration
  - Azure Cognitive Search instance
  - Azure OpenAI deployment (`text-embedding-3-small`)
  - Azure Blob Storage (for policy PDFs)
- Keka HRMS account with API access (client ID, client secret, API key)
- Anthropic API key (or AWS Secrets Manager with `caizin/anthropic-api-key`)
- Microsoft Teams tenant with bot app manifest deployed

---

## Setup & Installation

```bash
# Clone the repository
git clone <repo-url>
cd caizin-hr-bot

# Create and activate virtual environment
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and fill in environment variables
cp .env.example .env   # edit .env with your values
```

---

## Environment Variables

Create a `.env` file in the project root (never commit this file):

```env
# Microsoft Bot Framework
MicrosoftAppId=<azure-bot-app-registration-id>
MicrosoftAppPassword=<azure-bot-secret>
MicrosoftAppTenantId=<azure-tenant-id>

# Azure Cognitive Search (RAG)
AZURE_SEARCH_ENDPOINT=https://<your-search-service>.search.windows.net
AZURE_SEARCH_KEY=<admin-key>
AZURE_SEARCH_INDEX=<index-name>

# Azure OpenAI (Embeddings)
AZURE_OPENAI_API_KEY=<api-key>
AZURE_OPENAI_ENDPOINT=https://<your-openai>.openai.azure.com
AZURE_OPENAI_DEPLOYMENT=text-embedding-3-small

# Azure Storage (Policy PDF links in citations)
AZURE_STORAGE_ACCOUNT=<storage-account-name>

# Anthropic Claude
ANTHROPIC_API_KEY=<anthropic-api-key>
# OR use AWS Secrets Manager:
ANTHROPIC_SECRET_NAME=caizin/anthropic-api-key
AWS_REGION=ap-south-1

# Keka HRMS
KEKA_BASE_URL=https://caizin.keka.com/api/v1
KEKA_TOKEN_URL=https://login.keka.com/connect/token
KEKA_CLIENT_ID=<client-id>
KEKA_CLIENT_SECRET=<client-secret>
KEKA_API_KEY=<api-key>

# Server
PORT=8000
```

---

## Running Locally

```bash
# Start the FastAPI server
python main.py

# Or with uvicorn directly
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

The app will be available at `http://localhost:8000`.

To receive Teams messages locally, use a tunneling tool (e.g. [ngrok](https://ngrok.com/)) and configure the messaging endpoint in your Azure Bot Service registration:

```bash
ngrok http 8000
# Set messaging endpoint to: https://<ngrok-id>.ngrok.io/api/messages
```

---

## Ingesting Policy Documents

Policy documents (PDFs or TXT files) are ingested manually using `ingest.py`. Place documents in the source directory, then run:

```bash
python ingest.py
```

This will:
1. Extract text from PDFs using `pypdf` / `pdfplumber`
2. Chunk text into overlapping segments
3. Generate embeddings via Azure OpenAI
4. Upload chunks + embeddings + metadata to Azure Cognitive Search

To clear the existing index before re-ingesting:

```python
# In ingest.py
clear_index()   # deletes all documents from the index
ingest_all()    # re-ingests everything
```

Policy documents currently supported: Leave Policy, Fitness Reimbursement, Travel Policy, Referral Policy, POSH Policy.

---

## Testing

All tests use mocked HTTP — no real API calls are made.

```bash
# Run all tests
pytest

# Run with verbose output (configured in pytest.ini)
pytest -v

# Run a specific test file
pytest tests/test_leave.py

# Run a specific test
pytest tests/test_leave.py::test_apply_leave_success
```

**Test coverage:**

| File | What's tested |
|---|---|
| `tests/test_client.py` | OAuth2 token fetch, token refresh, employee ID pagination, HTTP helpers |
| `tests/test_leave.py` | All four leave handlers — success, error, validation, edge cases (130+ tests) |
| `tests/test_holidays.py` | Weekend detection, holiday lookup, date validation, working-day counting |

---

## Deployment

Deployment is automated via GitHub Actions to Azure App Service.

**Trigger:** Push to the `version1.0` branch

**Pipeline (`.github/workflows/deploy.yml`):**

1. Checkout code
2. Setup Python 3.11
3. Install requirements
4. Azure OIDC login (workload identity — no secrets in workflow)
5. Deploy to Azure Web App

**Manual deploy:**

```bash
# From Azure CLI
az webapp deploy \
  --resource-group <rg-name> \
  --name <app-name> \
  --src-path . \
  --type zip
```

**Runtime configuration on Azure App Service:**

- Python 3.11
- Startup command: `python main.py` or `uvicorn main:app --host 0.0.0.0 --port 8000`
- All environment variables set in App Service → Configuration → Application settings

---

## Security & Access Control

| Concern | Approach |
|---|---|
| Bot identity | Azure Bot Service App ID + Secret (env vars, never in code) |
| Keka credentials | OAuth2 client credentials stored as Azure App Service env vars |
| Employee data scoping | Employee email resolved from authenticated Teams activity context — users can only query their own data |
| No manager operations | Bot exposes no approve/reject endpoints; those remain in Keka portal only |
| Secret rotation | Manual via Azure App Service configuration; no automated rotation |

---

## Business Rules

The bot enforces the following rules before any leave request reaches Keka:

| Rule | Enforced In |
|---|---|
| Cannot apply leave for past dates | `keka/holidays.py:validate_leave_dates()` |
| Cannot start leave on a weekend (Sat/Sun) | `keka/holidays.py:validate_leave_dates()` |
| Cannot start leave on a public holiday | `keka/holidays.py:validate_leave_dates()` |
| Date range must include at least 1 working day | `keka/holidays.py:count_working_days()` |
| Leave type must exist in Keka | `keka/leave.py:_find_leave_type_id()` |
| Employee must exist in Keka | `keka/client.py:get_employee_id()` |
| Policy answers grounded in source documents only | `rag.py` system prompt |

**Supported leave types (from Keka):**
Casual Leave, Sick Leave, Earned Leave, Compensatory Off, Maternity Leave, Paternity Leave, Loss of Pay

---

## Known Issues

| Issue | Severity | Status |
|---|---|---|
| **Keka API field names unverified** — `email`, `id`, `succeeded`, `availableBalance`, `leaveTypeId` etc. are assumed from Postman collection, not validated against live API | High | Open — requires end-to-end test against live Keka |
| **Cancel mechanism unverified** — `DELETE /time/leaverequests/{id}` assumed; may require a PATCH to a status field | Medium | Open |
| **No HTTP retries** — transient 5xx errors surface as error messages to users | Low | Open |
| **Synchronous HTTP blocks event loop** — `requests` library used instead of `httpx`; acceptable at current scale | Low | Planned — migrate to `httpx` if concurrency grows |
| **In-process employee cache** — lost on every deployment or scale-out event | Low | Acceptable for current scale |
| **Hardcoded 2026 holiday list** — several dates marked "confirm exact date" with HR | Medium | Open — planned migration to Keka `/time/holidayscalendar` API |

---

## Out of Scope

The following are explicitly not supported:

- Manager leave approval via the bot (managers use the Keka portal)
- Leave balance validation before submission (Keka's API response handles rejection)
- Performance reviews, payroll, onboarding workflows
- Multi-company or multi-tenant use (built specifically for Caizin)
- Proactive notifications (bot responds reactively only)
- Policy document upload or management through Teams
- Custom leave type creation or quota management (managed entirely in Keka)
- Non-standard work schedules (weekend validation blocks all Sat/Sun regardless of role)

---

## Data Flow & State

The application is **stateless** at the persistence layer — it owns no database.

| Data | Owner | Persistence |
|---|---|---|
| Leave records | Keka HRMS | Permanent (external) |
| Policy documents | Azure Cognitive Search | Permanent — re-indexed via `ingest.py` |
| OAuth2 access token | In-process dict | Process-scoped, auto-refreshed (24h TTL) |
| Employee UUID cache | In-process dict | Process-scoped, lost on restart |
| Conversation history | None | Stateless — no multi-turn memory |

---

*Built for Caizin · Powered by Anthropic Claude, Azure Cognitive Search, and Keka HRMS*
