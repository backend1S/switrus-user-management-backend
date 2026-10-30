import json
import logging
import os 
import random
import re
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status, BackgroundTasks, Body
import jwt
from database.database import Database
from models.models import SendOtpRequest, UserCreate, UserLogin, ChangePasswordRequest
from utils.password_policy import validate_password_policy
from contextlib import asynccontextmanager
from fastapi import Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from app.user import router as user_router
from app.auth import router as auth_router
from app.health import router as health_router
from app.loyaltri import router as loyaltri_router
from app.attendance import router as attendance_router
from core import audit, config
from core.security import Principal, authenticate_request, client_ip, require_admin
from services.auth_service import authenticate_credentials
from services.loyaltri import loyaltri_scheduler

# Import helpers from local email module (email.py)
from services.email import (
    _send_email_blocking,
    hash_password,
    verify_password,
    build_otp_body,
    pwd_context,
    PEPPER,
)

# Load environment variables
load_dotenv()

# JWT signing secret for /login tokens (validated in core.config).
SECRET_KEY = config.SECRET_KEY


@asynccontextmanager
async def lifespan(_app):
    # Only starts a thread when LOYALTRI_SCHEDULER=embedded. The sync runs off
    # the event loop, and nothing here waits for Loyaltri.
    loyaltri_scheduler.start_embedded()
    try:
        yield
    finally:
        loyaltri_scheduler.stop_embedded()


app = FastAPI(
    title="Switrus API",
    version="1.0",
    docs_url=None,
    redoc_url=None,
    dependencies=[Depends(authenticate_request)],
    lifespan=lifespan,
)

origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://localhost:3000",
    "https://127.0.0.1:3000",
    "http://localhost:3002",
    "https://127.0.0.1:3002",
    "http://135.13.36.155:9000",
    "http://103.189.88.199:9000",
    "http://135.13.36.155:9005",
    "https://acc.switrus.com",
    "http://acc.switrus.com",
    "http://crm.switrus.com",
    "https://crm.switrus.com",
    "https://dev.crm.switrus.com",
    "http://dev.crm.switrus.com",
    "https://dev.crm.switrus.com",
    "http://dev.acc.switrus.com",
    "https://dev.acc.switrus.com",
] + config.CORS_EXTRA_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/docs", include_in_schema=False)
async def swagger_docs():
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title="Switrus API Docs"
    )

# Logging setup
log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)

os.makedirs("logs", exist_ok=True)
log_path = os.path.join("logs", "app.log")

# Clear default handlers and use a rotating file handler
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

log_handler = RotatingFileHandler(
    log_path,
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
formatter = logging.Formatter("%(asctime)s - %(levelname)s -%(name)s -  %(message)s")
log_handler.setFormatter(formatter)

logging.basicConfig(level=log_level, handlers=[log_handler])
logger = logging.getLogger(__name__)


class _RedactSecrets(logging.Filter):
    """uvicorn's access log prints the full URL; hide ?api_key= values in it."""

    _pattern = re.compile(r"(?i)(api_key=)[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._pattern.sub(r"\1[redacted]", a) if isinstance(a, str) else a for a in record.args
            )
        elif isinstance(record.msg, str):
            record.msg = self._pattern.sub(r"\1[redacted]", record.msg)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactSecrets())

# App + DB
db = Database()

