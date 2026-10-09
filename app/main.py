"""
Employee Attendance & Analytics API
Built with FastAPI and MongoDB for HROne Engineering Assignment.
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import math
import os
from typing import Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# IST Timezone (UTC +05:30)
IST = timezone(timedelta(hours=5, minutes=30))

# MongoDB Connection Configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]


def init_indexes():
    """Idempotently create all required database indexes at application startup."""
    # employees collection indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs collection indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helper Functions & Business Logic (R1 - R10)
# --------------------------------------------------------------------------- #
def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a UTC or timezone-aware datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    """
    Convert integer epoch milliseconds to UTC BSON datetime.
    Truncates to whole seconds per Rule R1.
    """
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def current_epoch_ms() -> int:
    """Return current time in epoch milliseconds truncated to whole seconds (R1)."""
    now_sec = int(datetime.now(timezone.utc).timestamp())
    return now_sec * 1000


def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """Round a float half-up to specified decimal places per Rule R8."""
    if val is None:
        return None
    fmt = "0.01" if decimals == 2 else "0.0001"
    d = Decimal(str(val)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP)
    return float(d)


def compute_attendance_date(punch_in_ms: int, shift_start: str, shift_end: str) -> str:
    """
    Determine the attendance date (YYYY-MM-DD) in IST per Rule R1.
    Overnight shift (shift_end <= shift_start): if punch_in is earlier than
    shift_end, it belongs to the shift that started the previous calendar day.
    """
    dt_ist = datetime.fromtimestamp(punch_in_ms / 1000, tz=timezone.utc).astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        shift_end_t = dt_time(end_h, end_m)
        if dt_ist.time() < shift_end_t:
            return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_ms: int, attendance_date: str, shift_start: str) -> int:
    """
    Compute late minutes per Rule R2.
    Grace period is 10 minutes. A punch-in is late only if strictly more than
    10:00 (600 seconds) after shift_start. When late, whole minutes (floor) since shift_start.
    """
    punch_in_ist = datetime.fromtimestamp(punch_in_ms / 1000, tz=timezone.utc).astimezone(IST)
    date_obj = dt_date.fromisoformat(attendance_date)
    start_h, start_m = map(int, shift_start.split(":"))
    shift_start_dt = datetime.combine(date_obj, dt_time(start_h, start_m), tzinfo=IST)

    diff_sec = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(math.floor(diff_sec / 60))
    return 0


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """
    Compute work hours per Rule R4.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_seconds = (punch_out_ms - punch_in_ms) / 1000.0
    hours_dec = Decimal(str(diff_seconds)) / Decimal("3600")
    rounded = hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime(punch_out_ms: int, attendance_date: str, shift_start: str, shift_end: str) -> int:
    """
    Compute overtime minutes per Rule R3.
    Whole minutes (floor) from shift_end to punch_out, counted only when >= 30; otherwise 0.
    """
    punch_out_ist = datetime.fromtimestamp(punch_out_ms / 1000, tz=timezone.utc).astimezone(IST)
    date_obj = dt_date.fromisoformat(attendance_date)
    end_h, end_m = map(int, shift_end.split(":"))
    is_overnight = shift_end <= shift_start
    if is_overnight:
        shift_end_dt = datetime.combine(date_obj + timedelta(days=1), dt_time(end_h, end_m), tzinfo=IST)
    else:
        shift_end_dt = datetime.combine(date_obj, dt_time(end_h, end_m), tzinfo=IST)

    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    if diff_sec >= 1800:
        return int(math.floor(diff_sec / 60))
    return 0


