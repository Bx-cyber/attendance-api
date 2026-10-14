"""Independent pure-Python implementation of the analytics rules (R5-R9), used as an oracle.

It deliberately shares no code with app/main.py: it works on plain dicts and Decimal arithmetic.
"""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

PRESENT = {"PRESENT", "WFH", "ON_DUTY"}


def hu(x, places):
    return float(Decimal(x).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def d(s):
    return date.fromisoformat(s)


def weight(log):
    if log["status"] not in PRESENT:
        return Decimal(0)
    return Decimal("0.5") if log.get("half_day") else Decimal(1)


def month_bounds(month):
    y, m = int(month[:4]), int(month[5:])
    first = date(y, m, 1)
    last = (date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
    return first, last


def in_month(log, month):
    first, last = month_bounds(month)
    return first <= d(log["date"]) <= last


def employee_monthly(emps, logs, code, month):
    first, last = month_bounds(month)
    e = next(x for x in emps if x["emp_code"] == code)
    mine = [l for l in logs if l["emp_code"] == code and in_month(l, month)]
    start = max(first, d(e["joined_on"]))
    wd = sum(1 for i in range((last - start).days + 1) if (start + timedelta(days=i)).weekday() < 5) if start <= last else 0
    present = sum((weight(l) for l in mine if d(l["date"]).weekday() < 5), Decimal(0))
    return {
        "emp_code": code, "month": month, "working_days": wd, "present_days": float(present),
        "leave_days": sum(l["status"] == "LEAVE" for l in mine),
        "late_count": sum((l.get("late_minutes") or 0) > 0 for l in mine),
        "total_late_minutes": sum(l.get("late_minutes") or 0 for l in mine),
        "total_overtime_minutes": sum(l.get("overtime_minutes") or 0 for l in mine),
        "attendance_pct": hu(present / wd * 100, 2) if wd else None,
    }


def department_summary(emps, logs, month, department=None):
    first, last = month_bounds(month)
    out = []
    for dept in sorted({e["department"] for e in emps}):
        if department is not None and dept != department:
            continue
        members = [e for e in emps if e["department"] == dept and d(e["joined_on"]) <= last]
        if not members:
            continue
        codes = {e["emp_code"] for e in members}
        ls = [l for l in logs if l["emp_code"] in codes and in_month(l, month)]
        whs = [Decimal(repr(l["work_hours"])) for l in ls if l["status"] in PRESENT and l.get("work_hours") is not None]
        out.append({
            "department": dept, "headcount": len(members),
            "present_days": float(sum((weight(l) for l in ls if d(l["date"]).weekday() < 5), Decimal(0))),
            "avg_work_hours": hu(sum(whs) / len(whs), 2) if whs else None,
            "late_count": sum((l.get("late_minutes") or 0) > 0 for l in ls),
            "total_late_minutes": sum(l.get("late_minutes") or 0 for l in ls),
            "leave_count": sum(l["status"] == "LEAVE" for l in ls),
            "on_duty_count": sum(l["status"] == "ON_DUTY" for l in ls),
        })
    return out


def late_leaderboard(emps, logs, month, limit=10, department=None):
    by_code = {e["emp_code"]: e for e in emps}
    tot, cnt = {}, {}
    for l in logs:
        if l["emp_code"] in by_code and in_month(l, month):
            lm = l.get("late_minutes") or 0
            tot[l["emp_code"]] = tot.get(l["emp_code"], 0) + lm
            cnt[l["emp_code"]] = cnt.get(l["emp_code"], 0) + (lm > 0)
    rows = [(c, t) for c, t in tot.items() if t > 0 and (department is None or by_code[c]["department"] == department)]
    rows.sort(key=lambda r: (-r[1], r[0]))
    out = []
    for c, t in rows:
        rank = 1 + sum(1 for _, t2 in rows if t2 > t)
        if rank <= limit:
            out.append({"rank": rank, "emp_code": c, "name": by_code[c]["name"], "department": by_code[c]["department"],
                        "total_late_minutes": t, "late_count": cnt[c]})
    return out


def department_trend(emps, logs, department, start, end):
    members = [e for e in emps if e["department"] == department]
    codes = {e["emp_code"] for e in members}
    s, e_ = d(start), d(end)
    rows, rates = [], []
    for i in range((e_ - s).days + 1):
        day = s + timedelta(days=i)
        ls = [l for l in logs if l["emp_code"] in codes and l["date"] == day.isoformat()]
        head = sum(d(m["joined_on"]) <= day for m in members)
        present = sum((weight(l) for l in ls), Decimal(0))
        wd = day.weekday() < 5
        rate = hu(present / head, 4) if wd and head > 0 else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        rows.append({
            "date": day.isoformat(), "is_working_day": wd, "headcount": head, "present_count": float(present),
            "late_count": sum((l.get("late_minutes") or 0) > 0 for l in ls), "attendance_rate": rate,
            "moving_avg_7d": hu(sum(Decimal(repr(r)) for r in window) / len(window), 4) if window else None,
        })
    return rows
