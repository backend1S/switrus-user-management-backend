import re
from datetime import date
from typing import Optional, Union

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class UserLogin(BaseModel):
    username: str
    password: str


# Note: an earlier, unused UserCreate definition (with BranchID) was shadowed by
# this one and has been removed. This is the contract POST /users has always used.
class UserCreate(BaseModel):
    # Lengths match the SW_USER nvarchar(50) columns, so an over-long value is a
    # 422 here instead of a truncation error from SQL Server.
    EmpID: str = Field(max_length=50)
    EmpName: str = Field(max_length=50)
    EmpPassword: str = Field(max_length=128)
    Email: str = Field(max_length=50)
    DepartmentID: str = Field(max_length=50)
    Roles: str = Field(max_length=50)
    BRANCH_ID: str | None = Field(default=None, max_length=50)
    Phone: str | None = Field(default=None, max_length=50)
    DeptEmail: str | None = Field(default=None, max_length=50)
    DeptPassword: str | None = Field(default=None, max_length=50)
    IsDisabled: int | None = 0


class SessionLogin(BaseModel):
    """POST /auth/login. Same credentials as /login: EmpName and password."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=128)


class SendOtpRequest(BaseModel):
    email:str

class ChangePasswordRequest(BaseModel):
    email: str
    new_password: str
    confirm_password: str


_PHONE_RE = re.compile(r"^[0-9+()\-\s]{6,20}$")


def _blank_to_none(v):
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def check_email(v: Optional[str]) -> Optional[str]:
    """Trim; blank -> None; otherwise must be a syntactically valid address."""
    v = _blank_to_none(v)
    if v is None:
        return None
    try:
        validate_email(v, check_deliverability=False)
    except EmailNotValidError as e:
        raise ValueError(f"Invalid email address: {e}")
    return v


def check_phone(v: Optional[str]) -> Optional[str]:
    v = _blank_to_none(v)
    if v is None:
        return None
    if not _PHONE_RE.match(v):
        raise ValueError("Phone must be 6-20 characters: digits, spaces, + ( ) -")
    return v


class UserUpdate(BaseModel):
    """PATCH /users/{emp_id}. Omitted fields are untouched; explicit null clears
    (only allowed for optional columns, enforced in the handler)."""

    model_config = ConfigDict(extra="forbid")

    EmpName: Optional[str] = Field(default=None, max_length=50)
    Email: Optional[str] = Field(default=None, max_length=50)
    DepartmentID: Optional[Union[str, int]] = None
    Roles: Optional[str] = Field(default=None, max_length=50)
    BRANCH_ID: Optional[str] = Field(default=None, max_length=50)
    Phone: Optional[str] = None
    MANAGER_ID: Optional[str] = Field(default=None, max_length=50)
    DeptEmail: Optional[str] = Field(default=None, max_length=50)

    @field_validator("EmpName", "Roles", "BRANCH_ID", "MANAGER_ID")
    @classmethod
    def _trim(cls, v):
        return _blank_to_none(v)

    @field_validator("DepartmentID")
    @classmethod
    def _dept(cls, v):
        # Stored as nvarchar(50); numbers are accepted for older callers.
        if isinstance(v, int):
            v = str(v)
        v = _blank_to_none(v)
        if v is not None and len(v) > 50:
            raise ValueError("At most 50 characters")
        return v

    @field_validator("Email", "DeptEmail")
    @classmethod
    def _email(cls, v):
        return check_email(v)

    @field_validator("Phone")
    @classmethod
    def _phone(cls, v):
        return check_phone(v)


class StatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_disabled: StrictBool


class LeaveUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date
    end_date: date
