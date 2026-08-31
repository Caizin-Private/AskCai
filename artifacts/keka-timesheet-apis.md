# Keka APIs behind the Timesheet dashboard

Every Keka call the timesheet dashboard makes, what it takes from each response, and
the translation rules that turn Keka's shapes into the UI contract.

| | |
|---|---|
| UI | [`../static/timesheet-dashboard.html`](../static/timesheet-dashboard.html) |
| Contract | [`timesheet-ui-contract.yaml`](./timesheet-ui-contract.yaml) |
| Translator | [`../keka/timesheet_service.py`](../keka/timesheet_service.py) |
| Credentials & URLs | environment only, read in [`../keka/client.py`](../keka/client.py) — see § 5 |
| Policy & caching | [`../config/keka.yaml`](../config/keka.yaml) (committed; no secrets) |

**Read-only.** Nothing here writes to Keka.

---

## 1. Filters: one employee, one month

Both filters come from the request, not from configuration:

- **Employee** — the caller's email resolves to a Keka employee id, and that id is
  passed as `employeeIds` to every per-employee endpoint. Keka has no email filter,
  so this is a two-step lookup (§ 2.1).
- **Month** — `YYYY-MM` from the path. The service reads the **42-day grid span**
  (the Monday before the 1st through 41 days later), not the calendar month, so the
  grid is filled in one pass. That span is ~42 days, comfortably inside Keka's
  90-day cap on both `/psa/timeentries` and `/time/leaverequests`.

---

## 2. The calls

### 2.1 Resolve the employee — `GET /hris/employees`