@app.post("/login")
def login_user(user: UserLogin):
    """Validate user and return JWT + minimal user info."""
    logger.info("Login attempt for username: %s", user.username)

    try:
        # Credential + disabled checks (shared with the browser sign-in).
        u = authenticate_credentials(db, user.username, user.password)
        emp_id      = u["emp_id"]
        branch_id   = u["branch_id"]
        dep_id      = u["dep_id"]
        role        = u["role"]
        emp_name    = u["emp_name"]
        email_addr  = u["email"]
        manager_id  = u["manager_id"]
        phone       = u["phone"]

        # For BPO users, fetch the branches assigned to them.
        assigned_branches = []
        if role and str(role).strip().upper() == "BPO":
            ok_ba, ba_rows = db.db_query(
                "SELECT BRANCH_ID FROM BPO_BRANCH_ASSIGN "
                "WHERE RTRIM(LTRIM(SW_USER_ID)) = RTRIM(LTRIM(%s))",
                (emp_id,),
            )
            if not ok_ba:
                logger.error(
                    "Database error while fetching BPO branches for %s: %s",
                    user.username, ba_rows,
                )
                raise HTTPException(status_code=500, detail="Database error")
            assigned_branches = [r[0] for r in ba_rows if r and r[0] is not None]

        # 3. Added to the JWT payload
        payload = {
            "emp_id": emp_id,
            "emp_name": emp_name,
            "branch_id": branch_id,
            "dep_id": dep_id,
            "role": role,
            "email": email_addr,
            "manager_id": manager_id,
            "phone": phone,
            "assigned_branches": assigned_branches,
            "exp": datetime.utcnow() + timedelta(hours=2),
        }

        try:
            token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        except Exception as e:
            # Log the error and the fact that SECRET_KEY is present (but encoding failed)
            logger.exception("JWT encoding failed for user %s. SECRET_KEY present: %s", user.username, bool(SECRET_KEY))
            # Surface a clear error to client (500)
            raise HTTPException(status_code=500, detail="JWT generation failed")

        # 4. Added to the return response
        return {
            "message": "User validated successfully",
            "token": token,
            "user": {
                "emp_id": emp_id,
                "emp_name": emp_name,
                "branch_id": branch_id,
                "dep_id": dep_id,
                "role": role,
                "email": email_addr,
                "manager_id": manager_id,
                "phone": phone,
                "assigned_branches": assigned_branches,
            },
        }
 
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error during login for %s", user.username)
        raise HTTPException(status_code=500, detail="Internal server error")

##########################  POST USERS API ######################


