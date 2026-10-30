import re
from typing import Tuple

def validate_password_policy(password: str) -> Tuple[bool, str]:
    """
    Returns (ok, message). ok=True means password passes policy.
    Adjust rules below to match your org requirements.
    """
    if password is None:
        return False, "Password is required"

    if len(password) < 8:
        return False, "Password must be at least 8 characters long"

    # At least one uppercase
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter"

    # At least one lowercase
    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter"

    # At least one digit
    if not re.search(r"\d", password):
        return False, "Password must contain at least one digit"

    # At least one special character (non-word, non-space)
    if not re.search(r"[^\w\s]", password):
        return False, "Password must contain at least one special character"

    return True, ""
