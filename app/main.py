"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables always win)

Layout of this file
  1. Configuration, database and indexes
  2. Time and rounding helpers (business rules R1 - R5, R8)
  3. Request validation and models
  4. Serialisation (MongoDB documents -> API JSON)
  5. Endpoints: system, employees, attendance
  6. Analytics: pipeline builders and endpoints
  7. Explain
"""
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration, database and indexes
# --------------------------------------------------------------------------- #
load_dotenv()  # does not override variables that are already set in the real environment


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"environment variable {name} is not set (see README.md)")
    return value


# tz_aware=True: PyMongo returns BSON dates as timezone-aware UTC datetimes instead of naive ones.
client = MongoClient(_required_env("MONGO_URI"), tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[_required_env("MONGO_DB")]
employees = db["employees"]
attendance = db["attendance_logs"]

INDEXES = {
    "employees": [
        # Identity of an employee; makes concurrent duplicate creates fail atomically (one 201, the rest 409).
        ([("emp_code", 1)], {"name": "uniq_emp_code", "unique": True}),
        # GET /employees?department=..., and the department filter of every analytics endpoint.
        ([("department", 1), ("emp_code", 1)], {"name": "department_emp_code"}),
        # Department summary without a department filter: employees who joined by the end of the month.
        ([("joined_on", 1)], {"name": "joined_on"}),
    ],
    "attendance_logs": [
        # One record per (emp_code, date): makes punch-in race-safe; serves per-employee lookups and $lookup joins.
        ([("emp_code", 1), ("date", 1)], {"name": "uniq_emp_code_date", "unique": True}),
        # GET /attendance sort order (date desc, emp_code asc) and month-wide scans such as the leaderboard.
        ([("date", -1), ("emp_code", 1)], {"name": "date_desc_emp_code"}),
        # Punch-out: the employee's most recent record whose punch_in is at or before the punch-out time.
        ([("emp_code", 1), ("punch_in", -1)], {"name": "emp_code_punch_in_desc"}),
    ],
}

_indexes_ready = False
_indexes_lock = threading.Lock()


def ensure_indexes() -> None:
    """Creates every index once. create_index is idempotent, so restarts are safe."""
    global _indexes_ready
    with _indexes_lock:
        if _indexes_ready:
            return
        for collection, specs in INDEXES.items():
            for keys, options in specs:
                db[collection].create_index(keys, **options)
        _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep starting; /health reports 503 and retries until MongoDB is reachable
        log.warning("could not create indexes at startup: %s", exc)
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# 2. Time and rounding helpers (R1 - R5, R8)
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30), "IST")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_EPOCH_MS = 100_000_000_000  # anything smaller looks like seconds
MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01
GRACE = timedelta(minutes=10)  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_BELOW = Decimal("4.50")  # R5
MAX_SHIFT_LENGTH = timedelta(hours=24)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6
STATUSES = PRESENCE_STATUSES + ("ABSENT", "LEAVE")
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


def ms_to_datetime(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def datetime_to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:  # defensive: BSON dates are UTC even if a client was created without tz_aware
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def half_up(value: Decimal, places: int) -> Decimal:
    """R8: round half-up (Python's round() is banker's rounding and works on binary floats)."""
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def is_overnight(emp: dict) -> bool:
    return parse_hhmm(emp["shift_end"]) <= parse_hhmm(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: the IST calendar date of the punch-in; for an overnight shift a punch-in before shift_end belongs to the
    shift that started the previous day."""
    local = punch_in.astimezone(IST)
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_window(day: date, emp: dict) -> tuple[datetime, datetime]:
    """Shift start and end instants of the shift that starts on `day` (IST). Overnight shifts end the next day."""
    start = datetime.combine(day, parse_hhmm(emp["shift_start"]), IST)
    end = datetime.combine(day, parse_hhmm(emp["shift_end"]), IST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, day: date, emp: dict) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then whole minutes since shift start."""
    elapsed = punch_in - shift_window(day, emp)[0]
    return elapsed // timedelta(minutes=1) if elapsed > GRACE else 0


def compute_overtime_minutes(punch_out: datetime, day: date, emp: dict) -> int:
    """R3: whole minutes (floor) after shift end, counted only when at least 30."""
    minutes = (punch_out - shift_window(day, emp)[1]) // timedelta(minutes=1)
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: worked seconds / 3600, 2 decimals half-up. Done in Decimal so 2.675 does not become 2.67."""
    seconds = (punch_out - punch_in) // timedelta(seconds=1)
    return half_up(Decimal(seconds) / Decimal(3600), 2)


def derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], day: date,
                   emp: dict) -> dict:
    """The four stored, API-computed fields for a record (DATA_MODEL.md, "Stored derived values")."""
    fields = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE_STATUSES or punch_in is None:
        return fields
    fields["late_minutes"] = compute_late_minutes(punch_in, day, emp)
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        fields["work_hours"] = float(hours)
        fields["overtime_minutes"] = compute_overtime_minutes(punch_out, day, emp)
        fields["half_day"] = hours < HALF_DAY_BELOW  # R5 uses the rounded value
    return fields


# --------------------------------------------------------------------------- #
# 3. Request validation and models
# --------------------------------------------------------------------------- #
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
SHIFT_TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


def _check_date(value: str) -> str:
    # date.fromisoformat alone also accepts "20260706" and "2026-W27-1", so the shape is checked first.
    if not DATE_PATTERN.fullmatch(value):
        raise ValueError("must be a date in YYYY-MM-DD format")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("is not a valid calendar date") from None
    return value


DateStr = Annotated[str, AfterValidator(_check_date)]
# strict=True rejects floats (1.7e12), numeric strings and booleans; the bounds reject second-based values.
EpochMillis = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def validation_error(loc: tuple, msg: str, value: Any = None):
    """Raises a 422 with FastAPI's standard validation body, for checks that span several inputs."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=SHIFT_TIME_PATTERN)] = "09:30"
    shift_end: Annotated[str, Field(pattern=SHIFT_TIME_PATTERN)] = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shift_has_length(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    emp_code: Annotated[str, Field(min_length=1)]
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: Annotated[str, Field(min_length=1)]
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    """Only status and the punch times can be corrected; derived fields sent by a client are ignored."""
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        # The fields are optional but not nullable: "omitted" means unchanged, an explicit null is invalid.
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} may be omitted but not null")
        return self


# --------------------------------------------------------------------------- #
# 4. Serialisation (MongoDB documents -> API JSON)
# --------------------------------------------------------------------------- #
EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")
EMPLOYEE_PROJECTION = {"_id": 0}
RECORD_PROJECTION = {"_id": 0}


def _to_api(value: Any) -> Any:
    return datetime_to_ms(value) if isinstance(value, datetime) else value


def employee_out(doc: dict) -> dict:
    out = {field: doc.get(field) for field in EMPLOYEE_FIELDS}
    out["created_at"] = datetime_to_ms(doc.get("created_at"))
    return out


def history_out(entry: dict) -> dict:
    return {
        "at": datetime_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            field: {"from": _to_api(change.get("from")), "to": _to_api(change.get("to"))}
            for field, change in (entry.get("changes") or {}).items()
        },
    }


def record_out(doc: dict) -> dict:
    """Seeded records may omit history, half_day and the minute counters: missing means [] / false / 0."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_ms(doc.get("punch_in")),
        "punch_out": datetime_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_out(entry) for entry in doc.get("history") or []],
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, EMPLOYEE_PROJECTION)
    if emp is None:
        raise HTTPException(404, f"employee {emp_code} not found")
    return emp


