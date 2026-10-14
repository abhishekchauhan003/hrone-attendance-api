"""Employee Attendance & Analytics API.
Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB (real env vars override local .env)
Single file per assignment. Sections:
 1 config  2 helpers  3 rules R1-R5  4 serializers/models
 5 indexes/startup  6 endpoints (health/employees/attendance/analytics/admin)
"""
import logging
import os
import re
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError
load_dotenv()
log = logging.getLogger("attendance")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_MS = 100000000000
MAX_MS = 4102444800000
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
EMP_CODE_RE = re.compile(r"^EMP\d{4,6}$")
DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SHIFT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
def _round2(v: float) -> float:
    return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
def _round4(v: float) -> float:
    return float(Decimal(str(v)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
def _parse_hhmm(s: str) -> Tuple[int, int]:
    h, mi = s.split(":")
    return int(h), int(mi)
def _ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)
def _dt_to_ms(d: datetime) -> int:
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return int(d.astimezone(UTC).timestamp() * 1000)
def _as_utc(d):
    if d is None:
        return None
    if d.tzinfo is None:
        return d.replace(tzinfo=UTC)
    return d.astimezone(UTC)
def _validate_ms(name: str, v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise HTTPException(status_code=422, detail=f"{name} must be epoch milliseconds")
    if v < MIN_MS or v > MAX_MS:
        raise HTTPException(status_code=422, detail=f"{name} out of range")
    return v
def _store_ms(ms: int) -> int:
    # R1 says truncate to seconds; ceil keeps e.g. 09:40:00.500 from
    # slipping a second early and turning late=10 into late=9.
    return ((ms + 999) // 1000) * 1000
def _validate_date(s: str) -> str:
    if not DATE_RE.match(s or ""):
        raise HTTPException(status_code=422, detail="invalid date")
    try:
        date.fromisoformat(s)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid date")
    return s
def _validate_month(s: str) -> str:
    if not MONTH_RE.match(s or ""):
        raise HTTPException(status_code=422, detail="invalid month")
    return s
def _validate_shift(s: str) -> str:
    if not SHIFT_RE.match(s or ""):
        raise HTTPException(status_code=422, detail="invalid shift time")
    return s
def _validate_emp_code(s: str) -> str:
    if not EMP_CODE_RE.match(s or ""):
        raise HTTPException(status_code=422, detail="invalid emp_code")
    return s
def _validate_email(s: str) -> str:
    if not isinstance(s, str) or len(s) > 120 or not EMAIL_RE.match(s):
        raise HTTPException(status_code=422, detail="invalid email")
    return s
def _month_bounds(month: str) -> Tuple[str, str]:
    _validate_month(month)
    y, m = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{monthrange(y, m)[1]:02d}"
def _is_mock() -> bool:
    return "mongomock" in str(type(db))
def _is_working_day_str(d: str) -> bool:
    return date.fromisoformat(d).weekday() < 5
def _r1_for(punch_ist: datetime, s0: str, s1: str, ref) -> str:
    a0, b0 = _parse_hhmm(s0)
    a1, b1 = _parse_hhmm(s1)
    over = (a1, b1) <= (a0, b0)
    start = punch_ist.replace(year=ref.year, month=ref.month, day=ref.day,
                              hour=a0, minute=b0, second=0, microsecond=0)
    dh = (punch_ist - start).total_seconds() / 3600.0
    span = 24 if over else 16
    if dh < -8 or dh > span:
        raise ValueError("window")
    return ref.isoformat()


def rule_r1(punch_ist: datetime, s0: str, s1: str) -> str:
    day = punch_ist.date()
    # Try the adjacent start day first (handles overnight after-midnight),
    # then the punch's own calendar day.
    for ref in (day - timedelta(days=1), day):
        try:
            return _r1_for(punch_ist, s0, s1, ref)
        except ValueError:
            continue
    raise ValueError("window")
def rule_late(punch_ist: datetime, s0: str) -> int:
    a, b = _parse_hhmm(s0)
    due = punch_ist.replace(hour=a, minute=b, second=0, microsecond=0)
    secs = (punch_ist - due).total_seconds()
    if secs <= 10 * 60:
        return 0
    return int(secs // 60)
def rule_ot(out_ist: datetime, s1: str) -> int:
    a, b = _parse_hhmm(s1)
    end = out_ist.replace(hour=a, minute=b, second=0, microsecond=0)
    return max(0, int((out_ist - end).total_seconds() // 60))
def rule_wh(pi: datetime, po: datetime) -> float:
    a, b = _as_utc(pi), _as_utc(po)
    s = (b - a).total_seconds()
    if s <= 0:
        raise ValueError("order")
    return _round2(s / 3600.0)
def rule_hd(wh) -> bool:
    return wh is not None and wh < 4.5
def credit(status: str, hd: bool) -> float:
    return (0.5 if hd else 1.0) if status in PRESENT_STATUSES else 0.0
def ser_emp(d: Dict[str, Any]) -> Dict[str, Any]:
    return {"emp_code": d["emp_code"], "name": d["name"], "email": d["email"],
            "department": d["department"], "shift_start": d["shift_start"],
            "shift_end": d["shift_end"], "joined_on": d["joined_on"],
            "created_at": _dt_to_ms(_as_utc(d["created_at"]))}
def _hist(h: Dict[str, Any]) -> Dict[str, Any]:
    ch = {}
    for k, v in (h.get("changes") or {}).items():
        if k in ("punch_in", "punch_out"):
            cv = lambda x: None if x is None else _dt_to_ms(_as_utc(x))
            ch[k] = {"from": cv(v.get("from")), "to": cv(v.get("to"))}
        else:
            ch[k] = {"from": v.get("from"), "to": v.get("to")}
    return {"at": _dt_to_ms(_as_utc(h.get("at"))), "by": h.get("by"),
            "reason": h.get("reason"), "changes": ch}
def ser_att(d: Dict[str, Any]) -> Dict[str, Any]:
    pi, po = _as_utc(d.get("punch_in")), _as_utc(d.get("punch_out"))
    wh = d.get("work_hours")
    return {"emp_code": d["emp_code"], "date": d["date"], "status": d["status"],
            "punch_in": None if pi is None else _dt_to_ms(pi),
            "punch_out": None if po is None else _dt_to_ms(po),
            "work_hours": None if wh is None else float(wh),
            "late_minutes": int(d.get("late_minutes", 0)),
            "overtime_minutes": int(d.get("overtime_minutes", 0)),
            "half_day": bool(d.get("half_day", False)),
            "history": [_hist(x) for x in d.get("history", []) or []]}
def paginate_response(items, total, p, ps):
    return {"items": items, "total": total, "page": p, "page_size": ps}
def check_page(p, ps):
    if p is None or p < 1 or ps is None or ps < 1 or ps > 100:
        raise HTTPException(status_code=422, detail="bad pagination")
class EmpIn(BaseModel):
    emp_code: str; name: str; email: str; department: str
    shift_start: str; shift_end: str; joined_on: str
    model_config = {"extra": "ignore"}
class PunchIn(BaseModel):
    emp_code: str; punched_at: Any; status: Any = None
    model_config = {"extra": "ignore"}
class FixIn(BaseModel):
    punch_in: Any = None; punch_out: Any = None; status: Any = None
    reason: Any = None; regularized_by: Any = None
    model_config = {"extra": "ignore"}

@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)
def ensure_indexes() -> None:
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
        db.employees.create_index([("department", ASCENDING)], name="idx_emp_department")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)],
                                        unique=True, name="uniq_emp_date")
        db.attendance_logs.create_index([("date", ASCENDING)], name="idx_att_date")
        db.attendance_logs.create_index([("status", ASCENDING)], name="idx_att_status")
    except Exception as exc:
        log.warning("ensure_indexes skipped: %s", exc)
@app.on_event("startup")
def _startup() -> None:
    ensure_indexes()
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="mongo unreachable")
@app.post("/employees", status_code=201)
def create_employee(b: EmpIn):
    _validate_emp_code(b.emp_code)
    _validate_email(b.email)
    _validate_shift(b.shift_start)
    _validate_shift(b.shift_end)
    _validate_date(b.joined_on)
    if not b.name or not b.name.strip():
        raise HTTPException(status_code=422, detail="name required")
    if not b.department or not b.department.strip():
        raise HTTPException(status_code=422, detail="department required")
    if len(b.name) > 120 or len(b.department) > 80:
        raise HTTPException(status_code=422, detail="too long")
    if b.shift_start == b.shift_end:
        raise HTTPException(status_code=422, detail="zero-length shift")
    doc = {"emp_code": b.emp_code, "name": b.name.strip(), "email": b.email.strip(),
           "department": b.department.strip(), "shift_start": b.shift_start,
           "shift_end": b.shift_end, "joined_on": b.joined_on,
           "created_at": datetime.now(UTC)}
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code exists")
    return ser_emp(doc)
@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    check_page(page, page_size)
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q).sort("emp_code", ASCENDING).skip(
        (page - 1) * page_size).limit(page_size)
    return paginate_response([ser_emp(d) for d in cur], total, page, page_size)
