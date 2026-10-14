"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real environment variables win)

Everything lives in this one file, in this order:
    1. configuration and Mongo connection
    2. time helpers and business rules (R1 - R5, R8)
    3. request validation models
    4. serialisation (BSON -> API shapes)
    5. indexes (created idempotently at startup)
    6. endpoints: system, employees, attendance
    7. analytics pipelines and endpoints
    8. admin / explain
"""
import json
import logging
import os
import re
from collections import Counter
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Dict, Literal, Optional, Tuple

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # does not override variables that are already set in the environment

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo hands back UTC-aware datetimes instead of naive ones, so
# arithmetic and epoch conversion can never silently use the server's local zone.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60          # R2
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_BELOW_HOURS = 4.5       # R5
MAX_SHIFT_SECONDS = 24 * 3600    # punch-out / correction window
MAX_PAGE = 1_000_000             # keeps skip() far away from int64 overflow
MAX_TREND_DAYS = 92

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
# Strict: floats ("1.5e12", 1.0), strings and booleans are rejected with 422.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]

HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


# --------------------------------------------------------------------------- #
# 2. Time helpers and business rules
# --------------------------------------------------------------------------- #
def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """BSON datetime -> epoch milliseconds (naive values are taken as UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def trunc_seconds(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.replace(microsecond=0)


def now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_millis() -> datetime:
    """Current time at millisecond precision, which is all BSON can store."""
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def round_half_up(value: Any, places: int = 2) -> float:
    """R8: half-up rounding done on the decimal representation, not on binary floats."""
    dec = value if isinstance(value, Decimal) else Decimal(repr(value))
    return float(dec.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm(shift_end) <= hhmm(shift_start)


def attendance_date(ts: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; overnight shifts that are still running
    after midnight belong to the day the shift started."""
    local = ts.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < hhmm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_window(date_str: str, shift_start: str, shift_end: str) -> Tuple[datetime, datetime]:
    """Start and end of the shift that belongs to attendance date `date_str` (IST-aware)."""
    day = date.fromisoformat(date_str)
    start = datetime.combine(day, hhmm(shift_start), tzinfo=IST)
    end = datetime.combine(day, hhmm(shift_end), tzinfo=IST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then whole minutes
    (floor) since shift start, not since the end of the grace period."""
    start, _ = shift_window(date_str, shift_start, shift_end)
    delta = int((punch_in - start).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, counted only when that is at least 30."""
    _, end = shift_window(date_str, shift_start, shift_end)
    delta = int((punch_out - end).total_seconds())
    return delta // 60 if delta >= MIN_OVERTIME_MINUTES * 60 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals, half-up (exact Decimal arithmetic)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                  date_str: str, emp: dict) -> dict:
    """All four stored derived fields for a record, following the table in DATA_MODEL.md."""
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    s, e = emp["shift_start"], emp["shift_end"]
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, date_str, s, e),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime_minutes(punch_out, date_str, s, e)
        out["half_day"] = out["work_hours"] < HALF_DAY_BELOW_HOURS
    return out


def weekdays_between(first: date, last: date) -> int:
    """Number of Mon-Fri days in [first, last] (R7)."""
    if last < first:
        return 0
    total = (last - first).days + 1
    full_weeks, extra = divmod(total, 7)
    count = full_weeks * 5
    for i in range(extra):
        if (first + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# Query-parameter validation helpers (all failures are FastAPI-style 422s)
# --------------------------------------------------------------------------- #
def fail_422(where: str, name: str, msg: str, value: Any = None):
    raise RequestValidationError([{"type": "value_error", "loc": (where, name), "msg": msg, "input": value}])


def parse_date(value: str, name: str, where: str = "query") -> date:
    if not re.fullmatch(DATE_PATTERN, value):
        fail_422(where, name, "must be a date in YYYY-MM-DD format", value)
    try:
        return date.fromisoformat(value)
    except ValueError:
        fail_422(where, name, "not a real calendar date", value)


def parse_month(value: str, name: str = "month") -> Tuple[date, date]:
    """'YYYY-MM' -> (first day, last day)."""
    if not re.fullmatch(MONTH_PATTERN, value) or int(value[:4]) < 1:
        fail_422("query", name, "must be a month in YYYY-MM format", value)
    year, month = int(value[:4]), int(value[5:])
    first = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return first, nxt - timedelta(days=1)


def require(name: str, value: Any) -> Any:
    if value is None:
        fail_422("query", name, "field required")
    return value


# --------------------------------------------------------------------------- #
# 3. Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Default None means "omitted"; an explicit null is rejected (the schema is not nullable).
    status: Status = Field(default=None)
    punch_in: EpochMs = Field(default=None)
    punch_out: EpochMs = Field(default=None)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 4. Serialisation
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def _history_value(value: Any) -> Any:
    return to_ms(value) if isinstance(value, datetime) else value


def history_out(entry: dict) -> dict:
    changes = {
        field: {"from": _history_value(pair.get("from")), "to": _history_value(pair.get("to"))}
        for field, pair in (entry.get("changes") or {}).items()
    }
    return {"at": to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Stored document -> API record. Missing legacy fields get their documented defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# 5. Indexes
# --------------------------------------------------------------------------- #
ATT_BY_EMP_DATE = "emp_code_1_date_1"
ATT_BY_DATE = "date_-1_emp_code_1_late_minutes_1"
ATT_BY_STATUS = "status_1_date_-1_emp_code_1"
ATT_BY_PUNCH_IN = "emp_code_1_punch_in_-1"
EMP_BY_DEPT_JOINED = "department_1_joined_on_1"
EMP_BY_DEPT_CODE = "department_1_emp_code_1"

_indexes_ready = False


def ensure_indexes() -> bool:
    """Idempotent: create_index is a no-op when an identical index already exists."""
    global _indexes_ready
    specs = [
        (db.employees, [("emp_code", 1)], {"unique": True, "name": "emp_code_1"}),
        (db.employees, [("department", 1), ("joined_on", 1)], {"name": EMP_BY_DEPT_JOINED}),
        (db.employees, [("department", 1), ("emp_code", 1)], {"name": EMP_BY_DEPT_CODE}),
        (db.attendance_logs, [("emp_code", 1), ("date", 1)], {"unique": True, "name": ATT_BY_EMP_DATE}),
        (db.attendance_logs, [("date", -1), ("emp_code", 1), ("late_minutes", 1)], {"name": ATT_BY_DATE}),
        (db.attendance_logs, [("status", 1), ("date", -1), ("emp_code", 1)], {"name": ATT_BY_STATUS}),
        (db.attendance_logs, [("emp_code", 1), ("punch_in", -1)], {"name": ATT_BY_PUNCH_IN}),
    ]
    ok = True
    for coll, keys, opts in specs:
        try:
            coll.create_index(keys, **opts)
        except ConnectionFailure:
            # MongoDB is not reachable: do not wait out the timeout once per index.
            # /health retries ensure_indexes() as soon as the database answers.
            log.warning("MongoDB not reachable, index creation postponed")
            ok = False
            break
        except PyMongoError:
            ok = False
            log.exception("could not create index %s on %s", opts["name"], coll.name)
    _indexes_ready = ok
    return ok


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception:  # MongoDB not reachable yet: /health will keep retrying
        log.exception("index creation at startup failed")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
        log.error("database error: %s: %s", type(exc).__name__, exc)
        return JSONResponse(status_code=503, content={"detail": f"database unavailable ({type(exc).__name__})"})


# --------------------------------------------------------------------------- #
# 6. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    if not _indexes_ready:
        ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code decides, not a prior read
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(body.status, ts, None, day, emp),
        "history": [],
    }
    try:
        # Atomic: the unique (emp_code, date) index lets exactly one of N racing requests win.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()

    # Most recent record that started at or before the punch-out; works across midnight.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = trunc_seconds(rec["punch_in"])
    if ts <= pin:
        raise HTTPException(422, "punched_at must be after the punch-in")
    if int((ts - pin).total_seconds()) > MAX_SHIFT_SECONDS:
        raise HTTPException(422, "punch-out is more than 24 hours after the punch-in")

    derived = derive_fields(rec["status"], pin, ts, rec["date"], emp)
    fields = {"punch_out": ts, **derived}
    # Compare-and-set: only the request that still sees punch_out empty can win.
    res = db.attendance_logs.update_one({"_id": rec["_id"], "punch_out": None}, {"$set": fields})
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    return record_out({**rec, **fields})


def attendance_query(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                     status: Optional[str], page: int, page_size: int) -> dict:
    """Filter / sort / paging for GET /attendance, shared with the explain endpoint."""
    lo = parse_date(date_from, "date_from") if date_from is not None else None
    hi = parse_date(date_to, "date_to") if date_to is not None else None
    if lo and hi and lo > hi:
        fail_422("query", "date_from", "date_from must not be after date_to", date_from)
    flt: Dict[str, Any] = {}
    if emp_code is not None:
        flt["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        flt["date"] = {}
        if date_from is not None:
            flt["date"]["$gte"] = date_from
        if date_to is not None:
            flt["date"]["$lte"] = date_to
    if status is not None:
        flt["status"] = status
    # With one employee fixed, emp_code is constant, so sorting by date alone is the same
    # order and lets the (emp_code, date) index serve the sort backwards.
    sort = {"date": -1} if emp_code is not None else {"date": -1, "emp_code": 1}
    return {"filter": flt, "sort": sort, "skip": (page - 1) * page_size, "limit": page_size}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    flt = spec["filter"]
    total = db.attendance_logs.count_documents(flt) if flt else db.attendance_logs.estimated_document_count()
    cursor = (
        db.attendance_logs.find(flt, {"_id": 0})
        .sort(list(spec["sort"].items()))
        .skip(spec["skip"])
        .limit(spec["limit"])
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


CHANGE_FIELDS = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date", where="path")
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old = {
        "status": rec["status"],
        "punch_in": trunc_seconds(rec.get("punch_in")),
        "punch_out": trunc_seconds(rec.get("punch_out")),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day") or False),
    }
    new_status = body.status if body.status is not None else old["status"]

    if new_status in PRESENCE_STATUSES:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else old["punch_in"]
        new_out = from_ms(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_in is None:
            raise HTTPException(422, f"status {new_status} requires a punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if int((new_out - new_in).total_seconds()) > MAX_SHIFT_SECONDS:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
    else:  # ABSENT / LEAVE clear both punch times
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"status {new_status} cannot carry punch times")
        new_in = new_out = None

    if (new_status, new_in, new_out) == (old["status"], old["punch_in"], old["punch_out"]):
        raise HTTPException(422, "the request changes nothing")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derive_fields(new_status, new_in, new_out, date, emp)}
    changes = {f: {"from": old[f], "to": new[f]} for f in CHANGE_FIELDS if old[f] != new[f]}
    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the update only applies if the record is still exactly what we
    # read (same status, punch times and number of history entries). Every successful PATCH
    # appends one entry, so a concurrent PATCH makes this match fail -> 409, never a lost entry.
    n_hist = len(rec.get("history") or [])
    hist_guard = {"history": {"$size": n_hist}} if n_hist else {
        "$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}]
    }
    guard = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        **hist_guard,
    }
    res = db.attendance_logs.update_one(guard, {"$set": new, "$push": {"history": entry}})
    if res.matched_count == 0:
        raise HTTPException(409, "the record was modified concurrently, please retry")
    return record_out({**rec, **new, "history": list(rec.get("history") or []) + [entry]})


# --------------------------------------------------------------------------- #
# 7. Analytics: shared pipeline fragments
# --------------------------------------------------------------------------- #
# Stored derived values are authoritative, so these read late_minutes / half_day /
# work_hours as stored (missing -> 0 / false) and never recompute from punch times.
LATE = {"$ifNull": ["$late_minutes", 0]}
IS_PRESENT = {"$in": ["$status", PRESENCE_STATUSES]}
IS_HALF_DAY = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
# "Present days" kept in half-days (integers) so sums are exact; divide by 2 at the end.
PRESENT_HALVES = {"$cond": [IS_PRESENT, {"$cond": [IS_HALF_DAY, 1, 2]}, 0]}


def _count_if(cond: dict) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


def log_stats_accumulators() -> dict:
    """$group accumulators shared by the employee summary and the department summary."""
    return {
        "present_halves": {"$sum": {"$cond": [IS_WEEKDAY, PRESENT_HALVES, 0]}},   # R7: Mon-Fri only
        # work_hours kept as integer hundredths so the later mean is exact
        "wh_hundredths": {"$sum": {"$cond": [
            {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]},
            {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]}},
        "wh_count": _count_if({"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}),
        "late_count": _count_if({"$gt": [LATE, 0]}),
        "total_late_minutes": {"$sum": LATE},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "leave_count": _count_if({"$eq": ["$status", "LEAVE"]}),
        "on_duty_count": _count_if({"$eq": ["$status", "ON_DUTY"]}),
    }


def month_range(month: str) -> Tuple[date, date, str, str]:
    first, last = parse_month(month)
    return first, last, first.isoformat(), last.isoformat()


# ---- employee monthly ------------------------------------------------------ #
def monthly_pipeline(emp_code: str, month: str) -> list:
    _, _, lo, hi = month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": lo, "$lte": hi}}},
        {"$group": {"_id": None, **log_stats_accumulators()}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last, _, _ = month_range(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)))
    s = rows[0] if rows else {}
    joined = date.fromisoformat(emp["joined_on"])
    working_days = weekdays_between(max(first, joined), last)
    halves = s.get("present_halves", 0)
    pct = None
    if working_days > 0:  # present_days / working_days * 100 == halves * 50 / working_days
        pct = round_half_up(Decimal(halves * 50) / Decimal(working_days), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": halves / 2,
        "leave_days": s.get("leave_count", 0),
        "late_count": s.get("late_count", 0),
        "total_late_minutes": s.get("total_late_minutes", 0),
        "total_overtime_minutes": s.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


# ---- department summary ---------------------------------------------------- #
def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    _, _, lo, hi = month_range(month)
    emp_match: Dict[str, Any] = {"joined_on": {"$lte": hi}}          # R9
    if department is not None:
        emp_match["department"] = department
    return [
        # Start from employees so people with zero logs are still counted.
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": lo, "$lte": hi}}},
                {"$group": {"_id": None, **log_stats_accumulators()}},
            ],
            "as": "s",
        }},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            **{k: {"$sum": {"$ifNull": [f"$s.{k}", 0]}} for k in (
                "present_halves", "wh_hundredths", "wh_count", "late_count",
                "total_late_minutes", "leave_count", "on_duty_count")},
        }},
        {"$sort": {"_id": 1}},
    ]


# The hint pins the employees side to the (department, joined_on) index, so even the
# all-departments query is an index scan rather than a collection scan.
DEPT_SUMMARY_OPTIONS = {"hint": EMP_BY_DEPT_JOINED}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    rows = db.employees.aggregate(department_summary_pipeline(month, department), **DEPT_SUMMARY_OPTIONS)
    items = []
    for r in rows:
        avg = None
        if r["wh_count"]:
            avg = round_half_up(Decimal(r["wh_hundredths"]) / Decimal(100 * r["wh_count"]), 2)
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": r["present_halves"] / 2,
            "avg_work_hours": avg,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


# ---- late leaderboard ------------------------------------------------------ #
def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    _, _, lo, hi = month_range(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": lo, "$lte": hi}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": LATE},
                    "late_count": _count_if({"$gt": [LATE, 0]})}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})
    pipeline += [
        # $rank is standard competition ranking: 1, 2, 2, 4. Ranking is computed over
        # the whole (department-scoped) population BEFORE limit is applied.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name",
                      "department": "$e.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))}


# ---- department trend ------------------------------------------------------ #
def trend_inputs(department: str, start: str, end: str) -> Tuple[date, date, list, list]:
    """Validate the range and load the department's employees (404 if there are none)."""
    d_from = parse_date(start, "from")
    d_to = parse_date(end, "to")
    if d_to < d_from:
        fail_422("query", "to", "to must not be before from", end)
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        fail_422("query", "to", f"range must not exceed {MAX_TREND_DAYS} days", end)
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    codes = [e["emp_code"] for e in emps]
    joined = [{"d": d, "n": n} for d, n in sorted(Counter(e["joined_on"] for e in emps).items())]
    return d_from, d_to, codes, joined


def trend_pipeline(codes: list, joined: list, d_from: date, d_to: date) -> list:
    n_days = (d_to - d_from).days + 1
    start_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    day_dt = {"$add": [start_dt, {"$multiply": ["$$i", 86400000]}]}
    window = {"documents": [-6, 0]}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        {"$group": {"_id": "$date", "halves": {"$sum": PRESENT_HALVES}, "late": _count_if({"$gt": [LATE, 0]})}},
        # $facet always emits one document, even when no log matched, so a department with
        # zero logs in the range still gets a full set of (zero) rows.
        {"$facet": {"rows": [{"$sort": {"_id": 1}}]}},
        # Gap filling: one row for every calendar day of the range, generated in the database.
        {"$set": {"days": {"$map": {"input": {"$range": [0, n_days]}, "as": "i", "in": {
            "ds": {"$dateToString": {"format": "%Y-%m-%d", "date": day_dt}},
            "dow": {"$isoDayOfWeek": day_dt},
        }}}}},
        {"$unwind": "$days"},
        {"$replaceRoot": {"newRoot": {"$mergeObjects": [
            "$days",
            {"row": {"$arrayElemAt": [
                {"$filter": {"input": "$rows", "as": "r", "cond": {"$eq": ["$$r._id", "$days.ds"]}}}, 0]}},
        ]}}},
        {"$set": {
            "halves": {"$ifNull": ["$row.halves", 0]},
            "late": {"$ifNull": ["$row.late", 0]},
            "is_wd": {"$lte": ["$dow", 5]},
            # R9: employees whose joined_on is on or before this day
            "headcount": {"$reduce": {
                "input": {"$literal": joined}, "initialValue": 0,
                "in": {"$cond": [{"$lte": ["$$this.d", "$ds"]}, {"$add": ["$$value", "$$this.n"]}, "$$value"]},
            }},
        }},
        # Rate in units of 1/10000, rounded half-up with exact integer arithmetic:
        # k = floor(halves/(2*h) * 10000 + 1/2). Null on weekends or when headcount is 0.
        {"$set": {"k": {"$cond": [
            {"$and": ["$is_wd", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": [2, "$halves", 10000]}, {"$multiply": [2, "$headcount"]}]},
                {"$multiply": [4, "$headcount"]}]}},
            None]}}},
        {"$set": {"kc": {"$cond": [{"$eq": ["$k", None]}, 0, 1]}}},
        # 7-row trailing window inside the requested range only (shorter at the start).
        {"$setWindowFields": {"sortBy": {"ds": 1}, "output": {
            "sk": {"$sum": "$k", "window": window},
            "sc": {"$sum": "$kc", "window": window},
        }}},
        {"$sort": {"ds": 1}},
        {"$project": {
            "_id": 0,
            "date": "$ds",
            "is_working_day": "$is_wd",
            "headcount": 1,
            "present_count": {"$divide": ["$halves", 2]},
            "late_count": "$late",
            "attendance_rate": {"$cond": [{"$eq": ["$k", None]}, None, {"$divide": ["$k", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$gt": ["$sc", 0]}, {"$divide": [
                {"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$sk"]}, "$sc"]}, {"$multiply": [2, "$sc"]}]}},
                10000]}, None]},
        }},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    d_from, d_to, codes, joined = trend_inputs(department, from_, to)
    items = list(db.attendance_logs.aggregate(trend_pipeline(codes, joined, d_from, d_to)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 8. Admin: explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    """Explain (executionStats) of the main query of an endpoint, built by the very same
    builder functions the endpoint itself uses."""
    if endpoint == "attendance_list":
        spec = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command: Dict[str, Any] = {
            "find": collection, "filter": spec["filter"], "sort": spec["sort"],
            "skip": spec["skip"], "limit": spec["limit"], "projection": {"_id": 0},
        }
    else:
        if endpoint == "employee_monthly":
            collection = "attendance_logs"
            pipeline = monthly_pipeline(require("emp_code", emp_code), require("month", month))
            options: Dict[str, Any] = {}
        elif endpoint == "department_summary":
            collection = "employees"
            pipeline = department_summary_pipeline(require("month", month), department)
            options = DEPT_SUMMARY_OPTIONS
        elif endpoint == "late_leaderboard":
            collection = "attendance_logs"
            pipeline = leaderboard_pipeline(require("month", month), limit, department)
            options = {}
        else:  # department_trend
            collection = "attendance_logs"
            d_from, d_to, codes, joined = trend_inputs(require("department", department),
                                                       require("from", from_), require("to", to))
            pipeline = trend_pipeline(codes, joined, d_from, d_to)
            options = {}
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, **options}
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return JSONResponse({
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS)),
    })