# --------------------------------------------------------------------------- #
# 5. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes()  # no-op once done; finishes the job if MongoDB was down at startup
    except PyMongoError:
        raise HTTPException(503, "database unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    now = datetime.now(UTC)
    doc["created_at"] = now.replace(microsecond=now.microsecond // 1000 * 1000)  # BSON dates hold milliseconds
    try:
        employees.insert_one(doc)  # the unique index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, f"emp_code {body.emp_code} already exists") from None
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {"department": department} if department is not None else {}
    cursor = (employees.find(query, EMPLOYEE_PROJECTION).sort("emp_code", 1)
              .skip((page - 1) * page_size).limit(page_size))
    items = [employee_out(doc) for doc in cursor]
    return {"items": items, "total": employees.count_documents(query), "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee_or_404(body.emp_code)
    punched = ms_to_datetime(body.punched_at) if body.punched_at is not None else utc_now()
    day = attendance_date(punched, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        **derived_fields(body.status, punched, None, day, emp),
        "history": [],
    }
    try:
        attendance.insert_one(doc)  # unique (emp_code, date): of two simultaneous punch-ins exactly one succeeds
    except DuplicateKeyError:
        raise HTTPException(409, f"{body.emp_code} has already punched in for {doc['date']}") from None
    return record_out(doc)


def attendance_list_sort(emp_code: Optional[str]) -> list:
    """Contract order is date desc, emp_code asc. With one emp_code the tie-breaker is constant, and sorting by
    date alone lets MongoDB walk the (emp_code, date) index backwards instead of sorting in memory."""
    return [("date", -1)] if emp_code is not None else [("date", -1), ("emp_code", 1)]


def attendance_list_query(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                          status: Optional[str]) -> dict:
    if date_from and date_to and date_from > date_to:
        validation_error(("query", "date_from"), "date_from must not be after date_to", date_from)
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query = attendance_list_query(emp_code, date_from, date_to, status)
    cursor = (attendance.find(query, RECORD_PROJECTION).sort(attendance_list_sort(emp_code))
              .skip((page - 1) * page_size).limit(page_size))
    items = [record_out(doc) for doc in cursor]
    # An unfiltered count over ~100k documents is answered from collection metadata instead of a scan.
    total = attendance.count_documents(query) if query else attendance.estimated_document_count()
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee_or_404(body.emp_code)
    punched = ms_to_datetime(body.punched_at) if body.punched_at is not None else utc_now()
    # The latest record that started at or before this punch-out. Looking up by punch_in rather than by today's
    # date is what makes an overnight shift (in at 22:00, out at 06:40 the next day) close the right record.
    record = attendance.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(404, f"no punch-in found for {body.emp_code} at or before this time")
    if record.get("punch_out") is not None:
        raise HTTPException(409, f"the record of {record['date']} is already punched out")
    if punched <= record["punch_in"]:
        validation_error(("body", "punched_at"), "punched_at must be after punch_in", body.punched_at)
    if punched - record["punch_in"] > MAX_SHIFT_LENGTH:
        validation_error(("body", "punched_at"), "punched_at is more than 24 hours after punch_in", body.punched_at)

    day = date.fromisoformat(record["date"])
    update = {"punch_out": punched, **derived_fields(record["status"], record["punch_in"], punched, day, emp)}
    # Conditional update: only one of several simultaneous punch-outs can still see punch_out == null.
    updated = attendance.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": update},
        projection=RECORD_PROJECTION,
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, f"the record of {record['date']} is already punched out")
    return record_out(updated)


CORRECTABLE_FIELDS = ("status", "punch_in", "punch_out")
TRACKED_FIELDS = CORRECTABLE_FIELDS + ("work_hours", "late_minutes", "overtime_minutes", "half_day")
FIELD_DEFAULTS = {"punch_in": None, "punch_out": None, "work_hours": None, "late_minutes": 0,
                  "overtime_minutes": 0, "half_day": False}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, day: Annotated[DateStr, Path(alias="date")], body: RegularizeRequest):
    emp = get_employee_or_404(emp_code)
    record = attendance.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise HTTPException(404, f"no attendance record for {emp_code} on {day}")

    # Missing or null derived fields in seeded data mean their defaults, so they do not show up as "changes".
    old = {field: record.get(field) for field in TRACKED_FIELDS}
    old.update({field: default for field, default in FIELD_DEFAULTS.items() if old[field] is None})
    old["half_day"] = bool(old["half_day"])
    status = body.status or old["status"]
    if status in PRESENCE_STATUSES:
        punch_in_at = ms_to_datetime(body.punch_in) if body.punch_in is not None else old["punch_in"]
        punch_out_at = ms_to_datetime(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if punch_in_at is None:
            validation_error(("body", "punch_in"), f"status {status} requires a punch_in")
        if body.punch_in is not None and attendance_date(punch_in_at, emp).isoformat() != day:
            validation_error(("body", "punch_in"), f"punch_in must fall on the attendance date {day} (R1)",
                             body.punch_in)
        if punch_out_at is not None and not timedelta(0) < punch_out_at - punch_in_at <= MAX_SHIFT_LENGTH:
            validation_error(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours",
                             body.punch_out)
    else:
        if body.punch_in is not None or body.punch_out is not None:
            validation_error(("body",), f"status {status} clears the punch times; do not send punch_in/punch_out")
        punch_in_at = punch_out_at = None

    new = {"status": status, "punch_in": punch_in_at, "punch_out": punch_out_at,
           **derived_fields(status, punch_in_at, punch_out_at, date.fromisoformat(day), emp)}
    if all(new[field] == old[field] for field in CORRECTABLE_FIELDS):
        validation_error(("body",), "the request does not change anything")
    changes = {field: {"from": old[field], "to": new[field]} for field in TRACKED_FIELDS if new[field] != old[field]}
    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the update only applies if the record still looks exactly as it did when we read
    # it (same corrected fields, same number of history entries). A concurrent edit makes this match nothing,
    # so the loser gets 409 instead of silently overwriting the winner's change or history entry.
    guard = {"_id": record["_id"], **{field: record.get(field) for field in CORRECTABLE_FIELDS}}
    if "history" in record:
        guard["history"] = {"$size": len(record["history"])}
    else:
        guard["history"] = {"$exists": False}
    updated = attendance.find_one_and_update(
        guard,
        {"$set": new, "$push": {"history": entry}},
        projection=RECORD_PROJECTION,
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "the record was changed by another request; reload it and try again")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 6. Analytics: pipeline builders and endpoints
#
# Each endpoint's pipeline is built by a function so that /admin/explain runs exactly the same pipeline.
# The pipelines read the stored derived fields (never recompute them from punch times) and treat a missing
# half_day / late_minutes / overtime_minutes / work_hours as false / 0 / 0 / null.
# --------------------------------------------------------------------------- #
Month = Annotated[str, Query(pattern=MONTH_PATTERN, description="Calendar month, YYYY-MM")]


def month_bounds(month: str) -> tuple[date, date]:
    first = date.fromisoformat(f"{month}-01")
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, next_month - timedelta(days=1)


def count_working_days(start: date, end: date) -> int:
    """R7: Monday-Friday between start and end inclusive (no holiday calendar). Pure calendar arithmetic."""
    if start > end:
        return 0
    full_weeks, extra_days = divmod((end - start).days + 1, 7)
    return full_weeks * 5 + sum(1 for i in range(extra_days) if (start.weekday() + i) % 7 < 5)


def to_decimal(value: Any) -> Decimal:
    return value.to_decimal() if isinstance(value, Decimal128) else Decimal(str(value))


def rounded(value: Any, places: int = 2) -> Optional[float]:
    return None if value is None else float(half_up(to_decimal(value), places))


# Aggregation expressions over one attendance record. `ref` turns a field name into a reference, so the same
# expression works on the top-level document ("$status") and inside $map ("$$log.status").
def _field(name: str) -> str:
    return f"${name}"


def expr_is_present(ref=_field) -> dict:
    return {"$in": [ref("status"), list(PRESENCE_STATUSES)]}


def expr_is_weekday(ref=_field) -> dict:
    # $isoDayOfWeek: Monday = 1 ... Sunday = 7. The "YYYY-MM-DD" string is read as a plain calendar date.
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": ref("date"), "format": "%Y-%m-%d"}}}, 5]}


def expr_present_value(ref=_field, weekdays_only: bool = True) -> dict:
    """R5 - R7: a present-status record is worth 1 day, or 0.5 when it is a half day."""
    condition = {"$and": [expr_is_present(ref), expr_is_weekday(ref)]} if weekdays_only else expr_is_present(ref)
    return {"$cond": [condition, {"$cond": [{"$ifNull": [ref("half_day"), False]}, 0.5, 1]}, 0]}


def expr_late_minutes(ref=_field) -> dict:
    return {"$ifNull": [ref("late_minutes"), 0]}


def expr_count_if(condition: dict) -> dict:
    return {"$cond": [condition, 1, 0]}


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": expr_present_value()},
            "leave_days": {"$sum": expr_count_if({"$eq": ["$status", "LEAVE"]})},
            "late_count": {"$sum": expr_count_if({"$gt": [expr_late_minutes(), 0]})},
            "total_late_minutes": {"$sum": expr_late_minutes()},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = get_employee_or_404(emp_code)
    stats = next(attendance.aggregate(employee_monthly_pipeline(emp_code, month)), {})
    first, last = month_bounds(month)
    working_days = count_working_days(max(first, date.fromisoformat(emp["joined_on"])), last)
    present_days = to_decimal(stats.get("present_days", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": rounded(present_days),
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": rounded(present_days * 100 / working_days) if working_days else None,
    }


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    # Start from employees, not from logs: an employee with zero logs still produces a row and is counted in
    # the headcount (R9), and someone who joins after the month never matches.
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department

    def log(name: str) -> str:
        return f"$$log.{name}"

    def per_employee(expression: dict) -> dict:
        return {"$sum": {"$map": {"input": "$logs", "as": "log", "in": expression}}}

    has_hours = {"$and": [expr_is_present(log), {"$ne": [{"$ifNull": [log("work_hours"), None]}, None]}]}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",  # with the date range below: served by the (emp_code, date) index
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "work_hours": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {
            "department": 1,
            "present_days": per_employee(expr_present_value(log)),
            # Decimal sums so the mean is exact before it is rounded half-up.
            "hours_sum": per_employee({"$cond": [has_hours, {"$toDecimal": log("work_hours")}, 0]}),
            "hours_count": per_employee(expr_count_if(has_hours)),
            "late_count": per_employee(expr_count_if({"$gt": [expr_late_minutes(log), 0]})),
            "total_late_minutes": per_employee(expr_late_minutes(log)),
            "leave_count": per_employee(expr_count_if({"$eq": [log("status"), "LEAVE"]})),
            "on_duty_count": per_employee(expr_count_if({"$eq": [log("status"), "ON_DUTY"]})),
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "hours_sum": {"$sum": "$hours_sum"},
            "hours_count": {"$sum": "$hours_count"},  # average over records, not an average of averages
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    items = []
    for row in employees.aggregate(department_summary_pipeline(month, department)):
        hours_count = int(row["hours_count"])
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": rounded(row["present_days"]),
            "avg_work_hours": rounded(to_decimal(row["hours_sum"]) / hours_count) if hours_count else None,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list = [
        # Only late records can contribute, so filter them out early (date range served by date_desc_emp_code).
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}],
            "as": "employee",
        }},
        {"$unwind": "$employee"},  # drops logs whose employee does not exist
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})  # rank inside the department only
    pipeline += [
        # Standard competition ranking: ties share a rank and the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        # limit applies to the rank, not to the number of rows, so a tie at the cutoff returns every tied row.
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipeline


Limit = Annotated[int, Query(ge=1, le=50)]


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Limit = 10, department: Optional[str] = None):
    items = [
        {**row, "rank": int(row["rank"]), "total_late_minutes": int(row["total_late_minutes"]),
         "late_count": int(row["late_count"])}
        for row in attendance.aggregate(late_leaderboard_pipeline(month, limit, department))
    ]
    return {"month": month, "items": items}