def compute_working_days(month_str: str, joined_on_str: str) -> int:
    """
    Compute Monday-Friday working days in the given month starting from joined_on per Rule R7.
    """
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    month_start = f"{month_str}-01"
    month_end = f"{month_str}-{num_days:02d}"

    if joined_on_str > month_end:
        return 0

    effective_start = max(month_start, joined_on_str)
    cur = dt_date.fromisoformat(effective_start)
    end = dt_date.fromisoformat(month_end)
    count = 0
    while cur <= end:
        if cur.weekday() < 5:  # Mon-Fri
            count += 1
        cur += timedelta(days=1)
    return count


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB attendance document to OpenAPI AttendanceRecord response schema."""
    punch_in_val = to_epoch_ms(doc.get("punch_in")) if doc.get("punch_in") else None
    punch_out_val = to_epoch_ms(doc.get("punch_out")) if doc.get("punch_out") else None

    formatted_history = []
    for h in doc.get("history", []):
        entry = {
            "at": to_epoch_ms(h.get("at")) if isinstance(h.get("at"), datetime) else h.get("at"),
            "by": h.get("by", ""),
            "reason": h.get("reason", ""),
            "changes": {},
        }
        for field, change in h.get("changes", {}).items():
            f_val = change.get("from")
            t_val = change.get("to")
            if field in ("punch_in", "punch_out"):
                f_val = to_epoch_ms(f_val) if isinstance(f_val, datetime) else f_val
                t_val = to_epoch_ms(t_val) if isinstance(t_val, datetime) else t_val
            entry["changes"][field] = {
                "from": f_val,
                "to": t_val,
            }
        formatted_history.append(entry)

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": punch_in_val,
        "punch_out": punch_out_val,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": formatted_history,
    }


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB employee document to OpenAPI Employee response schema."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# Pydantic Request Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$", description="EMP followed by 4-6 digits")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            dt_date.fromisoformat(v)
        except ValueError:
            raise ValueError("Invalid date format, must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def validate_shifts(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health", status_code=200)
def health():
    """Readiness probe: 200 only when MongoDB answers a ping; else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Database unreachable: {str(e)}",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create a new employee with unique emp_code."""
    now_ms = current_epoch_ms()
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": from_epoch_ms(now_ms),
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )
    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees paginated, sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Record a punch-in for the day."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_in_ms = body.punched_at if body.punched_at is not None else current_epoch_ms()
    punch_in_ms = (punch_in_ms // 1000) * 1000

    attendance_date = compute_attendance_date(punch_in_ms, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_ms, attendance_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": from_epoch_ms(punch_in_ms),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    """Record a punch-out for the most recent open record."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_out_ms = body.punched_at if body.punched_at is not None else current_epoch_ms()
    punch_out_ms = (punch_out_ms // 1000) * 1000
    punch_out_dt = from_epoch_ms(punch_out_ms)

    # Find the most recent record whose punch_in is <= punch_out
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(
            status_code=404,
            detail="No punch-in record found for punch-out",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record is already punched out",
        )

    punch_in_ms = to_epoch_ms(record["punch_in"])
    if punch_out_ms <= punch_in_ms:
        raise HTTPException(
            status_code=422,
            detail="punch_out must be strictly after punch_in",
        )
    if (punch_out_ms - punch_in_ms) > 24 * 3600 * 1000:
        raise HTTPException(
            status_code=422,
            detail="punch_out cannot be more than 24 hours after punch_in",
        )

    # Compute derived fields
    work_hrs = compute_work_hours(punch_in_ms, punch_out_ms)
    ot_min = compute_overtime(punch_out_ms, record["date"], emp["shift_start"], emp["shift_end"])
    is_half_day = work_hrs < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": is_half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Attendance record is already punched out",
        )

    return format_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records filtered and paginated, sorted by date desc, emp_code asc."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must not be greater than date_to",
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """Regularize (manually correct) one day's record with audit history."""
    try:
        dt_date.fromisoformat(date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Invalid date format",
        )

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    target_status = body.status if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE",
            )
        new_punch_in_ms = None
        new_punch_out_ms = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in_ms = (body.punch_in // 1000) * 1000
        elif record.get("punch_in") is not None:
            new_punch_in_ms = to_epoch_ms(record["punch_in"])
        else:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires a punch_in",
            )

        # Validate punch_in stays on the record's attendance date
        computed_date = compute_attendance_date(new_punch_in_ms, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in instant does not belong to attendance date {date}",
            )

        if body.punch_out is not None:
            new_punch_out_ms = (body.punch_out // 1000) * 1000
        elif record.get("punch_out") is not None:
            new_punch_out_ms = to_epoch_ms(record["punch_out"])
        else:
            new_punch_out_ms = None

        new_late_minutes = compute_late_minutes(new_punch_in_ms, date, emp["shift_start"])

        if new_punch_out_ms is not None:
            if new_punch_out_ms <= new_punch_in_ms:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be strictly after punch_in",
                )
            if (new_punch_out_ms - new_punch_in_ms) > 24 * 3600 * 1000:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )
            new_work_hours = compute_work_hours(new_punch_in_ms, new_punch_out_ms)
            new_overtime_minutes = compute_overtime(new_punch_out_ms, date, emp["shift_start"], emp["shift_end"])
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Check for actual changes
    old_punch_in_ms = to_epoch_ms(record.get("punch_in")) if record.get("punch_in") else None
    old_punch_out_ms = to_epoch_ms(record.get("punch_out")) if record.get("punch_out") else None
    old_status = record.get("status")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    changes_db: Dict[str, Any] = {}

    if old_status != target_status:
        changes_db["status"] = {"from": old_status, "to": target_status}
    if old_punch_in_ms != new_punch_in_ms:
        changes_db["punch_in"] = {
            "from": record.get("punch_in"),
            "to": from_epoch_ms(new_punch_in_ms) if new_punch_in_ms is not None else None,
        }
    if old_punch_out_ms != new_punch_out_ms:
        changes_db["punch_out"] = {
            "from": record.get("punch_out"),
            "to": from_epoch_ms(new_punch_out_ms) if new_punch_out_ms is not None else None,
        }
    if old_work_hours != new_work_hours:
        changes_db["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if old_late_minutes != new_late_minutes:
        changes_db["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if old_overtime_minutes != new_overtime_minutes:
        changes_db["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if old_half_day != new_half_day:
        changes_db["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes_db:
        raise HTTPException(
            status_code=422,
            detail="Request changes nothing",
        )

    now_ms = current_epoch_ms()
    history_entry = {
        "at": from_epoch_ms(now_ms),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    hist_len = len(record.get("history", []))
    update_doc = {
        "status": target_status,
        "punch_in": from_epoch_ms(new_punch_in_ms) if new_punch_in_ms is not None else None,
        "punch_out": from_epoch_ms(new_punch_out_ms) if new_punch_out_ms is not None else None,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            f"history.{hist_len}": {"$exists": False},
        },
        {
            "$set": update_doc,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification conflict on attendance history",
        )

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (Pipelines on MongoDB)
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    start_date = f"{month}-01"
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    end_date = f"{month}-{num_days:02d}"

    working_days = compute_working_days(month, emp["joined_on"])

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$project": {
                "is_weekday": {
                    "$in": [
                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [1, 2, 3, 4, 5],
                    ]
                },
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        agg = res[0]
        present_days = float(round_half_up(agg.get("present_days", 0.0), 2))
        leave_days = int(agg.get("leave_days", 0))
        late_count = int(agg.get("late_count", 0))
        total_late_minutes = int(agg.get("total_late_minutes", 0))
        total_overtime_minutes = int(agg.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100.0, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month cross-collection aggregation."""
    start_date = f"{month}-01"
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    end_date = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "logs": 1,
                "is_weekday": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {
                            "$in": [
                                {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                [1, 2, 3, 4, 5],
                            ]
                        },
                        False,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}]},
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        items.append({
            "department": doc["department"],
            "headcount": doc["headcount"],
            "present_days": float(round_half_up(doc["present_days"], 2)),
            "avg_work_hours": round_half_up(doc["avg_work_hours"], 2),
            "late_count": doc["late_count"],
            "total_late_minutes": doc["total_late_minutes"],
            "leave_count": doc["leave_count"],
            "on_duty_count": doc["on_duty_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard with competition ranking and tie handling."""
    start_date = f"{month}-01"
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    end_date = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend for a department with gap-filling and 7-day moving average."""
    try:
        from_d = dt_date.fromisoformat(from_date)
        to_d = dt_date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Invalid date format",
        )

    if to_d < from_d:
        raise HTTPException(
            status_code=422,
            detail="to date must be greater than or equal to from date",
        )

    days_diff = (to_d - from_d).days + 1
    if days_diff > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range must not exceed 92 days",
        )

    # Check department exists
    dept_check = db.employees.find_one({"department": department})
    if not dept_check:
        raise HTTPException(
            status_code=404,
            detail="Department not found",
        )

    from_dt_bound = datetime.combine(from_d, dt_time(0, 0), tzinfo=timezone.utc)
    to_dt_bound = datetime.combine(to_d + timedelta(days=1), dt_time(0, 0), tzinfo=timezone.utc)

    pipeline = [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "employees": 1,
                "date_dt": {"$literal": from_dt_bound},
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "partitionByFields": ["department"],
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt_bound, to_dt_bound],
                },
            }
        },
        {
            "$setWindowFields": {
                "partitionBy": "$department",
                "sortBy": {"date_dt": 1},
                "output": {
                    "employees": {"$first": "$employees"},
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "employees": 1,
                "department": 1,
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
                "emp_codes": {
                    "$map": {
                        "input": "$employees",
                        "as": "e",
                        "in": "$$e.emp_code",
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date", "codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$eq": ["$date", "$$d"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "day_logs",
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$in": [{"$isoDayOfWeek": "$date_dt"}, [1, 2, 3, 4, 5]]
                },
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$day_logs",
                                    "as": "l",
                                    "cond": {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                }
                            },
                            "as": "pl",
                            "in": {"$cond": [{"$eq": ["$$pl.half_day", True]}, 0.5, 1.0]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$ne": ["$attendance_rate", None]},
                        {"$round": ["$attendance_rate", 4]},
                        None,
                    ]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        items.append({
            "date": doc["date"],
            "is_working_day": doc["is_working_day"],
            "headcount": doc["headcount"],
            "present_count": float(round_half_up(doc["present_count"], 2)),
            "late_count": doc["late_count"],
            "attendance_rate": round_half_up(doc["attendance_rate"], 4) if doc["attendance_rate"] is not None else None,
            "moving_avg_7d": round_half_up(doc["moving_avg_7d"], 4) if doc["moving_avg_7d"] is not None else None,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Run MongoDB explain with executionStats for the named endpoint's main query."""
    if endpoint == "attendance_list":
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        skip = (page - 1) * page_size
        find_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        res = db.command({"explain": find_cmd, "verbosity": "executionStats"})
        collection_name = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required for employee_monthly explain",
            )
        start_date = f"{month}-01"
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        end_date = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_date, "$lte": end_date},
                }
            },
            {
                "$project": {
                    "is_weekday": {
                        "$in": [
                            {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                            [1, 2, 3, 4, 5],
                        ]
                    },
                    "status": 1,
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {"$and": ["$is_weekday", {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}]},
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        agg_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        res = db.command({"explain": agg_cmd, "verbosity": "executionStats"})
        collection_name = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for department_summary explain",
            )
        start_date = f"{month}-01"
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        end_date = f"{month}-{num_days:02d}"

        match_stage = {"joined_on": {"$lte": end_date}}
        if department is not None:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", start_date]},
                                        {"$lte": ["$date", end_date]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$unwind": {
                    "path": "$logs",
                    "preserveNullAndEmptyArrays": True,
                }
            },
            {
                "$project": {
                    "department": 1,
                    "emp_code": 1,
                    "logs": 1,
                    "is_weekday": {
                        "$cond": [
                            {"$ifNull": ["$logs.date", False]},
                            {
                                "$in": [
                                    {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                    [1, 2, 3, 4, 5],
                                ]
                            },
                            False,
                        ]
                    },
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "emp_codes": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {"$and": ["$is_weekday", {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}]},
                                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "avg_work_hours": {
                        "$avg": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$logs.work_hours", None]},
                                    ]
                                },
                                "$logs.work_hours",
                                "$$REMOVE",
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "ON_DUTY"]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": {"$size": "$emp_codes"},
                    "present_days": 1,
                    "avg_work_hours": 1,
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
            {"$sort": {"department": 1}},
        ]
        agg_cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        res = db.command({"explain": agg_cmd, "verbosity": "executionStats"})
        collection_name = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for late_leaderboard explain",
            )
        start_date = f"{month}-01"
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        end_date = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lte": end_date},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
        ]

        if department is not None:
            pipeline.append({"$match": {"emp.department": department}})

        pipeline.extend([
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}}
                    },
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1,
                }
            },
        ])

        agg_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        res = db.command({"explain": agg_cmd, "verbosity": "executionStats"})
        collection_name = "attendance_logs"

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to parameters are required for department_trend explain",
            )
        try:
            from_d = dt_date.fromisoformat(from_date)
            to_d = dt_date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="Invalid date format",
            )
        from_dt_bound = datetime.combine(from_d, dt_time(0, 0), tzinfo=timezone.utc)
        to_dt_bound = datetime.combine(to_d + timedelta(days=1), dt_time(0, 0), tzinfo=timezone.utc)

        pipeline = [
            {"$match": {"department": department}},
            {
                "$group": {
                    "_id": "$department",
                    "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
                }
            },
            {
                "$project": {
                    "department": "$_id",
                    "employees": 1,
                    "date_dt": {"$literal": from_dt_bound},
                }
            },
            {
                "$densify": {
                    "field": "date_dt",
                    "partitionByFields": ["department"],
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [from_dt_bound, to_dt_bound],
                    },
                }
            },
            {
                "$setWindowFields": {
                    "partitionBy": "$department",
                    "sortBy": {"date_dt": 1},
                    "output": {
                        "employees": {"$first": "$employees"},
                    },
                }
            },
            {
                "$project": {
                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                    "date_dt": 1,
                    "employees": 1,
                    "department": 1,
                }
            },
            {
                "$addFields": {
                    "headcount": {
                        "$size": {
                            "$filter": {
                                "input": "$employees",
                                "as": "e",
                                "cond": {"$lte": ["$$e.joined_on", "$date"]},
                            }
                        }
                    },
                    "emp_codes": {
                        "$map": {
                            "input": "$employees",
                            "as": "e",
                            "in": "$$e.emp_code",
                        }
                    },
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"d": "$date", "codes": "$emp_codes"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$in": ["$emp_code", "$$codes"]},
                                        {"$eq": ["$date", "$$d"]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "day_logs",
                }
            },
            {
                "$addFields": {
                    "is_working_day": {
                        "$in": [{"$isoDayOfWeek": "$date_dt"}, [1, 2, 3, 4, 5]]
                    },
                    "present_count": {
                        "$sum": {
                            "$map": {
                                "input": {
                                    "$filter": {
                                        "input": "$day_logs",
                                        "as": "l",
                                        "cond": {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    }
                                },
                                "as": "pl",
                                "in": {"$cond": [{"$eq": ["$$pl.half_day", True]}, 0.5, 1.0]},
                            }
                        }
                    },
                    "late_count": {
                        "$size": {
                            "$filter": {
                                "input": "$day_logs",
                                "as": "l",
                                "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                            }
                        }
                    },
                }
            },
            {
                "$addFields": {
                    "attendance_rate": {
                        "$cond": [
                            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                            {"$divide": ["$present_count", "$headcount"]},
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {
                                "documents": [-6, "current"],
                            },
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": {
                        "$cond": [
                            {"$ne": ["$attendance_rate", None]},
                            {"$round": ["$attendance_rate", 4]},
                            None,
                        ]
                    },
                    "moving_avg_7d": {
                        "$cond": [
                            {"$ne": ["$moving_avg_7d", None]},
                            {"$round": ["$moving_avg_7d", 4]},
                            None,
                        ]
                    },
                }
            },
            {"$sort": {"date": 1}},
        ]
        agg_cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        res = db.command({"explain": agg_cmd, "verbosity": "executionStats"})
        collection_name = "employees"

    # Sanitize BSON output for clean JSON response
    sanitized_explain = json.loads(json_util.dumps(res))
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": sanitized_explain,
    }
