"""Audit trail for administrative changes.

Every entry is written to logs/audit.log, and to the SW_USER_AUDIT table when
it exists (see database/migrations). A failed audit write is logged but never
fails the business operation that triggered it.
"""
import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional

from database.database import Database

logger = logging.getLogger(__name__)

_audit_logger = logging.getLogger("switrus.audit")
if not _audit_logger.handlers:
    os.makedirs("logs", exist_ok=True)
    _h = RotatingFileHandler(
        os.path.join("logs", "audit.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    _h.setFormatter(logging.Formatter("%(message)s"))
    _audit_logger.addHandler(_h)
    _audit_logger.setLevel(logging.INFO)
    _audit_logger.propagate = False

# Keys that must never be written to the audit trail, at any nesting level.
_SECRET_KEYS = {"emppassword", "deptpassword", "passwordchangeotp", "password"}


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("***" if str(k).lower() in _SECRET_KEYS else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def record(
    action: str,
    target_emp_id: Optional[str],
    actor_type: str,
    actor_id: Optional[str],
    details: Optional[Dict[str, Any]] = None,
    source_ip: Optional[str] = None,
) -> None:
    """Write one audit entry. Never raises."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    safe_details = _scrub(details or {})
    details_json = json.dumps(safe_details, default=str, sort_keys=True)

    try:
        _audit_logger.info(json.dumps({
            "at": now.isoformat() + "Z",
            "action": action,
            "target": target_emp_id,
            "actor_type": actor_type,
            "actor": actor_id,
            "ip": source_ip,
            "details": safe_details,
        }, default=str))
    except Exception:
        logger.exception("Audit file write failed for %s", action)

    try:
        ok, err = Database().db_update(
            "INSERT INTO SW_USER_AUDIT (occurred_at, actor_type, actor_id, action, "
            "target_emp_id, details, source_ip) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (now, actor_type, actor_id, action, target_emp_id, details_json, source_ip),
        )
        if not ok:
            logger.warning("Audit DB write failed for %s (%s); file log kept", action, err)
    except Exception:
        logger.exception("Audit DB write raised for %s", action)