def expr_half_up(expression: Any, places: int) -> dict:
    """Half-up rounding inside the database. $round rounds half to even, so floor(x * 10^n + 0.5) / 10^n is used,
    on Decimal128 values so that the tie is exact."""
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expression, factor]}, Decimal128("0.5")]}}, factor]}


MAX_TREND_DAYS = 92


def check_trend_range(start: str, end: str) -> tuple[date, date]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        validation_error(("query", "to"), "to must not be before from", end)
    if (last - first).days + 1 > MAX_TREND_DAYS:
        validation_error(("query", "to"), f"the range may cover at most {MAX_TREND_DAYS} days", end)
    return first, last


def department_trend_pipeline(department: str, first: date, last: date) -> list:
    start, end = first.isoformat(), last.isoformat()

    def log(name: str) -> str:
        return f"$$log.{name}"

    def event(day: Any, joined: Any = 0, present: Any = 0, late: Any = 0) -> dict:
        return {"date": day, "joined": joined, "present": present, "late": late}

    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        # Turn every employee into dated events: one "joined" event (headcount grows from that day on, R9) and
        # one event per log. The anchor event on `from` guarantees there is a row to densify from.
        {"$project": {"_id": 0, "events": {"$concatArrays": [
            [event(start)],
            {"$cond": [{"$lte": ["$joined_on", end]}, [event({"$max": ["$joined_on", start]}, joined=1)], []]},
            {"$map": {"input": "$logs", "as": "log", "in": event(
                log("date"),
                present=expr_present_value(log, weekdays_only=False),  # any weekday or weekend
                late=expr_count_if({"$gt": [expr_late_minutes(log), 0]}),
            )}},
        ]}}},
        {"$unwind": "$events"},
        {"$group": {
            "_id": "$events.date",
            "joined": {"$sum": "$events.joined"},
            "present_count": {"$sum": "$events.present"},
            "late_count": {"$sum": "$events.late"},
        }},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # Gap filling: exactly one row per calendar day in [from, to] (the upper bound of $densify is exclusive).
        {"$densify": {"field": "day", "range": {
            "step": 1, "unit": "day",
            "bounds": [datetime.combine(first, time(), UTC), datetime.combine(last + timedelta(days=1), time(), UTC)],
        }}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "headcount": {"$sum": "$joined", "window": {"documents": ["unbounded", "current"]}},
        }}},
        {"$set": {
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            expr_half_up({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
            None,
        ]}}},
        # $avg skips nulls; the window only sees rows inside the requested range, so the first rows use fewer.
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}},
        }}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
    ]


