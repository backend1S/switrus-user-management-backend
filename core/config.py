"""Application settings read from the environment.

Everything that the security layer needs lives here so it is read in one
place. Loyaltri settings live in services/loyaltri/config.py because that
module must stay optional and self-contained.
"""
import os
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _csv(name: str, default: str = "") -> List[str]:
    raw = os.getenv(name, default) or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# Signs the JWT returned by POST /login. Other systems (CRM, accounting) may
# verify those tokens with this value, so it is kept as-is for compatibility.
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY not set in environment")

# Keys accepted in X-API-Key for trusted server-to-server integrations.
# Defaults to SECRET_KEY so existing integrations keep working; set API_KEYS
# to a new value (comma separated, old,new during a rotation) to decouple the
# integration key from the JWT signing secret.
API_KEYS: List[str] = _csv("API_KEYS") or [SECRET_KEY]

# Legacy ?api_key=<key> query parameter. URLs end up in access logs, proxy
# logs and browser history, so the header is the supported way. Kept on by
# default only so existing integrations keep working while they move to the
# X-API-Key header; every use is logged (key fingerprint, never the key).
# Set to false once no caller uses it. Never shown in the API docs.
ALLOW_API_KEY_QUERY = _bool("ALLOW_API_KEY_QUERY", True)

# Signs browser session cookies. Must differ from SECRET_KEY so that a leaked
# integration token can never be replayed as an admin session.
SESSION_SECRET = os.getenv("SESSION_SECRET") or ""
if not SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET not set in environment")
if len(SESSION_SECRET) < 32:
    raise RuntimeError("SESSION_SECRET must be at least 32 characters")
if SESSION_SECRET == SECRET_KEY:
    raise RuntimeError("SESSION_SECRET must be different from SECRET_KEY")

SESSION_COOKIE_NAME = "sw_session"
CSRF_COOKIE_NAME = "sw_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
SESSION_TTL_MINUTES = _int("SESSION_TTL_MINUTES", 480)
SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", True)

# POST /auth/login: after this many wrong passwords for one user name from one
# address within the window, further attempts get 429 until the window passes.
LOGIN_MAX_FAILURES = _int("LOGIN_MAX_FAILURES", 5)
LOGIN_FAILURE_WINDOW_SECONDS = _int("LOGIN_FAILURE_WINDOW_SECONDS", 900)

# Roles (case-insensitive, matched against SW_USER.Roles) that may perform
# administrative actions from the browser UI.
ADMIN_ROLES = {r.upper() for r in _csv("ADMIN_ROLES", "ADMIN")}
# Read-only UI roles, by what they may see (see core/permissions.py):
# own branch and own department / own branch, all departments / everything.
UI_BRANCH_DEPARTMENT_ROLES = {r.upper() for r in _csv("UI_BRANCH_DEPARTMENT_ROLES", "MANAGER")}
UI_BRANCH_ROLES = {r.upper() for r in _csv("UI_BRANCH_ROLES", "BRANCHMANAGER")}
# Empty by default: nobody but admins sees every branch.
UI_VIEWER_ROLES = {r.upper() for r in _csv("UI_VIEWER_ROLES", "")}

# No-login mode: browser requests without a session act as UI_DEFAULT_EMPID
# (role and disabled flag still checked per request). Anyone who can reach the
# API gets that user's access, so it is refused when APP_ENV=production.
UI_AUTH_DISABLED = _bool("UI_AUTH_DISABLED", False)
UI_DEFAULT_EMPID = (os.getenv("UI_DEFAULT_EMPID") or "").strip()
if UI_AUTH_DISABLED:
    if os.getenv("APP_ENV", "").strip().lower() == "production":
        raise RuntimeError("UI_AUTH_DISABLED cannot be used with APP_ENV=production")
    if not UI_DEFAULT_EMPID:
        raise RuntimeError("UI_AUTH_DISABLED requires UI_DEFAULT_EMPID")

CORS_EXTRA_ORIGINS = _csv("CORS_EXTRA_ORIGINS")

# Origins (besides the API's own host) allowed to use a session cookie. The UI
# is normally served same-origin through a proxy, so this is usually empty.
SESSION_ALLOWED_ORIGINS = set(_csv("SESSION_ALLOWED_ORIGINS"))
