"""SQLite stand-in for SQL Server, for local development and automated tests.

Enabled only with DB_ENGINE=sqlite (refused when APP_ENV=production). It lets
the whole stack run on a laptop without the Switrus SQL Server. Queries are
the same ones sent to SQL Server, with two mechanical translations:
pymssql's %s placeholders become ?, and the dbo. schema prefix is dropped.

    python -m database.sqlite_dev --seed     # create dev.sqlite3 with sample users
"""
import argparse
import os
import sqlite3
from datetime import date, datetime

SCHEMA_FILE = os.path.join(os.path.dirname(__file__), "sqlite_dev_schema.sql")

sqlite3.register_adapter(datetime, lambda v: v.isoformat(" ", "seconds"))
sqlite3.register_adapter(date, lambda v: v.isoformat())


def _translate(sql: str) -> str:
    return sql.replace("%s", "?").replace("dbo.", "")


class _Cursor:
    def __init__(self, cur: sqlite3.Cursor):
        self._cur = cur

    def execute(self, sql, params=None):
        if params is None:
            return self._cur.execute(_translate(sql))
        return self._cur.execute(_translate(sql), tuple(params))

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self):
        return self._cur.rowcount

    def close(self):
        self._cur.close()


class _Connection:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, timeout=10)

    def cursor(self):
        return _Cursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __bool__(self):
        return True


def db_path() -> str:
    return os.getenv("SQLITE_PATH", "dev.sqlite3")


def connect(path: str = None) -> _Connection:
    if os.getenv("APP_ENV", "").lower() == "production":
        raise RuntimeError("DB_ENGINE=sqlite is for development only")
    return _Connection(path or db_path())


def create_schema(path: str) -> None:
    with open(SCHEMA_FILE, encoding="utf-8") as f:
        script = f.read()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(script)
        conn.commit()
    finally:
        conn.close()


def seed(path: str, admin_password: str) -> None:
    from services.email import hash_password

    rows = [
        # The UI signs in with DeptPassword (plaintext, as in SW_USER); /login with EmpPassword.
    ("E1000", "admin", "ADMIN", "1", "BR01", "ravi.menon@switrus.com", "9800000001", None, 0, None, None),
        ("E1001", "Asha Nair", "ADMIN", "5", "BR01", "asha@switrus.com", "9876543210", "E1000", 0, None, None),
        ("E1002", "Kiran Das", "BPO", "5", "BR02", "kiran.das@switrus.com", "9800000003", "E1000", 0,
         date.today().isoformat(), date.today().isoformat()),
        ("E1003", "Old Account", "SALES", "6", "BR02", "old.account@switrus.com", None, None, 1, None, None),
    ]
    pw = hash_password(admin_password)
    conn = sqlite3.connect(path)
    try:
        for r in rows:
            conn.execute(
                "INSERT INTO SW_USER (EMPID, EmpName, Roles, DepartmentID, BRANCH_ID, Email, Phone, "
                "MANAGER_ID, IsDisabled, suspend_start, suspend_end, EmpPassword, DeptPassword) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", r + (pw, admin_password))
        conn.commit()
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Create a local SQLite dev database")
    parser.add_argument("--path", default=db_path())
    parser.add_argument("--seed", action="store_true", help="add sample users")
    parser.add_argument("--admin-password", default=os.getenv("DEV_ADMIN_PASSWORD", "Admin@12345"))
    args = parser.parse_args()
    if os.path.exists(args.path):
        os.remove(args.path)
    create_schema(args.path)
    if args.seed:
        seed(args.path, args.admin_password)
        print(f"Created {args.path}. Sign in to the UI as 'admin' with the password you set (default Admin@12345).")
    else:
        print(f"Created empty schema in {args.path}")


if __name__ == "__main__":
    main()