FromDate = Annotated[DateStr, Query(alias="from")]
ToDate = Annotated[DateStr, Query(alias="to")]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, start: FromDate, end: ToDate):
    first, last = check_trend_range(start, end)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, f"department {department} has no employees")
    items = [
        {
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": int(row["headcount"]),
            "present_count": rounded(row["present_count"]),
            "late_count": int(row["late_count"]),
            "attendance_rate": rounded(row["attendance_rate"], 4),
            "moving_avg_7d": rounded(row["moving_avg_7d"], 4),
        }
        for row in employees.aggregate(department_trend_pipeline(department, first, last))
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 7. Explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                        "department_trend"]


def _require(name: str, value: Any) -> Any:
    if value is None:
        validation_error(("query", name), f"{name} is required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: Limit = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    start: Annotated[Optional[DateStr], Query(alias="from")] = None,
    end: Annotated[Optional[DateStr], Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    """Runs explain (executionStats) on the same query the named endpoint runs, built by the same builder."""
    if endpoint == "attendance_list":
        collection = attendance
        command = {
            "find": collection.name,
            "filter": attendance_list_query(emp_code, date_from, date_to, status),
            "projection": RECORD_PROJECTION,
            "sort": dict(attendance_list_sort(emp_code)),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            collection = attendance
            pipeline = employee_monthly_pipeline(_require("emp_code", emp_code), _require("month", month))
        elif endpoint == "department_summary":
            collection = employees
            pipeline = department_summary_pipeline(_require("month", month), department)
        elif endpoint == "late_leaderboard":
            collection = attendance
            pipeline = late_leaderboard_pipeline(_require("month", month), limit, department)
        else:
            collection = employees
            first, last = check_trend_range(_require("from", start), _require("to", end))
            pipeline = department_trend_pipeline(_require("department", department), first, last)
        command = {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection.name, "explain": json.loads(json_util.dumps(result))}
