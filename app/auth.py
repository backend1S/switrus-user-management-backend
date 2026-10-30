"""Browser session endpoints for the User Management UI.

Integrations keep using POST /login with X-API-Key; this router does not
change that contract (EmpName + Argon2 EmpPassword). POST /auth/login checks
EmpName + SW_USER.DeptPassword (legacy plaintext, compared in constant time;
see services/auth_service.authenticate_ui_credentials), then issues an HttpOnly
session cookie instead of returning a token, so no secret ever reaches the
browser's JavaScript.
"""
import logging
import threading
import time
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from core import audit, config
from core.security import (
    Principal,
    build_ui_principal,
    client_ip,
    decode_session,
    issue_session,
    origin_allowed,
    revoke_session,
    require_session,
)
from database.database import Database
from models.models import SessionLogin
from services.auth_service import authenticate_ui_credentials

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth (browser session)"])


def _session_body(p: Principal, csrf: str) -> Dict:
    """What the UI needs to adapt itself. The backend still enforces all of it."""
    return {
        "user": {
            "emp_id": p.emp_id, "emp_name": p.emp_name, "role": p.role,
            "branch_id": p.home_branch_id, "department_id": p.home_department_id,
        },
        "permissions": {
            "is_admin": p.is_admin,
            "can_edit": p.is_admin,
            # all | branch | branch_department, and the values it is limited to.
            "scope": p.scope,
            "scope_branch_id": p.branch_id,
            "scope_department_id": p.department_id,
        },
        "csrf_token": csrf,
    }


# ---------------------------------------------------------------- sign-in throttling
# Per process and in memory: enough to stop password guessing through the UI.
# Keyed on user name + client address, so one attacker cannot lock a user out
# from everywhere.

_failures: Dict[str, List[float]] = {}
_failures_lock = threading.Lock()


def _throttle_key(username: str, request: Request) -> str:
    return f"{username.strip().lower()}|{client_ip(request) or '-'}"


def _recent_failures(key: str, now: float) -> List[float]:
    window = config.LOGIN_FAILURE_WINDOW_SECONDS
    times = [t for t in _failures.get(key, []) if now - t < window]
    if times:
        _failures[key] = times
    else:
        _failures.pop(key, None)
    return times


def _check_throttle(key: str) -> None:
    now = time.time()
    with _failures_lock:
        times = _recent_failures(key, now)
    if len(times) >= config.LOGIN_MAX_FAILURES:
        wait = int(config.LOGIN_FAILURE_WINDOW_SECONDS - (now - times[0])) + 1
        minutes = max(1, (wait + 59) // 60)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed sign-in attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.",
            headers={"Retry-After": str(wait)},
        )


def _record_failure(key: str) -> None:
    with _failures_lock:
        _failures.setdefault(key, []).append(time.time())


def _clear_failures(key: str) -> None:
    with _failures_lock:
        _failures.pop(key, None)


# ---------------------------------------------------------------- endpoints

@router.post(
    "/login",
    summary="Sign in (browser session)",
    responses={
        401: {"description": "Wrong user name or password"},
        403: {"description": "Account disabled, role without UI access, or cross-site request"},
        429: {"description": "Too many failed attempts"},
    },
)
def session_login(body: SessionLogin, request: Request, response: Response) -> Dict:
    """Checks EmpName + DeptPassword against SW_USER and sets the HttpOnly session
    cookie. The response carries the CSRF token that every later POST, PUT,
    PATCH and DELETE must send in the X-CSRF-Token header."""
    # A cross-site page must not be able to sign a visitor in (login CSRF).
    if not origin_allowed(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-site request rejected")

    key = _throttle_key(body.username, request)
    _check_throttle(key)

    try:
        user = authenticate_ui_credentials(Database(), body.username, body.password)
    except HTTPException as e:
        if e.status_code == status.HTTP_401_UNAUTHORIZED:
            _record_failure(key)
        raise
    _clear_failures(key)

    emp_id = str(user["emp_id"]).strip()
    try:
        principal = build_ui_principal(emp_id, user["emp_name"], user["role"], user["branch_id"], user["dep_id"])
    except HTTPException:
        logger.warning("Sign-in refused for %s: role %r has no UI access here", emp_id, user["role"])
        raise

    token, csrf, max_age = issue_session(emp_id)
    response.set_cookie(
        config.SESSION_COOKIE_NAME,
        token,
        max_age=max_age,
        path="/",
        httponly=True,
        secure=config.SESSION_COOKIE_SECURE,
        samesite="strict",
    )
    audit.record("session.login", emp_id, "session", emp_id, {}, client_ip(request))
    logger.info("Browser sign-in for %s", emp_id)
    return _session_body(principal, csrf)


@router.get("/me", summary="Current browser session")
def session_me(request: Request, principal: Principal = Depends(require_session)) -> Dict:
    claims = getattr(request.state, "session_claims", None) or {}
    return _session_body(principal, claims.get("csrf", ""))


@router.post("/logout", summary="Sign out")
def session_logout(request: Request, response: Response) -> Dict:
    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    claims = decode_session(token) if token else None
    if claims:
        revoke_session(claims)
        audit.record("session.logout", claims["sub"], "session", claims["sub"], {}, client_ip(request))
    response.delete_cookie(config.SESSION_COOKIE_NAME, path="/", httponly=True,
                           secure=config.SESSION_COOKIE_SECURE, samesite="strict")
    response.delete_cookie(config.CSRF_COOKIE_NAME, path="/")
    return {"message": "Signed out"}
