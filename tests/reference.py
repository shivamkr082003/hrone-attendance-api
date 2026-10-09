"""A deliberately naive, independent re-statement of the analytics rules in plain Python loops.

The API computes these numbers with aggregation pipelines; the tests compare both on a generated dataset. This
code is the "obviously correct but slow" version and is only used by the tests.
"""
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

PRESENT = {"PRESENT", "WFH", "ON_DUTY"}


def q(value, places):
    return float(Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def month_days(month):
    first = date.fromisoformat(month + "-01")
    days = []
    while first.strftime("%Y-%m") == month:
        days.append(first)
        first += timedelta(days=1)
    return days


def presence_value(log):
    return Decimal("0.5") if log.get("half_day") else Decimal(1)


def is_weekday(day_str):
    return date.fromisoformat(day_str).weekday() < 5


def monthly(emp, month, logs):
    mine = [l for l in logs if l["emp_code"] == emp["emp_code"] and l["date"].startswith(month)]
    present = sum((presence_value(l) for l in mine if l["status"] in PRESENT and is_weekday(l["date"])), Decimal(0))
    working = sum(1 for d in month_days(month) if d.weekday() < 5 and d.isoformat() >= emp["joined_on"])
    return {
        "emp_code": emp["emp_code"], "month": month, "working_days": working, "present_days": q(present, 2),
        "leave_days": sum(1 for l in mine if l["status"] == "LEAVE"),
        "late_count": sum(1 for l in mine if (l.get("late_minutes") or 0) > 0),
        "total_late_minutes": sum(l.get("late_minutes") or 0 for l in mine),
        "total_overtime_minutes": sum(l.get("overtime_minutes") or 0 for l in mine),
        "attendance_pct": q(present * 100 / working, 2) if working else None,
    }


def summary(month, employees, logs, department=None):
    last = month_days(month)[-1].isoformat()
    by_dept = {}
    for emp in employees:
        if emp["joined_on"] <= last and (department is None or emp["department"] == department):
            by_dept.setdefault(emp["department"], set()).add(emp["emp_code"])
    items = []
    for dept in sorted(by_dept):
        codes = by_dept[dept]
        mine = [l for l in logs if l["emp_code"] in codes and l["date"].startswith(month)]
        hours = [Decimal(str(l["work_hours"])) for l in mine if l["status"] in PRESENT and l.get("work_hours") is not None]
        late = [l for l in mine if (l.get("late_minutes") or 0) > 0]
        items.append({
            "department": dept, "headcount": len(codes),
            "present_days": q(sum((presence_value(l) for l in mine if l["status"] in PRESENT and is_weekday(l["date"])), Decimal(0)), 2),
            "avg_work_hours": q(sum(hours) / len(hours), 2) if hours else None,
            "late_count": len(late), "total_late_minutes": sum(l["late_minutes"] for l in late),
            "leave_count": sum(1 for l in mine if l["status"] == "LEAVE"),
            "on_duty_count": sum(1 for l in mine if l["status"] == "ON_DUTY"),
        })
    return {"month": month, "items": items}


def leaderboard(month, employees, logs, limit=10, department=None):
    emps = {e["emp_code"]: e for e in employees}
    totals = {}
    for l in logs:
        if l["date"].startswith(month) and (l.get("late_minutes") or 0) > 0 and l["emp_code"] in emps:
            if department is None or emps[l["emp_code"]]["department"] == department:
                t = totals.setdefault(l["emp_code"], [0, 0])
                t[0] += l["late_minutes"]
                t[1] += 1
    rows = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
    items = []
    for code, (total, count) in rows:
        rank = 1 + sum(1 for _, (other, _) in rows if other > total)
        if rank <= limit:
            e = emps[code]
            items.append({"rank": rank, "emp_code": code, "name": e["name"], "department": e["department"],
                          "total_late_minutes": total, "late_count": count})
    return {"month": month, "items": items}


def trend(department, start, end, employees, logs):
    members = [e for e in employees if e["department"] == department]
    codes = {e["emp_code"] for e in members}
    by_day = {}
    for l in logs:
        if l["emp_code"] in codes and start <= l["date"] <= end:
            by_day.setdefault(l["date"], []).append(l)
    items, rates = [], []
    day, last = date.fromisoformat(start), date.fromisoformat(end)
    while day <= last:
        ds = day.isoformat()
        todays = by_day.get(ds, [])
        headcount = sum(1 for e in members if e["joined_on"] <= ds)
        present = sum((presence_value(l) for l in todays if l["status"] in PRESENT), Decimal(0))
        working = day.weekday() < 5
        rate = Decimal(str(q(present / headcount, 4))) if working and headcount else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        items.append({
            "date": ds, "is_working_day": working, "headcount": headcount, "present_count": q(present, 2),
            "late_count": sum(1 for l in todays if (l.get("late_minutes") or 0) > 0),
            "attendance_rate": float(rate) if rate is not None else None,
            "moving_avg_7d": q(sum(window) / len(window), 4) if window else None,
        })
        day += timedelta(days=1)
    return {"department": department, "items": items}
