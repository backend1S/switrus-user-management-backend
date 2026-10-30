"""Read-only report on SW_USER sign-in credentials. Never prints stored values.

* User Management UI (POST /auth/login) signs in with DeptPassword: legacy
  plaintext, often shared by many employees. Lists accounts with no
  DeptPassword and counts how many share one, with how many of those are admins.
* Integrations (POST /login) sign in with EmpPassword: only Argon2 hashes work.
  Lists accounts whose EmpPassword is empty or not a supported hash.

    python -m database.password_report

Never changes anything. See README, "Moving off DeptPassword".
"""
from collections import Counter

from core.security import is_disabled_value, role_is_admin
from database.database import Database
from services.email import is_supported_hash


def _rows():
    ok, rows = Database().db_query(
        "SELECT EMPID, EmpName, Roles, IsDisabled, EmpPassword, DeptPassword FROM dbo.SW_USER ORDER BY EMPID"
    )
    if not ok:
        raise SystemExit(f"Database error: {rows}")
    return rows


def unverifiable_accounts(rows=None):
    """Accounts that cannot use POST /login (EmpPassword empty or not Argon2)."""
    out = []
    for emp_id, name, _role, disabled, stored, _dept in rows if rows is not None else _rows():
        if is_supported_hash(stored or ""):
            continue
        out.append({
            "emp_id": str(emp_id).strip(),
            "emp_name": name,
            "is_disabled": bool(disabled),
            "problem": "empty" if not (stored or "").strip() else "not a supported hash",
        })
    return out


def dept_password_summary(rows=None):
    rows = rows if rows is not None else _rows()
    usable = [r for r in rows if r[5] and str(r[5]).strip()]
    counts = Counter(r[5] for r in usable)
    shared = [r for r in usable if counts[r[5]] > 1]
    admin_values = {r[5] for r in usable if role_is_admin(r[2])}
    return {
        "missing": [{"emp_id": str(r[0]).strip(), "emp_name": r[1]} for r in rows if r not in usable],
        "distinct_values": len(counts),
        "users_sharing": len(shared),
        "largest_group": max(counts.values()) if counts else 0,
        "admins_sharing": sum(1 for r in shared if role_is_admin(r[2])),
        "non_admins_with_an_admins_password": sum(
            1 for r in usable if not role_is_admin(r[2]) and r[5] in admin_values),
        "enabled_admins_can_sign_in": sum(
            1 for r in usable if role_is_admin(r[2]) and not is_disabled_value(r[3])),
    }


def main():
    rows = _rows()
    d = dept_password_summary(rows)
    print("User Management sign-in (DeptPassword, plaintext):")
    print(f"  admins able to sign in: {d['enabled_admins_can_sign_in']}")
    print(f"  distinct passwords: {d['distinct_values']}, users sharing one: {d['users_sharing']} "
          f"(largest group {d['largest_group']})")
    print(f"  admins sharing their password: {d['admins_sharing']}; "
          f"non-admins whose password equals an admin's: {d['non_admins_with_an_admins_password']}")
    print(f"  no DeptPassword (cannot sign in): {len(d['missing'])}")
    for a in d["missing"]:
        print(f"    {a['emp_id']:<24} {a['emp_name'] or ''}")

    accounts = unverifiable_accounts(rows)
    print(f"\nIntegration sign-in (/login, EmpPassword Argon2): {len(accounts)} account(s) cannot sign in")
    for a in accounts:
        print(f"    {a['emp_id']:<24} {str(a['emp_name'] or ''):<30} disabled={a['is_disabled']!s:<6} {a['problem']}")


if __name__ == "__main__":
    main()