Scope: **Employee & Org Information** · [reference](https://developers.keka.com/reference/get_hris-employees)

```
GET /hris/employees?searchKey={email}&employmentStatus=Working&pageSize=200
```

There is **no email query parameter**. `searchKey` (min 3 chars) is a fuzzy text
match, so it only narrows the page — the service then compares `email` exactly, and
falls back to the exhaustive page walk in `find_by_email()` if `searchKey` misses.

Consumed: `id`, `email`, `displayName` / `firstName` / `lastName`, **`holidayCalendarId`**.

`holidayCalendarId` is the reason this call cannot be skipped — it is the only way to
know which holiday calendar the employee follows.

Also present and **not yet used**: `weeklyOffPolicyInfo` (would replace the org-wide
`working_days` list) and `shiftPolicyInfo`.

### 2.2 Project catalogue — `GET /psa/projects`

Scope: **Timesheet** · [reference](https://developers.keka.com/reference/get_psa-projects-1)

```
GET /psa/projects?pageSize=200
```

Tenant-wide, cached under a single key. Consumed: `id`, `name`, `code`.

Needed because the allocation endpoint's own `name` field is not documented as being
the project's name rather than the employee's — so names are taken from here, and
`/psa/project/resources` is used purely as the employee → project link.

### 2.3 Which projects the employee is on — `GET /psa/project/resources`

Scope: **Timesheet** · [reference](https://developers.keka.com/reference/get_psa-project-resources)

```
GET /psa/project/resources?employeeIds={id}&pageSize=200
```

Consumed: `projectId`. Response rows are `{employeeId, projectId, name}`.

One call, and it answers only *which* projects — no dates, no role, no percentage.
That detail comes from § 2.3b.

### 2.3b Assignment detail — `GET /psa/projects/{id}/allocations`

Scope: **Timesheet** · [reference](https://developers.keka.com/reference/get_psa-projects-id-allocations)

```
GET /psa/projects/{projectId}/allocations?pageSize=200
```

Called once per project the employee is on, then **filtered to that employee** — the
response carries every allocation on the project, including other people's.

Consumed: `employee.id` (to filter), `startDate`, `endDate`, `allocationPercentage`,
`billingRole.name`, `isShadow`.
Ignored: `billingRate`, `billingType`, `id`.

**Cached per project, not per employee.** One project's allocation list serves
everyone on it, so three projects shared across fifty people is three cached calls
rather than a hundred and fifty. This is the only endpoint here that fans out per
project instead of being a single call, so that distinction carries the cost.

Two rules the service applies on top:

- **An assignment whose date range does not overlap the month is dropped** — someone
  who left a project in May does not see it in August.
- **Unless hours were logged against it that month**, in which case it is kept even
  if the employee has since been de-allocated. Those hours have to be attributable to
  something. Such a project shows `allocation: null` and no colour slot.

If this call fails the project still renders with its name, colour and hours, and
`allocation: null` — assignment detail is enrichment, not the point of the screen.

### 2.4 Logged hours — `GET /psa/timeentries`

Scope: **Timesheet** · [reference](https://developers.keka.com/reference/get_psa-timeentries)

```
GET /psa/timeentries?employeeIds={id}&from={gridStart}T00:00:00&to={gridEnd}T23:59:59&pageSize=200
```

Consumed: `date`, `projectId`, **`totalMinutes`**, `comments`.
Ignored: `taskId`, `startTime`, `endTime`, `isBillable`, `status`, `identifier`.

Two translation rules worth knowing:

- **`totalMinutes` is minutes.** Hours are `totalMinutes / 60`, rounded to 1dp.
  Nothing in Keka's timesheet API is expressed in hours.
- **Keka allows several entries per project per day.** The calendar shows one row per
  project, so same-project rows on the same date are folded: hours summed, comments
  joined with ` · `. Without this a day with two Cyber entries would render two
  identical-coloured segments.

`comments` (plural, from Keka) becomes `comment` (singular, in the contract).

### 2.5 Holidays — `GET /time/holidayscalendar/{calendarId}/holidays`

Scope: **Attendance** · [reference](https://developers.keka.com/reference/get_time-holidayscalendar-calendarid-holidays)

```
GET /time/holidayscalendar/{holidayCalendarId}/holidays?calendarYear={year}&pageSize=200
```

`calendarId` is the `holidayCalendarId` from § 2.1. Called once per year the grid
touches — a December grid spills into January, so two years.

Consumed: `name`, `date`, **`isFloater`**.

**A floater holiday does not close the day.** It is an optional holiday the employee
chooses whether to take, so it is labelled on the tile — chip reads `Onam (opt)` —
but `day_type` stays `working`, capacity stays the full cap, and logged hours still
count. Only non-floater holidays zero out a day. Flip
`timesheet.treat_floater_holidays_as_closed` if your org treats floaters as
company-wide closures.

`GET /time/holidayscalendar` (list all calendars) is wired as a fallback for an
employee whose record carries no `holidayCalendarId`.

### 2.6 Approved leave — `GET /time/leaverequests`

Scope: **Leave** · [reference](https://developers.keka.com/reference/get_time-leaverequests)

```
GET /time/leaverequests?employeeIds={id}&from={gridStart}T00:00:00&to={gridEnd}T23:59:59&pageSize=200
```

Consumed: `fromDate`, `toDate`, **`fromSession`**, **`toSession`**, **`status`**,
`selection[0].leaveTypeName`.

**Only `status == 1` (Approved) marks a day.** A pending request must not blank out a
day the employee still has to fill.

| `status` | meaning | used |
|---|---|---|
| 0 | Pending | ignored |
| **1** | **Approved** | **marks the day** |
| 2 | Rejected | ignored |
| 3 | Cancelled | ignored |
| 4 | InApprovalProcess | ignored |

**Sessions decide half days.** `SessionType` is 0 = first half, 1 = second half, and
the two sessions qualify only the **first and last** date of a span — interior days
are always whole:

| `fromSession` → `toSession` | Meaning |
|---|---|
| 0 → 1 | whole day (or whole span) |
| 0 → 0 | first half only |
| 1 → 1 | second half only |

A half day halves that date's `capacity_hours` and leaves the remainder loggable,
which is why 4 h against a half-day leave reads as `complete`.

---

## 3. What the UI gets

Translation summary — full field-by-field detail is in the contract.

| Contract | Derived from |
|---|---|
| `employee` | § 2.1 `id`, `displayName` |
| `policy` | `config/keka.yaml` → `timesheet:` (not from Keka yet — see § 6) |
| `projects[]` | § 2.3 ids × § 2.2 names × § 2.3b assignment detail, per employee |
| `projects[].allocation` | § 2.3b `startDate`, `endDate`, `allocationPercentage`, `billingRole.name`, `isShadow` |
| `projects[].has_hours_this_month` | whether § 2.4 has any entry for it in the month |
| `days[].day_type` | § 2.5 non-floater holiday › weekend › § 2.6 approved leave › working |
| `days[].capacity_hours` | cap, halved on half-day leave, `0` on weekend/holiday/full leave |
| `days[].entries[]` | § 2.4, minutes → hours, same-project rows folded |
| `days[].annotation` | § 2.5 holiday name, or § 2.6 leave type + which half |
| `days[].status` | computed here, never in the browser — `missing` needs the server's date |
| `by_project[]` | § 2.4 summed, plus synthetic `Approved leave` / `Public holiday` rows |

`color_slot` is assigned by **alphabetical project name over the employee's full
assignment list**, and only then filtered to the month. Assigning after filtering
would let a project dropping out of one month shift every other project's colour,
which is the one thing the slot exists to prevent. The trade-off is that a slot can
go unused — if the 3rd-alphabetical project is not in this month, a 4th project keeps
`null` rather than taking the free slot. A stable colour beats a dense one.

`allocationPercentage` is displayed but **does not cap anything**. What may be logged
against a project on a given day is governed by `capacity_hours` alone; a 40%
assignment does not mean 3.2 h.

---

## 4. Rate limit and caching

Keka allows **50 requests per minute for the whole tenant**, shared with the leave
flows and anything else on the same API key.

A cold month view costs **five calls plus one per assigned project** (§ 2.3b) — so
eight for someone on three projects. Warm it is usually **one** (`/psa/timeentries`,
60 s TTL), because the per-project allocation lists are cached tenant-wide. Ten people opening the tab cold in the same minute would consume the
tenant's entire quota, so the caches in `keka/dao/_http.py` are load-bearing, not an
optimisation.

| Cached | Key | Default TTL |
|---|---|---|
| Employee | email | 1 h |
| Projects | tenant-wide | 15 min |
| Which projects (§ 2.3) | employee id | 15 min |
| Assignment detail (§ 2.3b) | **project id** | 15 min |
| Holidays | calendar + year | 24 h |
| Leave | employee + span | 5 min |
| Time entries | employee + span | 60 s |

Tune under `keka.cache_ttl` in `config/keka.yaml`, or per bucket with
`KEKA_CACHE_TTL_TIME_ENTRIES` etc.

A Keka `429` is surfaced as the contract's `429 rate_limited` with `Retry-After`
rather than absorbed, so the tab can back off visibly instead of hanging.

Pagination follows the `nextPage` URI Keka returns, as its docs instruct — not
constructed `?pageNumber=N` offsets. (The pre-existing `employee_dao.find_by_email`
still builds offsets; it is untouched because the leave flow depends on it.)

---

## 5. Configuration

### Connecting to Keka — environment only

```
KEKA_CLIENT_ID       KEKA_BASE_URL
KEKA_CLIENT_SECRET   KEKA_TOKEN_URL
KEKA_API_KEY
```

Read in [`../keka/client.py`](../keka/client.py) with `os.getenv`, unchanged from how
this repo always did it. `config/keka.yaml` is **never** consulted for any of them,
and `keka/config.py` does not resolve them either — callers that need a URL or a
credential check import `keka.client`, so there is exactly one place to look.

A secret found in the YAML is dropped and logged as a warning: a credential in a file
on disk is one `git add -f` from being committed.

- **Locally** — `.env`, which `.gitignore` already covers and `keka/client.py` loads.
- **On EC2 / App Service** — the platform's app settings, as today.

`keka.client.missing_secrets()` reports which are unset, and the API's error log names
them rather than reporting a generic outage.

### Policy and caching — `config/keka.yaml`

Committed, because it holds neither secrets nor connection settings: timesheet policy
(daily cap, working days, week start, timezone, floater behaviour) and the cache TTLs.
Each key can also be set as an environment variable, which **wins** over the file — the
env name is documented beside it in the file.

**The API key needs all four scopes.** Scopes are set on the key in Keka Admin, not
per request, so a key missing any of them must be reissued:

| Scope | Needed for |
|---|---|
| Employee & Org Information | § 2.1 |
| Timesheet | § 2.2, § 2.3, § 2.4 |
| Attendance | § 2.5 |
| Leave | § 2.6 |

A 401/403 from any call is reported as "check the API key's scopes in Keka Admin",
because that is overwhelmingly the cause.

### Choosing the data source

`GET /api/timesheet/months/{month}` reads Keka when credentials are present, and the
synthetic `timesheet_mock` when they are not — so local dev needs no Keka access.
Force either with `TIMESHEET_SOURCE=keka|mock`. The response carries
`X-Timesheet-Source: keka|mock` so you can tell which answered.

---

## 6. Open items

1. **`weeklyOffPolicyInfo` is not read.** `policy.working_days` is an org-wide list in
   `config/keka.yaml`. Anyone on a non-Mon–Fri week is classified wrongly until the
   per-employee policy from § 2.1 is used.
2. **`/psa/project/resources` has no date bounds**, so it reports current membership.
   § 2.3b's `startDate`/`endDate` are what actually decide whether a project belongs
   to the month, which covers the common case — but a project the employee joined and
   left entirely before the API's current view would not appear at all unless hours
   were logged against it.
3. **A holiday and approved leave on the same date** cannot both be represented —
   `day_type` is a single enum. Holiday currently wins.
4. **Archived projects.** `/psa/projects` returns `isArchived`; it is not filtered,
   so hours logged historically against an archived project still resolve to a name
   (deliberate — the alternative is a blank row).
5. **Time-entry `status` is ignored.** `/psa/timeentries` returns a 0–5 enum whose
   meanings are undocumented in the reference. If it distinguishes draft from
   approved, the dashboard is currently showing both alike.
6. **90-day cap.** Not hit by a single month, but any future multi-month view must
   chunk its reads.

7. **`billingRate` is not read.** `/psa/projects/{id}/allocations` returns a rate and
   unit per allocation. Deliberately left out — cost data does not belong on an
   employee's own timesheet — but it is there if a manager view ever needs it.
