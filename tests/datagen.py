"""Builds a large, deliberately awkward dataset (about 100k attendance records) for local testing.

Usage:  python -m tests.datagen            (uses MONGO_URI / MONGO_DB; drops both collections first)

Covers the cases the business rules call out: ties on late minutes, weekend records, employees with no logs,
mid-month joiners, people who join after a month, overnight shifts, legacy records without history / half_day,
open records, half days, LEAVE / ABSENT / ON_DUTY / WFH, logs whose employee does not exist.
"""
import os
import random
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

from app.main import IST, attendance_date, derived_fields

DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "HR", "Ops", "engineering"]  # case-sensitive pair
FIRST_DAY, LAST_DAY = date(2025, 11, 1), date(2026, 10, 31)


def build(seed: int = 7, employee_count: int = 500):
    rng = random.Random(seed)
    emps = []
    for i in range(1, employee_count + 1):
        overnight = i % 9 == 0
        shift = ("22:00", "06:00") if overnight else rng.choice([("09:30", "18:30"), ("09:00", "18:00"), ("10:00", "19:00")])
        roll = rng.random()
        if roll < 0.75:
            joined = FIRST_DAY - timedelta(days=rng.randint(0, 400))
        else:  # joins during the data range, often mid-month
            joined = FIRST_DAY + timedelta(days=rng.randint(0, (LAST_DAY - FIRST_DAY).days))
        emps.append({
            "emp_code": f"EMP{i:04d}", "name": f"Person {i}", "email": f"p{i}@example.com",
            "department": DEPARTMENTS[i % len(DEPARTMENTS)], "shift_start": shift[0], "shift_end": shift[1],
            "joined_on": joined.isoformat(), "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        })

    logs = []
    no_log_employees = {e["emp_code"] for e in rng.sample(emps, 25)}
    for emp in emps:
        if emp["emp_code"] in no_log_employees:
            continue
        day = max(FIRST_DAY, date.fromisoformat(emp["joined_on"]))
        while day <= LAST_DAY:
            weekend = day.weekday() >= 5
            if (weekend and rng.random() > 0.08) or (not weekend and rng.random() < 0.06):  # missing weekdays too
                day += timedelta(days=1)
                continue
            status = rng.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [70, 12, 5, 5, 8])[0]
            doc = {"emp_code": emp["emp_code"], "date": day.isoformat(), "status": status}
            if status in ("ABSENT", "LEAVE"):
                doc.update(punch_in=None, punch_out=None, **derived_fields(status, None, None, day, emp))
            else:
                h, m = map(int, emp["shift_start"].split(":"))
                start = datetime(day.year, day.month, day.day, h, m, tzinfo=IST)
                # coarse lateness buckets make ties on monthly totals likely
                offset = rng.choice([-15, -5, 0, 5, 10, 10, 15, 20, 30, 45]) * 60 + rng.choice([0, 0, 1, 59])
                punch_in = (start + timedelta(seconds=offset)).astimezone(timezone.utc)
                worked = rng.choice([3.5, 4.4999, 4.5, 8, 9, 9.25, 9.6, 10.1]) * 3600 + rng.randint(0, 900)
                punch_out = punch_in + timedelta(seconds=int(worked))
                if day >= LAST_DAY - timedelta(days=2) and rng.random() < 0.5:
                    punch_out = None  # open records
                assert attendance_date(punch_in, emp) == day
                doc.update(punch_in=punch_in, punch_out=punch_out, **derived_fields(status, punch_in, punch_out, day, emp))
            if rng.random() < 0.1:  # legacy shape
                doc.pop("half_day", None)
            else:
                doc["history"] = []
            logs.append(doc)
            day += timedelta(days=1)

    # logs of an employee that does not exist must be ignored by the leaderboard and summaries
    for d in range(1, 28):
        logs.append({"emp_code": "EMP9999", "date": f"2026-07-{d:02d}", "status": "PRESENT",
                     "punch_in": datetime(2026, 7, d, 6, tzinfo=timezone.utc), "punch_out": None,
                     "work_hours": None, "late_minutes": 500, "overtime_minutes": 0, "half_day": False, "history": []})
    return emps, logs


if __name__ == "__main__":
    load_dotenv()
    database = MongoClient(os.environ["MONGO_URI"], tz_aware=True)[os.environ["MONGO_DB"]]
    employees, logs = build()
    for name, docs in (("employees", employees), ("attendance_logs", logs)):
        database[name].drop()
        database[name].insert_many(docs)
    print(f"{len(employees)} employees, {len(logs)} attendance records loaded into {database.name}")
