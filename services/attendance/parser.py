"""Turn the attendance service's muster into a small, allowlisted structure.

Documented response shape:

    {"success": true,
     "data": {"companyId": .., "fromDate": "YYYY-MM-DD", "toDate": "YYYY-MM-DD",
              "attendance": [{"employeeId": <service's numeric id>,
                              "employeeDetails": {"employeeCode": .., ...},
                              "attendanceDetails": {"<date>": <day>, ...},
                              "attendanceSummary": {...}}]}}

Only fields named here are copied out. Everything else in employeeDetails
(grossSalary, bank details, ...) is never read, so it cannot reach a response.

The layout of one <day> has not been confirmed against the real service, so
day fields are looked up under a few common names; the status label is kept
exactly as the service sends it and only *grouped* into a category for the
counts. Unknown labels stay visible and are counted as "other".
"""
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from services.attendance.client import AttendanceResponseError

CATEGORIES = ("present", "absent", "off_day", "half_day", "leave", "other")

_PRESENT = {"p", "present"}
_ABSENT = {"a", "ab", "absent"}
_OFF = {"off", "offday", "dayoff", "wo", "weeklyoff", "weekoff", "weekend", "rest", "restday",
        "h", "holiday", "ph", "publicholiday"}
_HALF = {"hd", "halfday", "half"}
_LEAVE_CODES = {"l", "cl", "sl", "el", "pl", "ml", "lop", "lwp", "co", "compoff", "od"}

STATUS_KEYS = ("status", "attendanceStatus", "attendance", "dayStatus", "statusName", "type")
IN_KEYS = ("inTime", "checkIn", "checkInTime", "punchIn", "firstIn", "in")
OUT_KEYS = ("outTime", "checkOut", "checkOutTime", "punchOut", "lastOut", "out")
HOURS_KEYS = ("workHours", "workingHours", "totalHours", "hoursWorked", "workedHours", "totalWorkHours")
OT_KEYS = ("overtime", "overTime", "otHours", "overtimeHours", "ot")
NAME_KEYS = ("employeeName", "fullName", "name")

# Words that mark a summary key as payroll or personal data. Matched as whole
# words of the key ("grossSalary" -> gross, salary), so "companyId" is fine.
SENSITIVE_WORDS = {
    "salary", "wage", "wages", "ctc", "amount", "deduction", "deductions", "bank", "account",
    "pan", "aadhaar", "aadhar", "ifsc", "tax", "pf", "esi", "esic", "uan", "bonus", "incentive",
    "allowance", "allowances", "earning", "earnings", "gross", "basic", "hra", "payable",
    "payroll", "payment", "pay", "net", "arrears", "reimbursement", "loan", "advance",
}


@dataclass
class Day:
    status: str
    category: str
    in_time: Optional[str] = None
    out_time: Optional[str] = None
    work_minutes: Optional[int] = None
    overtime_minutes: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "category": self.category, "in_time": self.in_time,
                "out_time": self.out_time, "work_minutes": self.work_minutes,
                "overtime_minutes": self.overtime_minutes}


@dataclass
class Record:
    code: Optional[str]
    name: Optional[str]
    days: Dict[str, Day] = field(default_factory=dict)
    source_summary: Dict[str, float] = field(default_factory=dict)


@dataclass
class Parsed:
    records: List[Record]
    skipped_records: int = 0
    unparsed_dates: int = 0
    out_of_range_dates: int = 0
    range_mismatch: bool = False


def _words(key: str) -> List[str]:
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", key)  # "PFAmount" -> "PF Amount"
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", spaced)
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w]


def is_sensitive_key(key: str) -> bool:
    return any(w in SENSITIVE_WORDS for w in _words(str(key)))


def categorize(label: str) -> str:
    k = re.sub(r"[^a-z]", "", label.lower())
    if k in _PRESENT:
        return "present"
    if k in _ABSENT:
        return "absent"
    if k in _HALF or "halfday" in k:
        return "half_day"
    if k in _OFF:
        return "off_day"
    if "leave" in k or k in _LEAVE_CODES:
        return "leave"
    return "other"


def parse_date_key(key: Any) -> Optional[date]:
    """YYYY-MM-DD (optionally with a time), DD-MM-YYYY or DD/MM/YYYY."""
    s = str(key).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$", s)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.match(r"^(\d{2})[-/](\d{2})[-/](\d{4})$", s)
        if m:
            return date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        return None
    return None


