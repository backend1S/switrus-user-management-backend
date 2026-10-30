"""Loyaltri integration settings.

Loyaltri's API contract (endpoints, authentication, payload, pagination) has
not been provided, so nothing about it is assumed here: every detail the HTTP
client needs is configuration, and the client reports itself "not configured"
until the required values are set. See docs/LOYALTRI.md.
"""
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

# Canonical employee fields the mapper produces. emp_id is the match key.
CANONICAL_FIELDS = ("emp_id", "emp_name", "email", "phone", "department_id", "branch_id", "manager_id")
META_FIELDS = ("source_id", "employment_status")

# Fields Loyaltri may ever write, and the SW_USER column each maps to.
SYNCABLE_COLUMNS: Dict[str, str] = {
    "emp_name": "EmpName",
    "email": "Email",
    "phone": "Phone",
    "department_id": "DepartmentID",
    "branch_id": "BRANCH_ID",
    "manager_id": "MANAGER_ID",
}
# Never written by the sync, whatever the configuration says.
PROTECTED_COLUMNS = (
    "EMPID", "EmpPassword", "DeptPassword", "PasswordChangeOTP", "Roles",
    "IsDisabled", "suspend_start", "suspend_end", "DeptEmail",
)

CLIENT_MODES = ("disabled", "mock", "http")
SCHEDULER_MODES = ("embedded", "external", "off")
PAGINATION_MODES = ("none", "page", "offset", "cursor")
CREATE_MODES = ("off", "disabled")

# Field map used by the mock client's sample data (services/loyaltri/mock_employees.json).
# It describes the mock file only; it is NOT a guess at Loyaltri's real field names.
MOCK_FIELD_MAP = {
    "emp_id": "employee_code",
    "emp_name": "full_name",
    "email": "work_email",
    "phone": "mobile",
    "department_id": "department_code",
    "branch_id": "branch_code",
    "manager_id": "reporting_manager_code",
    "source_id": "id",
    "employment_status": "status",
}


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _csv(name: str, default: str) -> List[str]:
    return [p.strip().lower() for p in _env(name, default).split(",") if p.strip()]


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


def _json(name: str):
    raw = _env(name)
    if not raw:
        return None, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, f"{name} is not valid JSON ({e.msg})"


