# Timesheet calendar — UI contract notes

Companion to [`timesheet-ui-contract.yaml`](./timesheet-ui-contract.yaml). The YAML is the contract;
this records **why it is shaped that way**.

- UI it serves: [`../static/timesheet-dashboard.html`](../static/timesheet-dashboard.html)
- Worked example: [`examples/timesheet-2026-08.json`](./examples/timesheet-2026-08.json)

**Scope: read only.** One `GET` fills the calendar. Writing hours is phase 2 and is
deliberately absent — see § 6.

---

## 1. Keka is not in this contract

The API composes the response from several upstream systems — projects and logged time,
the holiday calendar, approved leave, the employee record. Which systems, how many calls,
and how they are cached is the API's private business.

Nothing upstream leaks into the payload: no vendor ids, no vendor field names, no vendor
error codes. Two consequences worth stating:

- The UI can be built and tested against this document and the example payload alone,
  with no Keka access and no Keka credentials.
- The upstream can be replaced without touching the UI.

The translation layer, the caching, and the upstream rate limit are real work — they are
just work on the other side of this boundary.

---

## 2. Who owns what

| | Owns |
|---|---|
| **API** | dates, hours, day classification, capacity, completion status, names, comments |
| **UI** | colours, copy, layout, tooltip phrasing, the calendar grid |

One exception in each direction, both deliberate:

- **`annotation.label` is data, not copy.** "Raksha Bandhan", "Casual Leave" — these come
  from upstream records. The UI cannot invent them, so the API supplies them even though
  they end up as display text.
- **`color_slot` is assigned by the API** even though colour is a UI concern, because a
  slot must stay stable for a project across months. If the UI assigned slots by array
  position, a project absent in July and present in August would shift every other
  project's colour. The UI maps slot → hex; the API only guarantees stability.

---

## 3. The response is a grid, not a list of records

`days` is always **42 entries** — six weeks, in display order, starting on the weekday
that begins the week containing the 1st. Days outside the month carry `in_month: false`.

Two reasons:

- **The UI does no calendar arithmetic.** That is where timezone bugs breed:
  `new Date("2026-08-27")` parses as UTC in JavaScript and silently shifts a day for
  anyone west of Greenwich. `weekday`, `day_of_month` and `is_today` are all server-supplied
  for the same reason — the organisation runs on IST, the browser does not necessarily.
- **A fixed 42 means the grid never changes height** between months.

A day with `in_month: false` is always reported inert — `capacity_hours: 0`,
`status: not_applicable`, `entries: []`, `annotation: null` — whatever it may be in its own
month. Without that rule 30 July arrives as `missing`, paints red inside the August grid,
and gets counted in an attention list for a month it does not belong to.

---

## 4. Why `status` is computed server-side

`status` is the one field a reviewer might expect the UI to derive. It encodes policy:

| status | meaning |
|---|---|
| `complete` | logged equals capacity |
| `partial` | some hours, under capacity |
| `empty` | working day, nothing logged, not yet past |
| `missing` | **past** working day, nothing logged |
| `not_applicable` | weekend, holiday, full-day leave, or out of month |

`missing` is the distinction that matters — it drives the red treatment and the attention
count — and it is the one the UI cannot safely make, because it depends on the server's
`today`, not the browser's clock.

`capacity_hours` is on the same footing: a half-day leave reduces it to 4 h, which is a
rule, not arithmetic the client should repeat. The UI sizes each cell's bar track against
the day's own `capacity_hours`, so a half-day fills its track at 4 h.

The tab renders these; it does not derive them.

---

## 5. Two counts that are not the same

- `working_days` — weekdays in the month. **21** in August 2026.
- `days_expected` — weekdays where work was actually expected: `working_days` less
  holidays and full-day leave. **18**.

The rail reads "Logged across 16 of 18 working days", so it needs `days_expected`. Without
it the UI would have to re-walk the array to say it, and would likely get it subtly wrong.

Also: `by_project` includes synthetic rows for holiday and leave time (`project_id: null`)
so the rows sum **exactly** to `totals.logged_hours`. A rail whose parts do not add up to
its own header is a bug users spot immediately.

---

## 6. Deliberately not here

- **Writing hours.** No `PUT`, no `POST`. The entry panel in the mockup is not backed by
  this contract yet.
- **Submission and approval.** Not designed. When it arrives it is a period resource plus
  a state on the month, not a field on a day.
- **`/context`.** Projects and policy are folded into the month response so one call fills
  the calendar. If the entry panel arrives and needs the project list before a month
  loads, this splits out — cheap to do later, not worth the extra round trip now.

## 7. Open questions

1. **Is `capacity_hours` on a half-day leave always half the cap**, or does the leave
   record carry its own duration? The contract assumes half.
2. **Can a holiday and approved leave land on the same date?** The contract's `day_type`
   is a single enum, so it cannot represent both. Currently holiday wins.
3. **Are working days org-wide or per employee?** `policy.working_days` is per response
   either way; nothing decides whether it can vary between people.
4. **How far back can a month be requested?** Upstream time-entry reads are commonly
   capped at 90 days, which would make older months unavailable rather than empty — a
   distinction the UI needs to render differently.
