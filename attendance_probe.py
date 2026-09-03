#!/usr/bin/env python
"""
Read-only. Shows what the tracker DB actually holds for one employee over one
month's calendar grid, beside what the timesheet calendar makes of each row.

Answers "is the data missing, or is the app dropping it?" — nothing here writes.

    python attendance_probe.py you@caizin.com 2026-09
"""
import sys
from datetime import date, timedelta

from psycopg2.extras import RealDictCursor

import insync_db
from insync_db import _bucket
import timesheet_attendance as TA


def grid(month: str):
    """The same 42-day span the calendar asks for: Monday-start, six weeks."""
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    start = first - timedelta(days=first.weekday())
    return [start + timedelta(days=i) for i in range(42)]


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: python attendance_probe.py you@caizin.com 2026-09")
    email, month = sys.argv[1].strip().lower(), sys.argv[2]
    days = grid(month)
    frm, to = days[0].isoformat(), days[-1].isoformat()
    print(f"employee : {email}")
    print(f"grid     : {frm} .. {to}  ({len(days)} days)\n")

    # 1. Is the employee row itself unambiguous?
    conn = insync_db._get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT employee_id, name, email, is_active FROM employees WHERE lower(email) = %s",
            (email,),
        )
        emps = cur.fetchall()
    print(f"employees rows matching that email: {len(emps)}")
    for e in emps:
        print(f"   {e['employee_id']}  {e['name']!r}  is_active={e['is_active']!r}")
    if not emps:
        sys.exit("\nNo employees row — get_attendance_range() can never return anything.")
    if len(emps) > 1:
        print("   !! more than one row; the join will mix their attendance together")
    print()

    # 2. Raw rows, exactly as get_attendance_range reads them.
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT a.date, a.employee_id, a.status, a.employee_response, a.response_time
              FROM attendance a
              JOIN employees e ON a.employee_id = e.employee_id
             WHERE lower(e.email) = %s AND a.date >= %s AND a.date <= %s
             ORDER BY a.date
            """,
            (email, frm, to),
        )
        rows = cur.fetchall()
    by_date = {str(r["date"])[:10]: r for r in rows}
    print(f"attendance rows in that span: {len(rows)}\n")

    print(f"{'date':<12}{'wd':<5}{'in_month':<10}{'status':<26}{'response':<14}"
          f"{'resp_time':<12}{'-> bucket':<18}calendar")
    print("-" * 120)
    y, m = int(month[:4]), int(month[5:7])
    counts = {"row + shown": 0, "row, suppressed": 0, "NO ROW": 0}
    for d in days:
        iso = d.isoformat()
        in_month = (d.year, d.month) == (y, m)
        r = by_date.get(iso)
        if not r:
            counts["NO ROW"] += 1
            print(f"{iso:<12}{d.strftime('%a'):<5}{str(in_month):<10}{'—':<26}{'—':<14}"
                  f"{'—':<12}{'—':<18}no mark  (no row in DB)")
            continue
        bkt = _bucket(r["status"], r["employee_response"])
        cst = TA._STATUS.get(bkt)
        # why the calendar may still not show it
        if not in_month:
            why, ok = "suppressed: out-of-month grid day", False
        elif d.weekday() >= 5:
            why, ok = "suppressed: weekend", False
        elif cst is None:
            why, ok = f"suppressed: bucket {bkt!r} unmapped", False
        else:
            why, ok = f"shows as {cst!r}", True
        counts["row + shown" if ok else "row, suppressed"] += 1
        print(f"{iso:<12}{d.strftime('%a'):<5}{str(in_month):<10}{str(r['status'])[:25]:<26}"
              f"{str(r['employee_response'] or '—')[:13]:<14}"
              f"{str(r['response_time'] or '—')[:11]:<12}{bkt:<18}{why}")

    print()
    for k, v in counts.items():
        print(f"  {k:<18}{v}")
    nulls = sum(1 for r in rows if not r["employee_response"])
    print(f"\n  rows with employee_response NULL : {nulls} of {len(rows)}")
    if rows and nulls == len(rows):
        print("  -> no card response has ever been stored for this employee;")
        print("     every mark you see is derived from `status` alone.")


if __name__ == "__main__":
    main()
