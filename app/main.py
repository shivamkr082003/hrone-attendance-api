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
"""
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import MongoClient
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


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


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
    cursor = (attendance.find(query, RECORD_PROJECTION).sort(ATTENDANCE_SORT)
              .skip((page - 1) * page_size).limit(page_size))
    items = [record_out(doc) for doc in cursor]
    # An unfiltered count over ~100k documents is answered from collection metadata instead of a scan.
    total = attendance.count_documents(query) if query else attendance.estimated_document_count()
    return {"items": items, "total": total, "page": page, "page_size": page_size}