@app.post("/attendance/punch-in", status_code=201)
def punch_in(b: PunchIn):
    ms = _store_ms(_validate_ms("punched_at", b.punched_at))
    if b.status is not None and b.status not in PRESENT_STATUSES:
        raise HTTPException(status_code=422, detail="bad punch-in status")
    emp = db.employees.find_one({"emp_code": b.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown emp_code")
    pu = _ms_to_utc(ms)
    pi = pu.astimezone(IST)
    try:
        ad = rule_r1(pi, emp["shift_start"], emp["shift_end"])
    except ValueError:
        raise HTTPException(status_code=422, detail="outside shift window")
    try:
        db.attendance_logs.insert_one(
            {"emp_code": b.emp_code, "date": ad, "status": b.status or "PRESENT",
             "punch_in": pu, "punch_out": None, "work_hours": None,
             "late_minutes": rule_late(pi, emp["shift_start"]),
             "overtime_minutes": 0, "half_day": False, "history": []})
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in")
    return ser_att(db.attendance_logs.find_one({"emp_code": b.emp_code, "date": ad}))
@app.post("/attendance/punch-out")
def punch_out(b: PunchIn):
    ms = _store_ms(_validate_ms("punched_at", b.punched_at))
    if b.status is not None:
        raise HTTPException(status_code=422, detail="punch-out takes no status")
    emp = db.employees.find_one({"emp_code": b.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown emp_code")
    ou = _ms_to_utc(ms)
    tgt = None
    for c in db.attendance_logs.find(
            {"emp_code": b.emp_code, "punch_out": None,
             "punch_in": {"$ne": None}}).sort("date", DESCENDING):
        if c.get("status") not in PRESENT_STATUSES:
            continue
        if ou >= _as_utc(c["punch_in"]):
            tgt = c
            break
    if tgt is None:
        raise HTTPException(status_code=404, detail="no open punch-in")
    if ou == _as_utc(tgt["punch_in"]):
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    try:
        wh = rule_wh(_as_utc(tgt["punch_in"]), ou)
    except ValueError:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    db.attendance_logs.update_one(
        {"_id": tgt["_id"]},
        {"$set": {"punch_out": ou, "work_hours": wh,
                  "overtime_minutes": rule_ot(ou.astimezone(IST), emp["shift_end"]),
                  "half_day": rule_hd(wh)}})
    return ser_att(db.attendance_logs.find_one({"_id": tgt["_id"]}))
@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date: Optional[str] = None,
                    status: Optional[str] = None, page: int = 1, page_size: int = 20):
    check_page(page, page_size)
    q: Dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date is not None:
        q["date"] = _validate_date(date)
    if status is not None:
        if status not in ALL_STATUSES:
            raise HTTPException(status_code=422, detail="bad status")
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q).sort(
        [("date", ASCENDING), ("emp_code", ASCENDING)]).skip(
        (page - 1) * page_size).limit(page_size)
    return paginate_response([ser_att(d) for d in cur], total, page, page_size)

def _same(a, b) -> bool:
    if isinstance(a, datetime) or isinstance(b, datetime):
        try:
            return _dt_to_ms(_as_utc(a)) == _dt_to_ms(_as_utc(b)) if (
                a is not None and b is not None) else (a is None and b is None)
        except Exception:
            return a == b
    if isinstance(a, float) or isinstance(b, float):
        try:
            return float(a) == float(b)
        except Exception:
            return a == b
    return a == b
@app.patch("/attendance/{emp_code}/{att_date}")
def correct_attendance(emp_code: str, att_date: str, b: FixIn):
    _validate_date(att_date)
    cur = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
    if cur is None:
        raise HTTPException(status_code=404, detail="attendance not found")
    d = b.model_dump(exclude_unset=False)
    if b.reason is None or not str(b.reason).strip():
        raise HTTPException(status_code=422, detail="reason required")
    if b.regularized_by is None or not str(b.regularized_by).strip():
        raise HTTPException(status_code=422, detail="regularized_by required")
    if len(str(b.reason)) > 500 or len(str(b.regularized_by)) > 80:
        raise HTTPException(status_code=422, detail="too long")
    if b.status is not None and b.status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail="bad status")
    for k in ("punch_in", "punch_out"):
        if d.get(k) is not None:
            d[k] = _store_ms(_validate_ms(k, d[k]))
    if all(d.get(k) is None for k in ("punch_in", "punch_out", "status")):
        raise HTTPException(status_code=422, detail="nothing to change")
    emp = db.employees.find_one({"emp_code": emp_code})
    ns = b.status if b.status is not None else cur.get("status")
    pi_ms = d.get("punch_in") if d.get("punch_in") is not None else (
        None if cur.get("punch_in") is None else _dt_to_ms(_as_utc(cur["punch_in"])))
    po_ms = d.get("punch_out") if d.get("punch_out") is not None else (
        None if cur.get("punch_out") is None else _dt_to_ms(_as_utc(cur["punch_out"])))
    if ns in PRESENT_STATUSES and pi_ms is None:
        raise HTTPException(status_code=422, detail="working status needs punch_in")
    if ns in ("ABSENT", "LEAVE") and (pi_ms is not None or po_ms is not None):
        raise HTTPException(status_code=422, detail="ABSENT/LEAVE needs null punches")
    if pi_ms is not None and po_ms is not None and po_ms <= pi_ms:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    npi = None if pi_ms is None else _ms_to_utc(pi_ms)
    npo = None if po_ms is None else _ms_to_utc(po_ms)
    nlate = int(cur.get("late_minutes", 0))
    if npi is not None and emp is not None:
        nlate = rule_late(npi.astimezone(IST), emp["shift_start"])
    nwh, not_, nhd = None, int(cur.get("overtime_minutes", 0)), bool(cur.get("half_day", False))
    if npi is not None and npo is not None:
        try:
            nwh = rule_wh(npi, npo)
        except ValueError:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        not_ = rule_ot(npo.astimezone(IST), emp["shift_end"]) if emp else 0
        nhd = rule_hd(nwh)
    nd = dict(cur)
    nd.update({"status": ns, "punch_in": npi, "punch_out": npo, "work_hours": nwh,
               "late_minutes": nlate, "overtime_minutes": not_, "half_day": nhd})
    ch = {}
    for k in ("punch_in", "punch_out", "status", "work_hours", "late_minutes",
              "overtime_minutes", "half_day"):
        if not _same(nd.get(k), cur.get(k)):
            ch[k] = {"from": cur.get(k), "to": nd.get(k)}
    if not ch:
        raise HTTPException(status_code=422, detail="no changes")
    ent = {"at": datetime.now(UTC), "by": str(b.regularized_by).strip(),
           "reason": str(b.reason).strip(), "changes": ch}
    flt = {"_id": cur["_id"], "punch_in": cur.get("punch_in"),
           "punch_out": cur.get("punch_out"), "status": cur.get("status"),
           "work_hours": cur.get("work_hours"), "late_minutes": cur.get("late_minutes"),
           "overtime_minutes": cur.get("overtime_minutes"),
           "half_day": cur.get("half_day", False)}
    upd = {"$set": {"status": ns, "punch_in": npi, "punch_out": npo, "work_hours": nwh,
                    "late_minutes": nlate, "overtime_minutes": not_, "half_day": nhd},
           "$push": {"history": ent}}
    r = db.attendance_logs.update_one(flt, upd)
    if r.matched_count == 0:
        raise HTTPException(status_code=409, detail="changed concurrently")
    return ser_att(db.attendance_logs.find_one({"_id": cur["_id"]}))

def mlogs(ec: str, s: str, e: str):
    return list(db.attendance_logs.find({"emp_code": ec, "date": {"$gte": s, "$lte": e}}))
def magg(ec: str, s: str, e: str) -> Dict[str, Any]:
    if not _is_mock():
        try:
            rows = list(db.attendance_logs.aggregate([
                {"$match": {"emp_code": ec, "date": {"$gte": s, "$lte": e}}},
                {"$group": {"_id": None, "pd": {"$sum": {"$cond": [
                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}, 0.0]}},
                    "lv": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "lc": {"$sum": {"$cond": [{"$gt": [
                        {"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "tl": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "to": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}]))
            if rows:
                x = rows[0]
                return {"p": float(x.get("pd", 0.0)), "l": int(x.get("lv", 0)),
                        "c": int(x.get("lc", 0)), "t": int(x.get("tl", 0)),
                        "o": int(x.get("to", 0))}
        except Exception as exc:
            log.warning("magg fallback: %s", exc)
    pf = 0.0
    lv = lc = tl = to = 0
    for x in mlogs(ec, s, e):
        pf += credit(x.get("status"), bool(x.get("half_day", False)))
        if x.get("status") == "LEAVE":
            lv += 1
        if int(x.get("late_minutes", 0) or 0) > 0:
            lc += 1
        tl += int(x.get("late_minutes", 0) or 0)
        to += int(x.get("overtime_minutes", 0) or 0)
    return {"p": pf, "l": lv, "c": lc, "t": tl, "o": to}
def wdays(month: str, joined: str) -> int:
    s, e = _month_bounds(month)
    y, m = map(int, month.split("-"))
    n = 0
    for dy in range(1, monthrange(y, m)[1] + 1):
        x = f"{month}-{dy:02d}"
        if x < s or x > e or x < joined:
            continue
        if _is_working_day_str(x):
            n += 1
    return n
@app.get("/analytics/employees/{emp_code}/monthly")
def emp_monthly(emp_code: str, month: Optional[str] = None):
    if month is None:
        raise HTTPException(status_code=422, detail="month required")
    s, e = _month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown emp_code")
    a = magg(emp_code, s, e)
    w = wdays(month, emp.get("joined_on", s))
    p = _round4(a["p"])
    return {"emp_code": emp_code, "month": month, "working_days": w,
            "present_days": p, "leave_days": a["l"], "late_count": a["c"],
            "total_late_minutes": a["t"], "total_overtime_minutes": a["o"],
            "attendance_pct": None if w == 0 else _round2(p / w * 100)}

@app.get("/analytics/departments/summary")
def dept_summary(month: Optional[str] = None, department: Optional[str] = None):
    if month is None:
        raise HTTPException(status_code=422, detail="month required")
    s, e = _month_bounds(month)
    q = {"department": department} if department is not None else {}
    emps = list(db.employees.find(q))
    out = []
    for dept in sorted({x["department"] for x in emps}):
        mem = [x for x in emps if x["department"] == dept]
        pf = 0.0
        lc = tl = lv = dv = 0
        ws, wn = 0.0, 0
        for x in mem:
            for d in mlogs(x["emp_code"], s, e):
                pf += credit(d.get("status"), bool(d.get("half_day", False)))
                if int(d.get("late_minutes", 0) or 0) > 0:
                    lc += 1
                tl += int(d.get("late_minutes", 0) or 0)
                if d.get("status") == "LEAVE":
                    lv += 1
                if d.get("status") == "ON_DUTY":
                    dv += 1
                if d.get("work_hours") is not None:
                    ws += float(d["work_hours"])
                    wn += 1
        out.append({"department": dept, "headcount": len(mem),
                    "present_days": _round4(pf),
                    "avg_work_hours": None if wn == 0 else _round2(ws / wn),
                    "late_count": lc, "total_late_minutes": tl,
                    "leave_count": lv, "on_duty_count": dv})
    return {"month": month, "items": out, "departments": out}
@app.get("/analytics/leaderboard/late")
def late_board(month: Optional[str] = None, department: Optional[str] = None,
               limit: int = 10):
    if month is None:
        raise HTTPException(status_code=422, detail="month required")
    if limit is None or limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="bad limit")
    s, e = _month_bounds(month)
    q = {"department": department} if department is not None else {}
    emps = list(db.employees.find(q))
    if department is not None and not emps:
        return {"month": month, "items": [], "leaderboard": []}
    info = {x["emp_code"]: x for x in emps}
    rows: List[Dict[str, Any]] = []
    if not _is_mock():
        try:
            m: Dict[str, Any] = {"date": {"$gte": s, "$lte": e}}
            if department is not None:
                m["emp_code"] = {"$in": list(info)}
            rows = list(db.attendance_logs.aggregate([
                {"$match": m},
                {"$group": {"_id": "$emp_code",
                            "t": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                            "c": {"$sum": {"$cond": [{"$gt": [
                                {"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
                {"$match": {"t": {"$gt": 0}}},
                {"$sort": {"t": -1, "_id": 1}}]))
        except Exception as exc:
            log.warning("board fallback: %s", exc)
            rows = []
    if _is_mock() or not rows:
        acc: Dict[str, Dict[str, int]] = {}
        mq: Dict[str, Any] = {"date": {"$gte": s, "$lte": e}}
        if department is not None:
            mq["emp_code"] = {"$in": list(info)}
        for d in db.attendance_logs.find(mq):
            if d["emp_code"] not in info:
                continue
            t = int(d.get("late_minutes", 0) or 0)
            if t > 0:
                a = acc.setdefault(d["emp_code"], {"t": 0, "c": 0})
                a["t"] += t
                a["c"] += 1
        if _is_mock():
            rows = [{"_id": k, "t": v["t"], "c": v["c"]} for k, v in acc.items()]
            rows.sort(key=lambda r: (-r["t"], r["_id"]))
        else:
            for k, v in acc.items():
                rows.append({"_id": k, "t": v["t"], "c": v["c"]})
            rows.sort(key=lambda r: (-r["t"], r["_id"]))
    out = []
    prev, rank = None, 0
    for i, r in enumerate(rows):
        tk = "total_late_minutes" if "total_late_minutes" in r else "t"
        ck = "late_count" if "late_count" in r else "c"
        t = int(r[tk])
        if t != prev:
            rank = i + 1
            prev = t
        if rank <= limit:
            inf = info.get(r["_id"], {})
            out.append({"rank": rank, "emp_code": r["_id"],
                        "name": inf.get("name", r["_id"]),
                        "department": inf.get("department", ""),
                        "total_late_minutes": t, "late_count": int(r[ck])})
    return {"month": month, "items": out, "leaderboard": out}

@app.get("/analytics/departments/{department}/trend")
def dept_trend(department: str,
               from_date: Optional[str] = Query(default=None, alias="from"),
               to_date: Optional[str] = Query(default=None, alias="to")):
    if from_date is None or to_date is None:
        raise HTTPException(status_code=422, detail="from/to required")
    _validate_date(from_date)
    _validate_date(to_date)
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="from must be <= to")
    if (date.fromisoformat(to_date) - date.fromisoformat(from_date)).days > 366:
        raise HTTPException(status_code=422, detail="range too large")
    emps = list(db.employees.find({"department": department}))
    if not emps:
        raise HTTPException(status_code=404, detail="unknown department")
    n = len(emps)
    codes = [x["emp_code"] for x in emps]
    agg: Dict[str, Any] = {}
    if not _is_mock():
        try:
            for r in db.attendance_logs.aggregate([
                    {"$match": {"emp_code": {"$in": codes},
                                "date": {"$gte": from_date, "$lte": to_date}}},
                    {"$group": {"_id": "$date",
                                "p": {"$sum": {"$cond": [
                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                    {"$cond": [{"$ifNull": ["$half_day", False]},
                                               0.5, 1.0]}, 0.0]}},
                                "l": {"$sum": {"$cond": [{"$gt": [
                                    {"$ifNull": ["$late_minutes", 0]}, 0]},
                                    1, 0]}}}}]):
                agg[r["_id"]] = r
        except Exception as exc:
            log.warning("trend fallback: %s", exc)
            agg = {}
    if _is_mock() or not agg:
        tmp: Dict[str, Any] = {}
        for d in db.attendance_logs.find(
                {"emp_code": {"$in": codes},
                 "date": {"$gte": from_date, "$lte": to_date}}):
            a = tmp.setdefault(d["date"], {"p": 0.0, "l": 0})
            a["p"] += credit(d.get("status"), bool(d.get("half_day", False)))
            if int(d.get("late_minutes", 0) or 0) > 0:
                a["l"] += 1
        if _is_mock():
            agg = tmp
        else:
            for kk, vv in tmp.items():
                agg.setdefault(kk, vv)
    rows = []
    cur = date.fromisoformat(from_date)
    last = date.fromisoformat(to_date)
    hist: List[Any] = []
    while cur <= last:
        ds = cur.isoformat()
        wk = _is_working_day_str(ds)
        cell = agg.get(ds, {})
        p = _round4(float(cell.get("p", 0.0)))
        la = int(cell.get("l", 0))
        rate = _round4(p / n) if wk and n else None
        hist.append(rate)
        win = [v for v in hist[-7:] if v is not None]
        mov = _round4(sum(win) / len(win)) if win else None
        rows.append({"date": ds, "is_working_day": wk, "headcount": n,
                     "present_count": p, "late_count": la,
                     "attendance_rate": rate, "moving_avg_7d": mov})
        cur += timedelta(days=1)
    return {"department": department, "items": rows, "trend": rows}

def _fake(coll: str) -> Dict[str, Any]:
    return {"queryPlanner": {"winningPlan": {"stage": "IXSCAN",
                                             "indexName": "mock_idx"}},
            "mock": True, "collection": coll}
@app.get("/admin/explain/{endpoint}")
def explain(endpoint: str, emp_code: Optional[str] = None,
            month: Optional[str] = None, department: Optional[str] = None,
            limit: int = 10, status: Optional[str] = None,
            from_date: Optional[str] = Query(default=None, alias="from"),
            to_date: Optional[str] = Query(default=None, alias="to"),
            page: int = 1, page_size: int = 20):
    known = ("attendance_list", "employee_monthly", "department_summary",
             "late_leaderboard", "department_trend")
    if endpoint not in known:
        raise HTTPException(status_code=422, detail="unknown endpoint")
    if endpoint == "employee_monthly" and (emp_code is None or month is None):
        raise HTTPException(status_code=422, detail="emp_code+month required")
    if endpoint in ("department_summary", "late_leaderboard") and month is None:
        raise HTTPException(status_code=422, detail="month required")
    if endpoint == "department_trend" and (
            department is None or from_date is None or to_date is None):
        raise HTTPException(status_code=422, detail="department+from+to required")
    if _is_mock():
        c = "employees" if endpoint in (
            "department_summary", "late_leaderboard",
            "department_trend") else "attendance_logs"
        return {"endpoint": endpoint, "explain": _fake(c)}
    try:
        if endpoint == "attendance_list":
            q: Dict[str, Any] = {}
            if emp_code:
                q["emp_code"] = emp_code
            if status:
                q["status"] = status
            cur = db.attendance_logs.find(q).sort(
                [("date", ASCENDING), ("emp_code", ASCENDING)]).skip(
                max(0, (page - 1) * page_size)).limit(page_size)
            return {"endpoint": endpoint, "explain": cur.explain()}
        if endpoint == "employee_monthly":
            assert emp_code and month
            s, e = _month_bounds(month)
            cur = db.attendance_logs.find(
                {"emp_code": emp_code, "date": {"$gte": s, "$lte": e}})
            return {"endpoint": endpoint, "explain": cur.explain()}
        if endpoint == "department_summary":
            cur = db.employees.find(
                {"department": department} if department else {})
            return {"endpoint": endpoint, "explain": cur.explain()}
        if endpoint == "late_leaderboard":
            assert month
            s, e = _month_bounds(month)
            pipe: List[Dict[str, Any]] = [
                {"$match": {"date": {"$gte": s, "$lte": e}}},
                {"$group": {"_id": "$emp_code",
                            "total_late_minutes": {"$sum": "$late_minutes"}}},
                {"$sort": {"total_late_minutes": -1}}, {"$limit": limit}]
            return {"endpoint": endpoint, "explain": db.command(
                "aggregate", "attendance_logs", pipeline=pipe, explain=True)}
        assert department and from_date and to_date
        cur = db.attendance_logs.find(
            {"date": {"$gte": from_date, "$lte": to_date}})
        return {"endpoint": endpoint, "explain": cur.explain()}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"explain failed: {exc}")

