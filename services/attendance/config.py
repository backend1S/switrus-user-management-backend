"""Attendance Muster settings, read from the environment.

The real endpoint and key are supplied through .env only. Access is off by
default: nothing is shown until the client is configured AND the access
settings below say who may see it. See docs/ATTENDANCE.md.
"""
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

CLIENT_MODES = ("disabled", "mock", "http")
MOCK_FAILURES = ("none", "outage", "timeout", "auth_error", "server_error", "malformed", "not_success")
# What administrators may see:
#   none    - nothing (default; company-wide attendance needs explicit authorization)
#   matched - employees whose employeeCode matches a SW_USER.EMPID, every branch
#   all     - matched employees plus records that match no Switrus user
ADMIN_SCOPES = ("none", "matched", "all")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "y", "on")


def _num(name: str, default, cast=int):
    try:
        return cast(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class AttendanceSettings:
    client_mode: str = "disabled"
    api_url: str = ""
    api_key: str = field(default="", repr=False)
    api_key_header: str = "x-api-key"
    timeout_seconds: float = 20.0
    max_retries: int = 1
    max_range_days: int = 31
    cache_seconds: int = 60

    # The upstream employeeCode is matched to SW_USER.EMPID. Until someone has
    # confirmed that on real data, scoped roles see nothing.
    mapping_verified: bool = False
    admin_scope: str = "none"

    mock_failure: str = "none"

    @classmethod
    def from_env(cls) -> "AttendanceSettings":
        return cls(
            client_mode=_env("ATTENDANCE_CLIENT", "disabled").lower(),
            api_url=_env("ATTENDANCE_API_URL"),
            api_key=_env("ATTENDANCE_API_KEY"),
            api_key_header=_env("ATTENDANCE_API_KEY_HEADER", "x-api-key"),
            timeout_seconds=max(1.0, _num("ATTENDANCE_TIMEOUT_SECONDS", 20.0, float)),
            max_retries=min(3, max(0, _num("ATTENDANCE_MAX_RETRIES", 1))),
            max_range_days=min(92, max(1, _num("ATTENDANCE_MAX_RANGE_DAYS", 31))),
            cache_seconds=max(0, _num("ATTENDANCE_CACHE_SECONDS", 60)),
            mapping_verified=_bool("ATTENDANCE_MAPPING_VERIFIED", False),
            admin_scope=_env("ATTENDANCE_ADMIN_SCOPE", "none").lower(),
            mock_failure=_env("ATTENDANCE_MOCK_FAILURE", "none").lower(),
        )

    def problems(self) -> List[str]:
        """What prevents the client from being used. Empty list = ready."""
        p: List[str] = []
        if self.client_mode not in CLIENT_MODES:
            return [f"ATTENDANCE_CLIENT must be one of {', '.join(CLIENT_MODES)}"]
        if self.client_mode == "disabled":
            return ["Attendance integration is disabled (ATTENDANCE_CLIENT=disabled)"]
        if self.admin_scope not in ADMIN_SCOPES:
            p.append(f"ATTENDANCE_ADMIN_SCOPE must be one of {', '.join(ADMIN_SCOPES)}")
        if self.client_mode == "http":
            if not self.api_url:
                p.append("ATTENDANCE_API_URL is not set")
            elif not (self.api_url.startswith("https://") or self.api_url.startswith("http://localhost")):
                p.append("ATTENDANCE_API_URL must use https://")
            elif "?" in self.api_url:
                p.append("ATTENDANCE_API_URL must not contain a query string (the key goes in a header)")
            if not self.api_key:
                p.append("ATTENDANCE_API_KEY is not set")
            if not self.api_key_header:
                p.append("ATTENDANCE_API_KEY_HEADER is empty")
        if self.client_mode == "mock" and self.mock_failure not in MOCK_FAILURES:
            p.append(f"ATTENDANCE_MOCK_FAILURE must be one of {', '.join(MOCK_FAILURES)}")
        return p

    def public_summary(self) -> Dict:
        """Safe for administrators: never the key or the URL."""
        return {
            "client_mode": self.client_mode,
            "credentials_set": bool(self.api_key),
            "mapping_verified": self.mapping_verified,
            "admin_scope": self.admin_scope,
            "max_range_days": self.max_range_days,
        }


_override: Optional[AttendanceSettings] = None


def get_settings() -> AttendanceSettings:
    return _override or AttendanceSettings.from_env()


def set_override(settings: Optional[AttendanceSettings]) -> None:
    global _override
    _override = settings
