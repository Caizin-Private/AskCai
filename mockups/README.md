# Mockups

Design reference for development. **Nothing here is served or imported by the app** —
open the files directly in a browser.

| File | What it is |
|---|---|
| `timesheet-mockup.html` | Timesheet tab: interactive prototype + four spec sheets. 91 KB, self-contained, no dependencies. |

---

## timesheet-mockup.html

Five sections. Section 1 is a **working prototype** — click a date, pick a project from
the dropdown, type hours and a comment, Save. Hover any date for its detail card. A
switch in the header runs all five sections through Teams light / dark / high-contrast.

1. **Month view** — the full tab, clickable
2. **Colour system** — day-type tints, fill states, the three project hues
3. **Day cell** — every state, light and dark side by side
4. **Hover detail** — eight variants at real size
5. **Day entry panel** — the 300px rail: adding, day full, over cap, locked

Sample data is August 2026 for one employee. Nothing persists — a reload resets it.

---

## How this relates to what is built

`static/timesheet-dashboard.html` is the real tab. It has since moved **ahead of** this
mockup in some ways and deliberately **behind** it in one:

| | Mockup | Shipped tab |
|---|---|---|
| Data | hardcoded in the file | Keka, or the fixture in test mode |
| Hours entry | **yes** — dropdown, hours, comment, Save | **no** — read-only |
| Day detail | editable panel | read-only panel |
| Project assignments | 3 fixed projects | per employee from Keka, with %, role, dates |
| Themes | all three | all three |

**The entry panel exists only here.** The read API is specified and built; the write
path is not, so `mockups/timesheet-mockup.html` is the only place the hours-entry
interaction can be seen. Use it as the spec when that work starts.

The colour system, day states and hover card in sections 2–5 **are** what shipped — the
tab's tokens were lifted from `static/dashboard.html` and the day states match. Two
things drifted after this file was generated:

- A **floater holiday** annotates a day whose type is still `working`. The shipped tab
  handles it; the mockup has no floater case.
- Project names come from Keka now, so the legend will not read
  Cyber / Conduct / Datasacan for everyone.

---

## Where the rest lives

| | |
|---|---|
| Editable canvas (same design, pan/zoom, multi-artboard) | claude.ai/code/artifact/08678c2d-ab3f-4cc9-bb4d-94c82188bcbb |
| Design-system cards | claude.ai/design → **Caizin InSync — Timesheet** |
| UI ↔ API contract | `../artifacts/timesheet-ui-contract.yaml` |
| Keka endpoints used | `../artifacts/keka-timesheet-apis.md` |
| Example payload | `../artifacts/examples/timesheet-2026-08.json` |

## Regenerating

This file is **generated**, not hand-edited — the prototype's logic is extracted from
the canvas artboards so the spec sheets cannot drift from the prototype. Editing it by
hand will be overwritten. Change the canvas, or ask for a rebuild.
