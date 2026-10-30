"""Turn raw Loyaltri records into validated, normalized canonical employees.

The mapping (which source field feeds which Switrus field) is configuration
(LOYALTRI_FIELD_MAP). An invalid optional value is dropped with a warning so
the rest of the record still syncs; a missing/invalid emp_id rejects the record.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from email_validator import EmailNotValidError, validate_email

from services.loyaltri.loyaltri_client import get_path

_PHONE_RE = re.compile(r"^[0-9+()\-\s]{6,20}$")
_MAX_LEN = {"emp_id": 50, "emp_name": 100, "email": 255, "phone": 20,
            "department_id": 50, "branch_id": 50, "manager_id": 50, "source_id": 100,
            "employment_status": 50}


@dataclass
class Employee:
    emp_id: str
    emp_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    department_id: Optional[str] = None
    branch_id: Optional[str] = None
    manager_id: Optional[str] = None
    source_id: Optional[str] = None
    employment_status: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    def get(self, name: str) -> Optional[str]:
        return getattr(self, name)


def _clean(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = " ".join(str(value).split())
    return s or None


def map_record(raw: Any, field_map: Dict[str, str]) -> Tuple[Optional[Employee], Optional[str]]:
    """Return (employee, None) or (None, reason)."""
    if not isinstance(raw, dict):
        return None, "Record is not an object"

    values: Dict[str, Optional[str]] = {k: _clean(get_path(raw, src)) for k, src in field_map.items()}
    emp_id = values.get("emp_id")
    if not emp_id:
        return None, f"Missing employee identifier ({field_map.get('emp_id')})"
    if len(emp_id) > _MAX_LEN["emp_id"]:
        return None, "Employee identifier is too long"

    emp = Employee(emp_id=emp_id)
    for name, value in values.items():
        if name == "emp_id" or value is None:
            continue
        if len(value) > _MAX_LEN.get(name, 255):
            emp.warnings.append(f"{name} ignored: longer than {_MAX_LEN.get(name, 255)} characters")
            continue
        if name == "email":
            value = value.lower()
            try:
                validate_email(value, check_deliverability=False)
            except EmailNotValidError:
                emp.warnings.append("email ignored: not a valid address")
                continue
        if name == "phone" and not _PHONE_RE.match(value):
            emp.warnings.append("phone ignored: unexpected format")
            continue
        setattr(emp, name, value)
    return emp, None
