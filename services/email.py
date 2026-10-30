import os
import json
import logging
import smtplib
from email.message import EmailMessage
from datetime import datetime
from passlib.context import CryptContext
from dotenv import load_dotenv

# Load .env once when module is imported
load_dotenv()

# --- Configuration (read from environment) ---
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM = os.getenv("SMTP_FROM") or SMTP_USER
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in ("1", "true", "yes")

# Password hashing / verification context + pepper
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
PEPPER = os.getenv("PASSWORD_PEPPER", "defaultPepper")

# Logger for this module
logger = logging.getLogger(__name__)


def _send_email_blocking(to_email: str, subject: str, body: str) -> None:
    """Send a plain-text email using configured SMTP server in a blocking manner.

    This function intentionally keeps a simple contract: it logs success or
    exception and does not raise so callers (e.g. background tasks) can
    handle failures silently.
    """
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        logger.warning("SMTP not configured (host/user/password). Skipping email send.")
        return

    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            if SMTP_USE_TLS:
                s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        logger.info("Email sent to %s", to_email)
    except Exception:
        logger.exception("Failed to send email to %s", to_email)


def hash_password(plain_password: str) -> str:
    """Hash a password using configured pwd_context and application pepper."""
    return pwd_context.hash(plain_password + PEPPER)


def is_supported_hash(stored: str) -> bool:
    """True when the stored value is a hash this service can verify (Argon2)."""
    try:
        return bool(stored) and pwd_context.identify(stored) is not None
    except Exception:
        return False


def verify_password(plain_password: str, hashed: str) -> bool:
    """Verify a plain password against a stored hash (with pepper).

    A stored value that is not a recognised hash (empty, legacy plaintext) never
    matches: it is not compared as text. Such accounts must reset their password
    (see database/password_report.py).
    """
    if not is_supported_hash(hashed):
        logger.warning("Stored password is not a supported hash; sign-in refused")
        return False
    try:
        return pwd_context.verify(plain_password + PEPPER, hashed)
    except Exception:
        logger.exception("Password verification failed")
        return False


# Utility to build OTP email body (simple)
def build_otp_body(otp: str, minutes_valid: int = 5) -> str:
    now = datetime.utcnow().isoformat()
    return (
        f"Your OTP code is {otp}.\n\n"
        f"This code was generated at {now} UTC and is valid for {minutes_valid} minutes.\n"
        "If you did not request this, ignore this email."
    )