def parse_minutes(value: Any) -> Optional[int]:
    """'08:30' / '8:30:00' -> 510; 8.5 or '8.5' (hours) -> 510; anything else -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value) * 60) if 0 <= value <= 48 else None
    s = str(value).strip()
    m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", s)
    if m and int(m[2]) < 60:
        return int(m[1]) * 60 + int(m[2])
    try:
        hours = float(s)
    except ValueError:
        return None
    return round(hours * 60) if 0 <= hours <= 48 else None


def parse_time(value: Any) -> Optional[str]:
    """A clock time as HH:MM, from 'HH:MM', 'HH:MM:SS' or an ISO date-time."""
    if value is None:
        return None
    s = str(value).strip()
    m = re.search(r"(?:^|T| )(\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$", s)
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        return None
    return f"{int(m[1]):02d}:{m[2]}"


def _first(d: Dict[str, Any], keys) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def parse_day(value: Any) -> Optional[Day]:
    if isinstance(value, str):
        label = value.strip()
        return Day(label[:40], categorize(label)) if label else None
    if not isinstance(value, dict):
        return None
    raw = _first(value, STATUS_KEYS)
    if not isinstance(raw, str) or not raw.strip():
        return None
    label = raw.strip()[:40]
    return Day(
        status=label,
        category=categorize(label),
        in_time=parse_time(_first(value, IN_KEYS)),
        out_time=parse_time(_first(value, OUT_KEYS)),
        work_minutes=parse_minutes(_first(value, HOURS_KEYS)),
        overtime_minutes=parse_minutes(_first(value, OT_KEYS)),
    )


def _text(value: Any, limit: int = 100) -> Optional[str]:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    s = str(value).strip()
    return s[:limit] if s else None


def _name(details: Dict[str, Any]) -> Optional[str]:
    name = _text(_first(details, NAME_KEYS))
    if name:
        return name
    parts = [_text(details.get(k)) for k in ("firstName", "middleName", "lastName")]
    joined = " ".join(p for p in parts if p)
    return joined or None


def sanitize_summary(summary: Any) -> Dict[str, float]:
    """Numeric attendance counts only; payroll-type keys and nested values are dropped."""
    if not isinstance(summary, dict):
        return {}
    out: Dict[str, float] = {}
    for k, v in summary.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or is_sensitive_key(k):
            continue
        out[str(k)[:60]] = v
    return out


def _details_items(details: Any) -> List[Tuple[Any, Any]]:
    if isinstance(details, dict):
        return list(details.items())
    if isinstance(details, list):  # tolerate [{"date": .., ...}]
        return [(d.get("date"), d) for d in details if isinstance(d, dict)]
    return []


def parse_muster(body: Any, from_date: date, to_date: date) -> Parsed:
    if not isinstance(body, dict):
        raise AttendanceResponseError("The attendance service sent an unexpected response.")
    if body.get("success") is not True:
        raise AttendanceResponseError("The attendance service reported that it could not produce the muster.")
    data = body.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("attendance"), list):
        raise AttendanceResponseError("The attendance service sent a muster in an unexpected format.")

    parsed = Parsed(records=[])
    got_from, got_to = parse_date_key(data.get("fromDate") or ""), parse_date_key(data.get("toDate") or "")
    parsed.range_mismatch = (got_from, got_to) != (from_date, to_date)

    for item in data["attendance"]:
        if not isinstance(item, dict):
            parsed.skipped_records += 1
            continue
        details = item.get("employeeDetails") if isinstance(item.get("employeeDetails"), dict) else {}
        code = _text(details.get("employeeCode") if details else None) or _text(item.get("employeeCode"))
        rec = Record(code=code, name=_name(details), source_summary=sanitize_summary(item.get("attendanceSummary")))
        for key, value in _details_items(item.get("attendanceDetails")):
            d = parse_date_key(key) if key is not None else None
            if d is None:
                parsed.unparsed_dates += 1
                continue
            if not (from_date <= d <= to_date):
                parsed.out_of_range_dates += 1
                continue
            day = parse_day(value)
            if day is None:
                parsed.unparsed_dates += 1
                continue
            rec.days[d.isoformat()] = day
        parsed.records.append(rec)
    return parsed