@app.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    request: Request,
    principal: Principal = Depends(require_admin),
):
    """Create a new user after validating input and password policy."""
    logger.info("Create user attempt: %s", payload.EmpName)

    try:
        # Trim the identifying fields and reject blank required values.
        payload.EmpID = payload.EmpID.strip()
        payload.EmpName = payload.EmpName.strip()
        for field in ("EmpID", "EmpName", "Email", "DepartmentID", "Roles"):
            if not str(getattr(payload, field) or "").strip():
                raise HTTPException(status_code=400, detail=f"{field} cannot be empty")

        ok, rows = db.db_query(
            "SELECT EMPID FROM SW_USER WHERE RTRIM(LTRIM(EMPID)) = RTRIM(LTRIM(%s))",
            (payload.EmpID,),
        )
        if not ok:
            logger.error("Database error while checking EMPID existence: %s", rows)
            raise HTTPException(status_code=500, detail="Database error")
        if rows:
            logger.warning("Employee ID already exists: %s", payload.EmpID)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Employee ID already exists")

        check_sql = (
            "SELECT EmpName FROM SW_USER WHERE RTRIM(LTRIM(EmpName)) = RTRIM(LTRIM(%s))"
        )
        ok, rows = db.db_query(check_sql, (payload.EmpName,))
        if not ok:
            logger.error("Database error while checking user existence: %s", rows)
            raise HTTPException(status_code=500, detail="Database error")

        if rows:
            logger.warning("User already exists: %s", payload.EmpName)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists")

        plain_password = payload.EmpPassword
        if not plain_password:
            raise HTTPException(status_code=400, detail="Password is required")

        ok_policy, reason = validate_password_policy(plain_password)
        if not ok_policy:
            raise HTTPException(status_code=400, detail=reason)

        hashed_password = hash_password(plain_password)

        insert_sql = (
            "INSERT INTO SW_USER (EmpID, EmpName, EmpPassword, Email, DepartmentID, Roles, "
            "BRANCH_ID, Phone, DeptEmail, DeptPassword, IsDisabled) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )

        params = (
            str(payload.EmpID),
            payload.EmpName,
            hashed_password,
            payload.Email,
            payload.DepartmentID,
            payload.Roles,
            payload.BRANCH_ID,
            payload.Phone,
            payload.DeptEmail,
            payload.DeptPassword,
            payload.IsDisabled,
        )

        ok_ins, res_ins = db.db_update(insert_sql, params)
        if not ok_ins:
            logger.error("Insert failed for user %s: %s", payload.EmpName, res_ins)
            raise HTTPException(status_code=500, detail="Database error")

        logger.info("User created: %s", payload.EmpName)
        audit.record(
            "user.create", payload.EmpID, principal.kind, principal.actor_id,
            {
                "emp_name": payload.EmpName, "email": payload.Email,
                "department_id": payload.DepartmentID, "role": payload.Roles,
                "branch_id": payload.BRANCH_ID, "phone": payload.Phone,
                "is_disabled": payload.IsDisabled,
            },
            client_ip(request),
        )
        return {"message": f"User {payload.EmpName} created successfully"}

    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error while creating user %s", payload.EmpName)
        raise HTTPException(status_code=500, detail="Internal server error")

####################### POST SEND OTP ########################


@app.post("/password/send-otp")
def send_otp(req: SendOtpRequest, background_tasks: BackgroundTasks):
    """Send an OTP to user's email for password reset (if email exists)."""
    email_addr = req.email.strip().lower()
    logger.info("OTP request for: %s", email_addr)

    ok, rows = db.db_query(
        "SELECT PasswordChangeOTP FROM SW_USER WHERE RTRIM(LTRIM(Email))=RTRIM(LTRIM(%s))",
        (email_addr,),
    )

    generic_msg = {"message": "If the mail exists, OTP has been sent."}
    if not ok or not rows:
        return generic_msg

    otp = str(random.randint(100000, 999999))
    now = datetime.utcnow()
    otp_json = {
        "otp": otp,
        "sent_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "attempts": 0,
        "status": "pending",
    }

    # Save OTP and check DB response
    upd_sql = "UPDATE SW_USER SET PasswordChangeOTP=%s WHERE RTRIM(LTRIM(Email)) = RTRIM(LTRIM(%s))"
    ok_upd, res_upd = db.db_update(upd_sql, (json.dumps(otp_json), email_addr))
    if not ok_upd:
        logger.error("Failed to store OTP for %s: %s", email_addr, res_upd)
        raise HTTPException(status_code=500, detail="Failed to store OTP")

    logger.info("Stored OTP for %s", email_addr)

    subject = "OTP for Password Reset"
    body = build_otp_body(otp, minutes_valid=5)

    # send email in background
    background_tasks.add_task(_send_email_blocking, email_addr, subject, body)

    return generic_msg

####################### POST VERIFY - OTP #################

@app.post("/password/verify-otp")
def verify_otp(req: dict = Body(...)):
    """Verify OTP previously sent to the user's email."""
    email_addr = req.get("email", "").strip().lower()
    otp_in = str(req.get("otp", "")).strip()

    if not email_addr or not otp_in:
        raise HTTPException(status_code=400, detail="email and otp required")

    ok, rows = db.db_query(
        "SELECT PasswordChangeOTP FROM SW_USER WHERE RTRIM(LTRIM(Email)) = RTRIM(LTRIM(%s))",
        (email_addr,),
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Database error")
    if not rows:
        raise HTTPException(status_code=400, detail="OTP not requested")

    otp_col = rows[0][0]
    if not otp_col:
        raise HTTPException(status_code=400, detail="OTP not requested")

    try:
        otp_obj = json.loads(otp_col)
    except Exception:
        logger.exception("Stored OTP corrupted for %s", email_addr)
        raise HTTPException(status_code=500, detail="Stored OTP corrupted")

    if otp_obj.get("status") != "pending":
        raise HTTPException(status_code=400, detail="OTP not available")

    try:
        exp = datetime.fromisoformat(otp_obj["expires_at"])
    except Exception:
        logger.exception("Invalid expires_at for OTP of %s", email_addr)
        raise HTTPException(status_code=500, detail="Stored OTP corrupted")

    if datetime.utcnow() > exp:
        otp_obj["status"] = "expired"
        db.db_update(
            "UPDATE SW_USER SET PasswordChangeOTP=%s WHERE RTRIM(LTRIM(Email)) = RTRIM(LTRIM(%s))",
            (json.dumps(otp_obj), email_addr),
        )
        raise HTTPException(status_code=400, detail="OTP expired")

    attempts = int(otp_obj.get("attempts", 0))
    if attempts >= 5:
        otp_obj["status"] = "locked"
        db.db_update(
            "UPDATE SW_USER SET PasswordChangeOTP=%s WHERE RTRIM(LTRIM(Email)) = RTRIM(LTRIM(%s))",
            (json.dumps(otp_obj), email_addr),
        )
        raise HTTPException(status_code=400, detail="OTP locked")

    if otp_in != str(otp_obj.get("otp")):
        otp_obj["attempts"] = attempts + 1
        db.db_update(
            "UPDATE SW_USER SET PasswordChangeOTP=%s WHERE RTRIM(LTRIM(Email)) = RTRIM(LTRIM(%s))",
            (json.dumps(otp_obj), email_addr),
        )
        raise HTTPException(status_code=400, detail="Invalid OTP")

    otp_obj["status"] = "verified"
    otp_obj["verified_at"] = datetime.utcnow().isoformat()
    otp_obj["verified_expires_at"] = (datetime.utcnow() + timedelta(minutes=10)).isoformat()

    db.db_update(
        "UPDATE SW_USER SET PasswordChangeOTP=%s WHERE RTRIM(LTRIM(Email)) = RTRIM(LTRIM(%s))",
        (json.dumps(otp_obj), email_addr),
    )

    return {"message": "OTP verified. You can now reset your password."}

####################### POST PASSWORD API #####################

@app.post("/password")
def change_password(req: ChangePasswordRequest):
    """Change password using a verified OTP stored in DB."""
    email_addr = req.email.strip().lower()
    new_password = req.new_password
    confirm_password = req.confirm_password

    logger.info("Password reset attempt for: %s", email_addr)

    if new_password != confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    ok_policy, reason = validate_password_policy(new_password)
    if not ok_policy:
        raise HTTPException(status_code=400, detail=reason)

    ok_db, rows = db.db_query(
        "SELECT PasswordChangeOTP FROM SW_USER WHERE RTRIM(LTRIM(Email))=RTRIM(LTRIM(%s))",
        (email_addr,),
    )
    if not ok_db:
        raise HTTPException(status_code=500, detail="Database error")
    if not rows:
        raise HTTPException(status_code=400, detail="OTP verification required")

    otp_json_str = rows[0][0]
    if not otp_json_str:
        raise HTTPException(status_code=400, detail="OTP verification required")

    try:
        otp_data = json.loads(otp_json_str)
    except Exception:
        logger.exception("Stored OTP corrupted for %s", email_addr)
        raise HTTPException(status_code=500, detail="Stored OTP corrupted")

    if otp_data.get("status") != "verified":
        raise HTTPException(status_code=400, detail="OTP verification required")

    verified_exp = datetime.fromisoformat(otp_data["verified_expires_at"])
    if datetime.utcnow() > verified_exp:
        otp_data["status"] = "expired"
        db.db_update(
            "UPDATE SW_USER SET PasswordChangeOTP=%s WHERE RTRIM(LTRIM(Email)) = RTRIM(LTRIM(%s))",
            (json.dumps(otp_data), email_addr),
        )
        raise HTTPException(status_code=400, detail="Verification expired. Request a new OTP.")

    # Hash the new password using helper
    hashed = hash_password(new_password)

    otp_data["status"] = "used"
    otp_data["used_at"] = datetime.utcnow().isoformat()

    update_sql = (
        "UPDATE SW_USER SET EmpPassword=%s, PasswordChangeOTP=%s "
        "WHERE RTRIM(LTRIM(Email)) = RTRIM(LTRIM(%s))"
    )
    ok_upd, res = db.db_update(update_sql, (hashed, json.dumps(otp_data), email_addr))
    if not ok_upd:
        logger.error("Password update failed for %s: %s", email_addr, res)
        raise HTTPException(status_code=500, detail="Database error")

    return {"message": "Password updated successfully"}




app.include_router(user_router)
app.include_router(auth_router)
app.include_router(health_router)
app.include_router(loyaltri_router)
app.include_router(attendance_router)


@app.get("/")
def home():
    return {"message": "API is running"}

