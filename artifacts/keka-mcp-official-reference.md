# Keka MCP — Official Integration Reference

> **Source:** All content derived exclusively from the official Keka developer portal
> (`developers.keka.com`), official reference docs, and live probing of
> `https://developers.keka.com/mcp` using the MCP JSON-RPC protocol.
> **Date compiled:** 2026-06-15

---

## Table of Contents

1. [MCP Server Overview](#1-mcp-server-overview)
2. [Authentication](#2-authentication)
3. [MCP Tools](#3-mcp-tools)
4. [Available API Specs](#4-available-api-specs)
5. [Base URLs](#5-base-urls)
6. [API Endpoints by Domain](#6-api-endpoints-by-domain)
7. [Confirmed Response Schemas](#7-confirmed-response-schemas)
8. [Scopes](#8-scopes)
9. [Rate Limits & Pagination](#9-rate-limits--pagination)
10. [Webhooks](#10-webhooks)
11. [Integration Checklist](#11-integration-checklist)

---

## 1. MCP Server Overview

| Property | Value |
|---|---|
| **Endpoint** | `https://developers.keka.com/mcp` |
| **Protocol version** | MCP `2024-11-05` |
| **Transport** | HTTP POST — responses delivered as SSE (`text/event-stream`) |
| **Server name** | `Keka API` |
| **Server version** | `1.0` |
| **Server capability** | `tools` with `listChanged: true` |
| **Browser access** | Not supported — returns "This URL can only be accessed with a MCP client" |

### How responses are structured

Every request returns an SSE stream. The data line contains the JSON-RPC response:

```
event: message
data: {"result": { ... }, "jsonrpc": "2.0", "id": <request-id>}
```

Parse every line prefixed with `data:` and deserialize the JSON.

---

## 2. Authentication

Keka uses OAuth2. Before calling any API via the MCP `execute-request` tool,
an access token must be obtained from Keka's identity server.

### 2.1 Credential Setup (Keka Admin UI)

Navigate to:
> **Global Admin Settings → Integrations & Automations → API access → API key**

- Only **Global Admins** can generate API keys
- Select required **scopes** at key creation time (see [Section 8](#8-scopes))
- Optionally set an expiry date; keys have no automatic expiry unless configured

This generates three credentials:
- `client_id`
- `client_secret`
- `api_key`

### 2.2 Flow A — API Key Grant (recommended for server-to-server integrations)

```
POST https://login.keka.com/connect/token
Content-Type: application/x-www-form-urlencoded

grant_type=kekaapi
scope=kekaapi
client_id=<CLIENT_ID>
client_secret=<CLIENT_SECRET>
api_key=<API_KEY>
```

**Response (200):**
```json
{
  "access_token": "<JWT>",
  "expires_in": 86400,
  "token_type": "Bearer",
  "scope": "kekaapi"
}
```

- Token TTL: **86,400 seconds (24 hours)**
- No `refresh_token` is issued in this flow
- Add `User-Agent: Mozilla` header if calling from Python and encountering auth errors

**Sandbox endpoint:** `https://login.kekademo.com/connect/token`

### 2.3 Flow B — OAuth Authorization Code (for App Portal partners)

```
POST https://login.keka.com/connect/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code
client_id=<CLIENT_ID>
client_secret=<CLIENT_SECRET>
redirect_uri=<REDIRECT_URI>
code=<AUTHORIZATION_CODE>
scope=kekaapi offline_access
```

**Response (200):**
```json
{
  "access_token": "<JWT>",
  "expires_in": 86400,
  "token_type": "Bearer",
  "scope": "kekaapi offline_access",
  "refresh_token": "<REFRESH_TOKEN>"
}
```

- Returns a `refresh_token` for session management
- Use the `identity-app-portal` MCP spec to exchange a refresh token for a new access token

### 2.4 Using the Token

Pass the access token as a Bearer token on all subsequent API calls:

```
Authorization: Bearer <access_token>
```

When using the MCP `execute-request` tool, include this header inside the `harRequest.headers` array.

---

## 3. MCP Tools

The Keka MCP server exposes exactly **6 tools**. Confirmed via `tools/list`.

---

### `list-specs`

Lists all available OpenAPI specifications. No parameters required.
Call this to discover which API groups exist.

```json
{ "name": "list-specs", "arguments": {} }
```

**Returns:** Array of `{ "title": "<spec name>" }`

---

### `list-endpoints`

Lists all API paths, HTTP methods, and summaries for a given spec.

```json
{
  "name": "list-endpoints",
  "arguments": { "title": "<spec name>" }
}
```

| Parameter | Type | Required | Description |
|---|---|---|---|
| `title` | string | Yes | OpenAPI spec title from `list-specs` |

**Annotations:** `readOnlyHint: true` — safe to call freely.

---

### `search-endpoints`

Case-insensitive deep search across paths, operations, and parameters.
Use to discover endpoints by keyword without listing the full spec.

```json
{
  "name": "search-endpoints",
  "arguments": { "pattern": "<keyword>" }
}
```

| Parameter | Type | Required | Description |
|---|---|---|---|
| `pattern` | string | Yes | Search keyword (case-insensitive) |

**Annotations:** `readOnlyHint: true`

---

### `get-endpoint`

Returns the full OpenAPI definition for one specific endpoint, including
security schemes, request body schema, and response shapes.

```json
{
  "name": "get-endpoint",
  "arguments": {
    "title": "<spec name>",
    "path": "/hris/employees",
    "method": "GET"
  }
}
```

| Parameter | Type | Required | Description |
|---|---|---|---|
| `title` | string | Yes | OpenAPI spec title |
| `path` | string | Yes | API path, e.g. `/hris/employees` |
| `method` | string | Yes | HTTP method, e.g. `GET`, `POST` |

**Annotations:** `readOnlyHint: true`

---

### `get-server-variables`

Returns the base URL template and variable options (company subdomain, environment) for a spec.

```json
{
  "name": "get-server-variables",
  "arguments": { "title": "<spec name>" }
}
```

| Parameter | Type | Required | Description |
|---|---|---|---|
| `title` | string | Yes | OpenAPI spec title |

**Annotations:** `readOnlyHint: true`

---

### `execute-request`

**The only tool that makes live API calls.**
Executes any Keka API request using an HTTP Archive (HAR) request object.

```json
{
  "name": "execute-request",
  "arguments": {
    "title": "<spec name>",
    "harRequest": {
      "method": "get",
      "url": "https://{company}.keka.com/api/v1/hris/employees",
      "headers": [
        { "name": "Authorization", "value": "Bearer <token>" },
        { "name": "Content-Type", "value": "application/json" }
      ],
      "queryString": [
        { "name": "pageNumber", "value": "1" }
      ],
      "postData": {
        "mimeType": "application/json",
        "text": "{\"employeeId\": \"...\"}"
      }
    }
  }
}
```

#### `harRequest` fields

| Field | Type | Required | Description |
|---|---|---|---|
| `method` | string | Yes | `get`, `post`, `put`, `patch`, `delete`, `options`, `head` |
| `url` | string (URI) | Yes | Full URL including scheme and path |
| `headers` | `[{name, value}]` | No | Request headers (include `Authorization` here) |
| `queryString` | `[{name, value}]` | No | Query parameters |
| `postData` | object | No | Request body — see variants below |

#### `postData` variants

For JSON body:
```json
{
  "mimeType": "application/json",
  "text": "<JSON-serialized string>"
}
```

For form data:
```json
{
  "mimeType": "multipart/form-data",
  "params": [{ "name": "key", "value": "val" }]
}
```

#### Tool annotations

| Annotation | Value | Meaning |
|---|---|---|
| `readOnlyHint` | `false` | Can modify data |
| `destructiveHint` | `true` | May cause irreversible changes |
| `openWorldHint` | `true` | Reaches external systems |
| `taskSupport` | `forbidden` | Cannot be used in long-running/background tasks |

> **Note:** `title` is required on `execute-request`. Always specify the correct spec
> name for the API being called.

---

## 4. Available API Specs

Confirmed from `list-specs`. Pass the exact title string to any tool that requires `title`.

| Spec Title | Domain |
|---|---|
| `Core Hr` | Employees, departments, locations, groups, job titles, exit management |
| `Leave` | Leave requests, balances, types, plans |
| `Attendance` | Attendance records, WFH, On-Duty, shift policies, holiday calendars, regularisation |
| `Payroll` | Salaries, pay cycles, tax declarations, financial details, bonus, FnF |
| `Expense` | Expenses, claims, advance requests, travel desk |
| `PMS` | Goals, reviews, review cycles, badges, praise |
| `PSA` | Projects, clients, tasks, timesheets, invoices, resource allocation |
| `Assets` | Asset allocation, recovery, types, categories, conditions |
| `Document` | Employee documents, e-sign workflows |
| `Skills` | Organisation-wide skills, employee skill assignments |
| `Requisition` | Requisition requests |
| `Helpdesk` | Helpdesk tickets, categories, closing reasons |
| `Keka Hire API` | App configuration endpoint |
| `Keka Hire API (1)` | Jobs, candidates, assessments, preboarding, scorecards |
| `BGV APIs` | Background verification vendor checks |
| `BGV APIs (1)` | Background verification (alternate version) |
| `identity` | OAuth2 authorize, token exchange (`authorization_code`), user info |
| `identity-app-portal` | Refresh token → access token exchange (App Portal) |
| `keka-api-format` | API format and conventions reference |

---

## 5. Base URLs

Confirmed from `get-server-variables` on each spec.

### HR API specs
*(Core Hr, Leave, Attendance, Payroll, Expense, PMS, PSA, Assets, Document, Skills, Requisition, Helpdesk)*

| Environment | URL template |
|---|---|
| Production | `https://{company}.keka.com/api/v1` |
| Sandbox | `https://{company}.kekademo.com/api/v1` |

Replace `{company}` with your Keka subdomain (e.g. `caizin` → `https://caizin.keka.com/api/v1`).

### Identity / Auth specs
*(identity, identity-app-portal)*

| Environment | URL |
|---|---|
| Production | `https://login.keka.com` |
| Sandbox | `https://login.kekademo.com` |

---

## 6. API Endpoints by Domain

All endpoints confirmed via `list-endpoints` on each spec.

---

### 6.1 Core Hr

Base: `https://{company}.keka.com/api/v1`

| Method | Path | Summary |
|---|---|---|
| GET | `/hris/employees` | Get all employees (filterable, paginated) |
| POST | `/hris/employees` | Create an employee |
| GET | `/hris/employees/{employeeId}` | Get one employee by ID |
| PUT | `/hris/employees/personaldetails` | Update personal details |
| PUT | `/hris/employees/jobdetails` | Update job details |
| GET | `/hris/employees/updatefields` | Get all updatable fields |
| POST | `/hris/employees/search` | Search employee by work email or phone |
| POST | `/hris/employees/{id}/exitrequest` | Deactivate employee |
| PUT | `/hris/employees/{id}/exitrequest` | Update deactivation request |
| GET | `/hris/departments` | Get all departments |
| GET | `/hris/locations` | Get all locations |
| GET | `/hris/groups` | Get all groups |
| GET | `/hris/grouptypes` | Get all group types |
| GET | `/hris/jobtitles` | Get all job titles |
| GET | `/hris/currencies` | Get all currencies |
| GET | `/hris/noticeperiods` | Get all notice periods |
| GET | `/hris/contingenttypes` | Get all contingent types |
| GET | `/hris/exitreasons` | Get all exit reasons |
| GET | `/hris/bgv/vendors/{bgvId}/checks` | Get BGV checks for a vendor |
| POST | `/hris/bgv/vendors/{bgvId}/checks` | Add BGV checks for a vendor |
| DELETE | `/hris/bgv/vendors/{bgvId}/checks/{checkId}` | Delete a BGV check |
| GET | `/hris/bgv/{bgvId}/requests` | Get all BGV requests |
| PUT | `/hris/bgv/{bgvId}/requests/{requestId}` | Add BGV request report |

---

### 6.2 Leave

Base: `https://{company}.keka.com/api/v1`

| Method | Path | Summary |
|---|---|---|
| GET | `/time/leavetypes` | Get all leave types |
| GET | `/time/leavebalance` | Get all leave balances |
| GET | `/time/leaverequests` | Get leave requests (max 90-day date range) |
| POST | `/time/leaverequests` | Create a leave request |
| GET | `/time/leaveplans` | Get all leave plans |

---

### 6.3 Attendance

Base: `https://{company}.keka.com/api/v1`

| Method | Path | Summary |
|---|---|---|
| GET | `/time/attendance` | Get attendance records (max 90-day range) |
| GET | `/time/holidayscalendar` | Get all holiday calendars |
| GET | `/time/holidayscalendar/{calendarId}/holidays` | Get holidays for a calendar |
| GET | `/time/shiftpolicies` | Get shift policies |
| GET | `/time/weeklyoffpolicies` | Get weekly-off policies |
| GET | `/time/capturescheme` | Get capture schemes |
| GET | `/time/penalisationpolicies` | Get tracking/penalisation policies |
| GET | `/time/regularisationrequests` | Get regularisation requests |
| GET | `/time/wfh` | Get WFH requests (max 90-day range) |
| POST | `/time/wfh` | Add a WFH request |
| GET | `/time/od` | Get On-Duty requests (max 90-day range) |
| POST | `/time/od` | Add an On-Duty request |
| POST | `/attendance/employee/timeentry` | Add a time entry to attendance summary |
| POST | `/attendance/employee/{routeEmployeeId}/timeentry` | Add time entry by employee ID |

---

### 6.4 Payroll

Base: `https://{company}.keka.com/api/v1`

| Method | Path | Summary |
|---|---|---|
| GET | `/payroll/salaries` | Get all employee salaries |
| GET | `/payroll/salarycomponents` | Get all salary components |
| GET | `/payroll/salarystructures` | Get all salary structures |
| POST | `/payroll/employees/salary` | Add employee salary |
| PUT | `/payroll/employees/salary` | Revise employee salary |
| POST | `/payroll/employees/{routeEmployeeId}/salary` | Add salary (by route ID) |
| PUT | `/payroll/employees/{routeEmployeeId}/salary` | Revise salary (by route ID) |
| POST | `/payroll/salarycomponentsoverride` | Bulk salary component override |
| GET | `/payroll/paygroups` | Get all pay groups |
| GET | `/payroll/paygroups/paycycles` | Get pay cycles |
| GET | `/payroll/paygroups/paycycles/payregister` | Get pay register |
| GET | `/payroll/paygroups/paycycles/paybatches` | Get pay batches |
| GET | `/payroll/paygroups/paycycles/paybatches/payments` | Get batch payments |
| PUT | `/payroll/paygroups/paycycles/paybatches/payments` | Update payment status (max 100 per batch) |
| GET | `/payroll/paybands` | Get all pay bands |
| GET | `/payroll/paygrades` | Get all pay grades |
| GET | `/payroll/bonustypes` | Get all bonus types |
| PUT | `/payroll/paygroups/paycycles/adhoctransactions` | Add ad-hoc transaction |
| GET | `/payroll/employees/taxdeclarations` | Get employee tax declarations |
| PUT | `/payroll/taxdeclaration` | Update individual tax declaration component |
| PUT | `/payroll/employees/taxregime` | Update employee tax regime |
| POST | `/payroll/taxdeclaration/rentalresidence` | Create/update rental residence |
| POST | `/payroll/taxdeclaration/ownresidence` | Update own residence |
| POST | `/payroll/taxdeclaration/incomefromothersources` | Update income from other sources |
| GET | `/payroll/declarations/attachments/downloadurl` | Get attachment download URL |
| GET | `/payroll/employees/financialdetails` | Get financial details (paged) |
| PUT | `/payroll/employees/financialdetails` | Update employee financial details |
| PUT | `/payroll/employees/financialdetails/banks` | Update employee bank details |
| GET | `/payroll/employees/flexibenefits` | Get flexi benefits |
| POST | `/payroll/employees/flexibenefits` | Declare flexi benefits |
| GET | `/payroll/employees/form16` | Get employee Form 16 |
| GET | `/payroll/employees/fnf` | Get employee FnF details |
| GET | `/payroll/employees/componentclaims` | Get employee component claims |
| POST | `/payroll/employees/componentclaims` | Add employee component claim |
| GET | `/banks` | Get all banks |

---

### 6.5 Expense

Base: `https://{company}.keka.com/api/v1`

| Method | Path | Summary |
|---|---|---|
| GET | `/expense/employees/{employeeId}/expenses` | Get all expenses for employee |
| POST | `/expense/employees/{employeeId}/expenses` | Add expense for employee |
| PUT | `/expense/employees/{employeeId}/expenses/{expenseId}` | Update an expense |
| GET | `/expense/employees/expenses` | Get all expenses (org-wide) |
| POST | `/expense/employees/expenses` | Add expense |
| PUT | `/expense/employees/expenses` | Update expense |
| GET | `/expense/{expenseId}/attachment/{attachmentId}` | Get expense attachment download URL |
| GET | `/expense/attachment` | Get expense attachment download URL |
| GET | `/expense/categories` | Get all expense categories |
| GET | `/expense/claims` | Get all expense claims |
| POST | `/expense/claims` | Add an expense claim |
| PUT | `/expense/claims` | Update expense claim payment status |
| PUT | `/expense/claims/{expenseClaimId}` | Update claim payment status (by ID) |
| GET | `/expensepolicies` | Get all expense policies |
| GET | `/traveldesk/advancerequests` | Fetch advance requests |
| PUT | `/traveldesk/advancerequests` | Update advance status |
| PUT | `/traveldesk/advancerequests/{advanceId}` | Update advance status (by ID) |

---

### 6.6 Performance (PMS)

Base: `https://{company}.keka.com/api/v1`

| Method | Path | Summary |
|---|---|---|
| GET | `/pms/goals` | Get all goals |
| PUT | `/pms/goals/{goalId}/progress` | Update goal progress |
| GET | `/pms/timeframes` | Get all time frames |
| GET | `/pms/reviewcycles` | Get all review cycles |
| GET | `/pms/reviewgroups` | Get all review groups |
| GET | `/pms/reviews` | Get all employee reviews |
| GET | `/pms/badges` | Get all badges |
| GET | `/pms/praise` | Get all praise |
| POST | `/pms/praise` | Add praise |

---

### 6.7 PSA (Project Services Automation)

Base: `https://{company}.keka.com/api/v1`

| Method | Path | Summary |
|---|---|---|
| GET | `/psa/projects` | Get all projects |
| POST | `/psa/projects` | Create a project |
| GET | `/psa/projects/{id}` | Get a project |
| PUT | `/psa/projects/{id}` | Update a project |
| GET | `/psa/projects/{id}/allocations` | Get project allocations |
| POST | `/psa/projects/{id}/allocations` | Add a project allocation |
| GET | `/psa/projects/{id}/timeentries` | Get project time entries (max 90 days) |
| GET | `/psa/projects/{projectId}/phases` | Get project phases |
| POST | `/psa/projects/{projectId}/phases` | Create a project phase |
| GET | `/psa/projects/{projectId}/tasks` | Get project tasks |
| POST | `/psa/projects/{projectId}/tasks` | Create a task |
| PUT | `/psa/projects/{projectId}/tasks/{taskId}` | Update a task |
| GET | `/psa/projects/{projectId}/tasks/{taskId}/timeentries` | Get task time entries (max 90 days) |
| GET | `/psa/timeentries` | Get all time entries (max 90 days) |
| POST | `/psa/employees/{employeeId}/timeentries` | Add timesheet entries |
| GET | `/psa/project/resources` | Get all project resources |
| GET | `/psa/clients` | Get all clients |
| POST | `/psa/clients` | Create a client |
| GET | `/psa/clients/{id}` | Get a client |
| PUT | `/psa/clients/{id}` | Update a client |
| GET | `/psa/clients/{id}/billingroles` | Get billing roles for a client |
| GET | `/psa/clients/{clientId}/invoices` | Get invoice billing details |
| POST | `/psa/clients/{clientId}/invoices/{invoiceId}/receivepayment` | Post invoice payment |
| POST | `/clients/{clientId}/creditnote` | Post credit note |
| GET | `/psa/legalentity/{legalEntityId}/taxes` | Get taxes |
| GET | `/psa/legalentity/{legalEntityId}/taxgroups` | Get tax groups |

---

### 6.8 Assets

Base: `https://{company}.keka.com/api/v1`

| Method | Path | Summary |
|---|---|---|
| GET | `/assets` | Get all assets |
| PUT | `/assets/{assetId}/allocation` | Update asset assignment |
| PUT | `/assets/{assetId}/recover` | Recover an asset |
| GET | `/assets/types` | Get all asset types |
| GET | `/assets/categories` | Get all asset categories |
| GET | `/assets/conditions` | Get all asset conditions |

---

### 6.9 Document

Base: `https://{company}.keka.com/api/v1`

| Method | Path | Summary |
|---|---|---|
| GET | `/hris/documents/types` | Get document types |
| GET | `/hris/employees/documents` | Get employee documents |
| POST | `/hris/employees/documents` | Upload employee documents |
| GET | `/hris/employees/documents/attachment` | Get document attachment download URL |
| GET | `/hris/e-sign/{vendorId}/letterrequests` | Get e-sign workflow requests |
| GET | `/hris/e-sign/{vendorId}/letterrequests/{letterRequestId}/attachments` | Get e-sign document |
| PUT | `/hris/e-sign/{vendorId}/letterrequests/{letterRequestId}` | Upload digitally signed document |

---

### 6.10 Skills

Base: `https://{company}.keka.com/api/v1`

| Method | Path | Summary |
|---|---|---|
| GET | `/hris/skills` | Get all organisation skills |
| GET | `/hris/employees/{employeeId}/skills` | Get employee skills |
| POST | `/hris/employees/{employeeId}/skills` | Add employee skills |

---

### 6.11 Requisition

Base: `https://{company}.keka.com/api/v1`

| Method | Path | Summary |
|---|---|---|
| GET | `/requisition/requests` | Get all requisition requests |

---

### 6.12 Keka Hire (Recruitment)

Base: `https://{company}.keka.com/api/v1`

| Method | Path | Summary |
|---|---|---|
| GET | `/v1/hire/jobs` | Get all jobs (published, confidential, archived) |
| GET | `/v1/hire/jobs/{jobId}/applicationfields` | Get application fields for a job |
| GET | `/v1/hire/jobs/{jobId}/candidates` | Get candidates for a job |
| POST | `/v1/hire/jobs/{jobId}/candidate` | Post a candidate to a job |
| PUT | `/v1/hire/jobs/{jobId}/candidate/{candidateId}` | Update a candidate |
| POST | `/v1/hire/jobs/{jobId}/candidate/{candidateId}/notes` | Add candidate note |
| GET | `/v1/hire/jobs/{jobId}/candidate/{candidateId}/interviews` | Get candidate interviews |
| GET | `/v1/hire/jobs/{jobId}/candidate/{candidateId}/scorecards` | Get candidate scorecards |
| GET | `/v1/hire/jobs/candidate/{candidateId}/resume` | Get candidate resume |
| POST | `/v1/hire/jobs/candidate/{candidateId}/resume` | Upload candidate resume |
| GET | `/v1/hire/jobboards` | Get all job boards |
| GET | `/v1/hire/preboarding/candidates` | Get all preboarding candidates |
| POST | `/v1/hire/preboarding/candidates` | Add a preboarding candidate |
| PUT | `/v1/hire/preboarding/candidates/{id}` | Update a preboarding candidate |
| GET | `/v1/hire/{vendorId}/assessments` | Get vendor assessments |
| POST | `/v1/hire/{vendorId}/assessments` | Add assessments |
| PUT | `/v1/hire/{vendorId}/assessments/{assessmentId}` | Update assessments |
| DELETE | `/v1/hire/{vendorId}/assessments/{assessmentId}` | Delete assessments |
| GET | `/v1/hire/{vendorId}/assessmentrequests` | Get assessment requests |
| POST | `/v1/hire/{vendorId}/assessmentrequests/{assessmentRequestId}` | Add assessment result |

---

### 6.13 Identity (OAuth / SSO)

Base: `https://login.keka.com`

| Method | Path | Summary |
|---|---|---|
| GET | `/connect/authorize` | OAuth2 authorize endpoint |
| POST | `/connect/token` | Exchange authorization code for tokens |
| GET | `/connect/userinfo` | Fetch authenticated user details |

---

## 7. Confirmed Response Schemas

Extracted directly from the OpenAPI definitions returned by `get-endpoint`.

---

### 7.1 Standard Paged Response wrapper

All list endpoints return this envelope:

```json
{
  "succeeded": true,
  "message": "string | null",
  "errors": ["string"],
  "data": [ <items> ],
  "pageNumber": 1,
  "pageSize": 100,
  "totalPages": 5,
  "totalRecords": 450,
  "nextPage": "https://...?pageNumber=2 | null",
  "previousPage": "https://... | null"
}
```

> `nextPage: null` means the current page is the last page.

---

### 7.2 Standard Single-item Response wrapper

```json
{
  "succeeded": true,
  "message": "string | null",
  "errors": ["string"],
  "data": "<item or id string>"
}
```

---

### 7.3 Employee Profile (`EmployeeProfile`)

Key fields confirmed from `GET /hris/employees` OpenAPI schema:

```json
{
  "id":                 "string (UUID)",
  "employeeNumber":     "string",
  "firstName":          "string",
  "middleName":         "string",
  "lastName":           "string",
  "displayName":        "string",
  "email":              "string",
  "personalEmail":      "string",
  "workPhone":          "string",
  "joiningDate":        "date-time",
  "dateOfBirth":        "date-time",
  "exitDate":           "date-time",
  "resignationSubmittedDate": "date-time",
  "employmentStatus":   0 | 1,
  "accountStatus":      0 | 1 | 2,
  "jobTitle":           { "identifier": "string", "title": "string" },
  "reportsTo":          { "id": "string", "firstName": "string", "lastName": "string", "email": "string" },
  "holidayCalendarId":  "string",
  "leavePlanInfo":      { "identifier": "string", "title": "string" },
  "shiftPolicyInfo":    { "identifier": "string", "title": "string" },
  "groups":             [{ "id": "string", "title": "string", "groupType": 0 }],
  "customFields":       [{ "id": "string", "title": "string", "type": "string", "value": "string" }]
}
```

`GET /hris/employees` query parameters:

| Parameter | Type | Description |
|---|---|---|
| `employeeIds` | string | Comma-separated employee IDs |
| `employeeNumbers` | string | Comma-separated employee numbers |
| `employmentStatus` | string | `Working`, `Relieved` |
| `inProbation` | boolean | Filter probation employees |
| `inNoticePeriod` | boolean | Filter notice period employees |
| `lastModified` | date-time | ISO 8601 modified-since filter |
| `searchKey` | string | Min 3 characters |
| `pageNumber` | integer | Page number |
| `pageSize` | integer | Default 100, max 200 |

---

### 7.4 Create Leave Request (`PostLeaveRequest`)

All fields marked **required** unless noted.

```json
{
  "employeeId":   "string — Keka employee UUID (required)",
  "requestedBy":  "string — Keka employee UUID of requester (required)",
  "fromDate":     "string — ISO 8601 date-time (required)",
  "toDate":       "string — ISO 8601 date-time (required)",
  "leaveTypeId":  "string — leave type identifier (required)",
  "reason":       "string (required)",
  "fromSession":  0 | 1,
  "toSession":    0 | 1,
  "note":         "string (optional)"
}
```

`SessionType` enum: `0` = first half, `1` = second half.

**Response `data` field:** the created leave request ID as a string.

---

## 8. Scopes

Scopes are set at the **API key level** in Keka Admin — not per request.
The access token inherits the scopes of the key used to generate it.

| Scope | Grants access to |
|---|---|
| Employee & Org Information | Employee data, departments, groups, org structure |
| Leave | Leave balances, requests, types, plans |
| Attendance | Attendance records, WFH, On-Duty, shifts, holidays |
| Payroll | Salary, pay cycles, tax declarations, financial details |
| Timesheet | PSA timesheet entries |
| Performance | Goals, reviews, badges, praise |

---

## 9. Rate Limits & Pagination

### Rate Limit

- **50 requests per minute**, uniform across all endpoints
- On breach: HTTP `429` with reason `rateLimitExceeded`
- Recovery: wait ~60 seconds for the quota to refill automatically

### Pagination

- Default page size: **100 records**; max page size: **200**
- Default page number: **1**
- Navigate by following the `nextPage` URI returned in each response
- `nextPage: null` = final page; do not request further
- Do not manually construct `?pageNumber=N` offset URLs — use the returned reference URIs

---

## 10. Webhooks

> Keka pushes event notifications to a public-facing HTTPS endpoint you register.

### Setup

1. Log in as Global Admin
2. Navigate to **Settings → Communications → Event Triggers**
3. Select an event trigger → click **+ Add Action** → choose **Webhook**
4. Provide a name and the public URL to receive `POST` requests
5. Optionally add custom headers

### Available Events (confirmed from official docs)

| Event | Trigger |
|---|---|
| `EmployeeSalaryUpdated` | An employee's salary is modified |
| `leaverequestcreated` | An employee submits a leave request |
| `exitCancelled` | An approved employee exit is cancelled |

### Payload

All webhook calls are HTTP `POST` containing these form-encoded parameters:

| Field | Description |
|---|---|
| `employeeIdentifier` | Unique Keka employee identifier |
| `eventType` | The triggered event category |
| `subDomain` | Organisation subdomain from Keka URL |

Event-specific data is included as a JSON object alongside these fields.

### Requirements

- The receiving endpoint must be **publicly accessible** (no private/localhost URLs)
- Signature verification and retry behaviour are not documented in the official docs

---

## 11. Integration Checklist

### Prerequisites

- [ ] Active Keka subscription with the API add-on feature enabled
- [ ] Global Admin access to the Keka tenant
- [ ] API key created with required scopes (minimum: **Employee & Org Information** + **Leave**)
- [ ] Three credentials obtained: `client_id`, `client_secret`, `api_key`
- [ ] Company subdomain known (e.g. `caizin` for `caizin.keka.com`)

### Token Layer

- [ ] Implement `POST https://login.keka.com/connect/token` with the 5 form params
- [ ] Cache the returned Bearer token (TTL = 86,400s; refresh ~60s before expiry)
- [ ] Store credentials as environment variables — never in source code

### MCP Connection

Choose one approach:

**Option A — Agentic (via Anthropic `mcp_servers` parameter):**
- [ ] Pass `url: https://developers.keka.com/mcp` and `authorization_token: <Bearer token>` to Claude
- [ ] Enable tools: `search-endpoints` + `execute-request`
- [ ] Claude autonomously handles tool-call loop

**Option B — Programmatic (direct JSON-RPC):**
- [ ] `POST https://developers.keka.com/mcp` with `Content-Type: application/json` and `Accept: application/json, text/event-stream`
- [ ] Send `{"jsonrpc":"2.0","id":<n>,"method":"tools/call","params":{"name":"execute-request","arguments":{...}}}`
- [ ] Parse the `data:` line from the SSE response and extract `result.content[0].text`

### Per-Request Requirements for `execute-request`

- [ ] `title` — exact spec name from `list-specs` (e.g. `"Leave"`, `"Core Hr"`)
- [ ] `harRequest.url` — full URL: `https://{company}.keka.com/api/v1{path}`
- [ ] `harRequest.headers` — must include `Authorization: Bearer <token>`
- [ ] `harRequest.method` — lowercase
- [ ] `harRequest.postData` — for `POST`/`PUT` requests
- [ ] `harRequest.queryString` — for `GET` query filters

### Key API Calls for HR Self-Service

| Operation | Spec | Method | Path | Required inputs |
|---|---|---|---|---|
| Find employee by email | `Core Hr` | POST | `/hris/employees/search` | `{ "workEmail": "..." }` |
| Get leave types | `Leave` | GET | `/time/leavetypes` | — |
| Get leave balance | `Leave` | GET | `/time/leavebalance` | `employeeId` query param |
| Get leave history | `Leave` | GET | `/time/leaverequests` | `from`, `to` (max 90 days) |
| Apply leave | `Leave` | POST | `/time/leaverequests` | `employeeId`, `requestedBy`, `fromDate`, `toDate`, `leaveTypeId`, `reason` |
| Get holiday calendar | `Attendance` | GET | `/time/holidayscalendar/{id}/holidays` | calendar ID from employee profile |
| Get attendance records | `Attendance` | GET | `/time/attendance` | `from`, `to` (max 90 days) |

### Error Handling

- [ ] Handle HTTP `401` — token expired; re-fetch and retry
- [ ] Handle HTTP `429` — rate limit; wait 60s before retry
- [ ] Check `succeeded: false` in response body for application-level errors
- [ ] Check `errors[]` array for field-level validation messages