@dataclass(frozen=True)
class LoyaltriSettings:
    client_mode: str = "disabled"
    sync_enabled: bool = False
    interval_minutes: int = 60
    scheduler_mode: str = "embedded"
    poll_seconds: int = 15

    base_url: str = ""
    employees_path: str = ""
    auth_header: str = ""
    auth_scheme: str = ""
    api_key: str = field(default="", repr=False)
    extra_query: Dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 15.0
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0

    records_path: str = ""
    pagination: str = "none"
    page_param: str = ""
    page_size_param: str = ""
    page_size: int = 100
    first_page: int = 1
    cursor_param: str = ""
    next_cursor_path: str = ""
    max_pages: int = 500

    field_map: Dict[str, str] = field(default_factory=dict)
    owned_fields: Tuple[str, ...] = ("phone", "manager_id")
    fill_empty_fields: Tuple[str, ...] = ("email", "department_id", "branch_id")
    overwrite_local_changes: bool = False
    create_mode: str = "off"
    default_role: str = ""

    mock_file: str = ""
    mock_failure: str = "none"

    config_errors: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "LoyaltriSettings":
        errors: List[str] = []
        field_map, err = _json("LOYALTRI_FIELD_MAP")
        if err:
            errors.append(err)
        extra_query, err = _json("LOYALTRI_EXTRA_QUERY")
        if err:
            errors.append(err)
        mode = _env("LOYALTRI_CLIENT", "disabled").lower()
        if mode == "mock" and not field_map:
            field_map = dict(MOCK_FIELD_MAP)
        return cls(
            client_mode=mode,
            sync_enabled=_bool("LOYALTRI_SYNC_ENABLED", False),
            interval_minutes=max(5, _num("LOYALTRI_SYNC_INTERVAL_MINUTES", 60)),
            scheduler_mode=_env("LOYALTRI_SCHEDULER", "embedded").lower(),
            poll_seconds=max(2, _num("LOYALTRI_POLL_SECONDS", 15)),
            base_url=_env("LOYALTRI_BASE_URL").rstrip("/"),
            employees_path=_env("LOYALTRI_EMPLOYEES_PATH"),
            auth_header=_env("LOYALTRI_AUTH_HEADER"),
            auth_scheme=_env("LOYALTRI_AUTH_SCHEME"),
            api_key=_env("LOYALTRI_API_KEY"),
            extra_query={str(k): str(v) for k, v in (extra_query or {}).items()},
            timeout_seconds=_num("LOYALTRI_TIMEOUT_SECONDS", 15.0, float),
            max_retries=min(10, max(0, _num("LOYALTRI_MAX_RETRIES", 3))),
            retry_backoff_seconds=_num("LOYALTRI_RETRY_BACKOFF_SECONDS", 1.0, float),
            records_path=_env("LOYALTRI_RECORDS_PATH"),
            pagination=_env("LOYALTRI_PAGINATION", "none").lower(),
            page_param=_env("LOYALTRI_PAGE_PARAM"),
            page_size_param=_env("LOYALTRI_PAGE_SIZE_PARAM"),
            page_size=max(1, _num("LOYALTRI_PAGE_SIZE", 100)),
            first_page=_num("LOYALTRI_FIRST_PAGE", 1),
            cursor_param=_env("LOYALTRI_CURSOR_PARAM"),
            next_cursor_path=_env("LOYALTRI_NEXT_CURSOR_PATH"),
            max_pages=max(1, _num("LOYALTRI_MAX_PAGES", 500)),
            field_map={str(k): str(v) for k, v in (field_map or {}).items()},
            owned_fields=tuple(_csv("LOYALTRI_OWNED_FIELDS", "phone,manager_id")),
            fill_empty_fields=tuple(_csv("LOYALTRI_FILL_EMPTY_FIELDS", "email,department_id,branch_id")),
            overwrite_local_changes=_bool("LOYALTRI_OVERWRITE_LOCAL_CHANGES", False),
            create_mode=_env("LOYALTRI_CREATE_MODE", "off").lower(),
            default_role=_env("LOYALTRI_DEFAULT_ROLE"),
            mock_file=_env("LOYALTRI_MOCK_FILE"),
            mock_failure=_env("LOYALTRI_MOCK_FAILURE", "none").lower(),
            config_errors=tuple(errors),
        )

    def problems(self) -> List[str]:
        """Everything that prevents a sync from running. Empty list = ready."""
        p: List[str] = list(self.config_errors)
        if self.client_mode not in CLIENT_MODES:
            p.append(f"LOYALTRI_CLIENT must be one of {', '.join(CLIENT_MODES)}")
            return p
        if self.client_mode == "disabled":
            p.append("Loyaltri integration is disabled (LOYALTRI_CLIENT=disabled)")
            return p

        if self.client_mode == "http":
            for name, value in (("LOYALTRI_BASE_URL", self.base_url),
                                ("LOYALTRI_EMPLOYEES_PATH", self.employees_path)):
                if not value:
                    p.append(f"{name} is not set")
            if self.base_url and not self.base_url.startswith("https://") \
                    and not self.base_url.startswith("http://localhost"):
                p.append("LOYALTRI_BASE_URL must use https://")
            if self.api_key and not self.auth_header:
                p.append("LOYALTRI_AUTH_HEADER is required when LOYALTRI_API_KEY is set")
            if self.pagination not in PAGINATION_MODES:
                p.append(f"LOYALTRI_PAGINATION must be one of {', '.join(PAGINATION_MODES)}")
            if self.pagination in ("page", "offset") and not self.page_param:
                p.append("LOYALTRI_PAGE_PARAM is required for page/offset pagination")
            if self.pagination == "cursor" and not (self.cursor_param and self.next_cursor_path):
                p.append("LOYALTRI_CURSOR_PARAM and LOYALTRI_NEXT_CURSOR_PATH are required for cursor pagination")

        if not self.field_map:
            p.append("LOYALTRI_FIELD_MAP is not set (which Loyaltri field holds each Switrus field)")
        elif "emp_id" not in self.field_map:
            p.append("LOYALTRI_FIELD_MAP must map emp_id (the stable employee identifier)")
        unknown = [k for k in self.field_map if k not in CANONICAL_FIELDS + META_FIELDS]
        if unknown:
            p.append(f"LOYALTRI_FIELD_MAP has unknown keys: {', '.join(unknown)}")

        bad = [f for f in self.owned_fields + self.fill_empty_fields if f not in SYNCABLE_COLUMNS]
        if bad:
            p.append(f"Unknown or protected sync field(s): {', '.join(bad)}")
        both = set(self.owned_fields) & set(self.fill_empty_fields)
        if both:
            p.append(f"Field(s) listed as both owned and fill-empty: {', '.join(sorted(both))}")

        if self.create_mode not in CREATE_MODES:
            p.append(f"LOYALTRI_CREATE_MODE must be one of {', '.join(CREATE_MODES)}")
        if self.create_mode == "disabled" and not self.default_role:
            p.append("LOYALTRI_DEFAULT_ROLE is required when LOYALTRI_CREATE_MODE=disabled")
        return p

    @property
    def is_configured(self) -> bool:
        return not self.problems()

    def public_summary(self) -> Dict:
        """Safe to return to the admin UI: never includes the credential."""
        return {
            "client_mode": self.client_mode,
            "base_url": self.base_url or None,
            "credentials_set": bool(self.api_key),
            "scheduled_sync_enabled": self.sync_enabled,
            "interval_minutes": self.interval_minutes,
            "scheduler_mode": self.scheduler_mode,
            "pagination": self.pagination,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "field_policy": {
                "match_on": "emp_id -> SW_USER.EMPID",
                "owned_fields": list(self.owned_fields),
                "fill_empty_fields": list(self.fill_empty_fields),
                "overwrite_local_changes": self.overwrite_local_changes,
                "create_mode": self.create_mode,
                "never_synced": list(PROTECTED_COLUMNS),
            },
        }


_override: Optional[LoyaltriSettings] = None


def get_settings() -> LoyaltriSettings:
    """Read fresh from the environment each call (cheap), unless overridden by tests."""
    return _override or LoyaltriSettings.from_env()


def set_override(settings: Optional[LoyaltriSettings]) -> None:
    global _override
    _override = settings
