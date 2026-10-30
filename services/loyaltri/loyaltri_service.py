"""The Loyaltri -> Switrus synchronization.

Field ownership policy (configurable within hard limits, see config.py):

* owned fields       - Loyaltri is the source of truth. A change in Loyaltri is
                       copied to SW_USER, unless an admin changed the same field
                       locally since the last sync (3-way check against the
                       stored baseline). Then the local value is kept and the
                       record is reported as a conflict. LOYALTRI_OVERWRITE_LOCAL_CHANGES
                       turns that protection off.
* fill-empty fields  - only written when SW_USER has no value.
* everything else    - never written: EMPID, passwords, OTP, Roles, IsDisabled,
                       leave dates, DeptEmail.

Additional guarantees:
* A blank/missing value from Loyaltri never clears a local value.
* Employees missing from Loyaltri are left untouched (never deleted/disabled).
* Matching is by EMPID only. Names are never used to match.
* Loyaltri is fetched completely before anything is written, so an outage
  mid-pagination writes nothing.
* Each record is applied independently; one failure doesn't stop the rest.
* Running twice with the same data writes nothing the second time.
"""
import hashlib
import json
import logging
import secrets
from collections import Counter
from typing import Any, Dict, List, Optional

from core import audit
from database.database import Database
from services.email import hash_password
from services.loyaltri.config import SYNCABLE_COLUMNS, LoyaltriSettings
from services.loyaltri.loyaltri_client import LoyaltriClient, LoyaltriError, build_client
from services.loyaltri.loyaltri_mapper import Employee, map_record
from services.loyaltri.loyaltri_sync import COUNT_FIELDS, SyncStore

logger = logging.getLogger(__name__)

ACTOR = "loyaltri-sync"
MAX_ITEMS_PER_RUN = 2000


def _norm(field: str, v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if field == "email":
        s = s.lower()
    if field == "department_id":
        try:
            s = str(int(float(s)))
        except ValueError:
            pass
    return s


def _same(field: str, a: Any, b: Any) -> bool:
    return _norm(field, a) == _norm(field, b)


def _record_hash(emp: Employee) -> str:
    data = {f: emp.get(f) for f in ("emp_id", *SYNCABLE_COLUMNS, "source_id", "employment_status")}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class LocalUsers:
    """Snapshot of the SW_USER fields the sync compares, keyed by upper EMPID."""

    COLS = ["EMPID", "EmpName", "Email", "Phone", "DepartmentID", "BRANCH_ID", "MANAGER_ID"]

    def __init__(self, db_factory=Database):
        self._db = db_factory
        self.by_id: Dict[str, Dict[str, Any]] = {}
        self.names: Dict[str, str] = {}  # upper name -> upper emp id

    def load(self) -> "LocalUsers":
        ok, rows = self._db().db_query("SELECT " + ", ".join(self.COLS) + " FROM dbo.SW_USER")
        if not ok:
            raise RuntimeError(f"Could not read Switrus users: {rows}")
        for r in rows:
            d = dict(zip(self.COLS, r))
            emp_id = str(d["EMPID"]).strip()
            self._put(emp_id, {
                "emp_id": emp_id, "emp_name": d["EmpName"], "email": d["Email"], "phone": d["Phone"],
                "department_id": d["DepartmentID"], "branch_id": d["BRANCH_ID"], "manager_id": d["MANAGER_ID"],
            })
        return self

    def _put(self, emp_id: str, values: Dict[str, Any]) -> None:
        self.by_id[emp_id.upper()] = values
        if values.get("emp_name"):
            self.names[str(values["emp_name"]).strip().upper()] = emp_id.upper()

    def get(self, emp_id: str) -> Optional[Dict[str, Any]]:
        return self.by_id.get(emp_id.upper())

    def name_owner(self, name: str) -> Optional[str]:
        return self.names.get(name.strip().upper())

    def apply(self, emp_id: str, changes: Dict[str, Any]) -> None:
        cur = self.by_id[emp_id.upper()]
        old_name = cur.get("emp_name")
        cur.update(changes)
        if "emp_name" in changes:
            if old_name:
                self.names.pop(str(old_name).strip().upper(), None)
            self.names[str(changes["emp_name"]).strip().upper()] = emp_id.upper()

    def add(self, emp_id: str, values: Dict[str, Any]) -> None:
        self._put(emp_id, dict(values, emp_id=emp_id))


class LoyaltriSyncService:
    def __init__(self, settings: LoyaltriSettings, store: Optional[SyncStore] = None,
                 client: Optional[LoyaltriClient] = None, db_factory=Database):
        self.s = settings
        self.store = store or SyncStore(db_factory)
        self._client = client
        self._db = db_factory

    # ------------------------------------------------------------ entry point
    def run(self, run_id: str) -> Dict[str, Any]:
        counts = {c: 0 for c in COUNT_FIELDS}
        self.store.mark_running(run_id)
        self._items_written = 0

        # 1. Fetch everything first. Nothing is written if Loyaltri fails.
        try:
            client = self._client or build_client(self.s)
            try:
                raw: List[Any] = []
                for page in client.iter_pages():
                    raw.extend(page)
            finally:
                client.close()
        except LoyaltriError as e:
            logger.warning("Loyaltri sync %s: fetch failed: %s", run_id, e)
            self.store.finish_run(run_id, "failed", counts, str(e))
            return self.store.get_run(run_id)
        except Exception as e:
            logger.exception("Loyaltri sync %s: unexpected fetch error", run_id)
            self.store.finish_run(run_id, "failed", counts, f"Unexpected error while fetching: {type(e).__name__}")
            return self.store.get_run(run_id)

        counts["records_received"] = len(raw)

        # 2. Validate + normalize.
        employees: List[Employee] = []
        for i, rec in enumerate(raw):
            counts["records_processed"] += 1
            emp, reason = map_record(rec, self.s.field_map)
            if emp is None:
                counts["failed"] += 1
                self._item(run_id, None, "failed", None, f"Record #{i + 1}: {reason}")
            else:
                employees.append(emp)

        dupes = {k for k, n in Counter(e.emp_id.upper() for e in employees).items() if n > 1}
        if dupes:
            for e in [e for e in employees if e.emp_id.upper() in dupes]:
                counts["failed"] += 1
                self._item(run_id, e.emp_id, "failed", None,
                           "Employee identifier appears more than once in Loyaltri; record not applied")
            employees = [e for e in employees if e.emp_id.upper() not in dupes]

        # 3. Load the local state once.
        try:
            local = LocalUsers(self._db).load()
            links = self.store.get_links()
        except Exception as e:
            logger.exception("Loyaltri sync %s: could not load local state", run_id)
            self.store.finish_run(run_id, "failed", counts, str(e)[:500])
            return self.store.get_run(run_id)

        # 4. Apply record by record.
        for emp in employees:
            try:
                outcome = self._apply(run_id, emp, local, links)
            except Exception as e:
                logger.exception("Loyaltri sync %s: record %s failed", run_id, emp.emp_id)
                outcome = "failed"
                self._item(run_id, emp.emp_id, "failed", None, f"Could not apply: {type(e).__name__}: {str(e)[:200]}")
            if outcome == "conflict":
                counts["conflicts"] += 1
            elif outcome == "updated+conflict":
                counts["updated"] += 1
                counts["conflicts"] += 1
            else:
                counts[outcome] += 1

        received = counts["records_received"]
        if received and counts["failed"] >= received:
            status = "failed"
        elif counts["failed"]:
            status = "partial"
        else:
            status = "succeeded"
        note = None
        if not received:
            note = "Loyaltri returned no employees. No Switrus users were changed."
        elif status == "failed":
            note = "Every record failed. No Switrus users were changed by this run."
        self.store.finish_run(run_id, status, counts, note)
        logger.info("Loyaltri sync %s finished: %s %s", run_id, status, counts)
        return self.store.get_run(run_id)

    # ------------------------------------------------------------ helpers
    def _item(self, run_id, emp_id, outcome, fields, message) -> None:
        if self._items_written >= MAX_ITEMS_PER_RUN:
            return
        self._items_written += 1
        try:
            self.store.add_item(run_id, emp_id, outcome, fields, message)
        except Exception:
            logger.exception("Could not record sync item for %s", emp_id)

    def _update_user(self, emp_id: str, changes: Dict[str, Any]) -> None:
        cols = [SYNCABLE_COLUMNS[f] for f in changes]  # allow-list; KeyError if not syncable
        sql = ("UPDATE dbo.SW_USER SET " + ", ".join(f"{c} = %s" for c in cols)
               + " WHERE RTRIM(LTRIM(EMPID)) = RTRIM(LTRIM(%s))")
        ok, err = self._db().db_update(sql, tuple(changes.values()) + (emp_id,))
        if not ok:
            raise RuntimeError(f"Database error: {err}")

    def _check_value(self, field: str, value: str, emp_id: str, local: LocalUsers) -> Optional[str]:
        """Return a reason if the value cannot be written to this user."""
        if field == "manager_id":
            if value.upper() == emp_id.upper():
                return "manager_id ignored: employee cannot be their own manager"
            if not local.get(value):
                return f"manager_id ignored: manager {value} is not a Switrus user"
        if field == "emp_name":
            owner = local.name_owner(value)
            if owner and owner != emp_id.upper():
                return f"emp_name not changed: '{value}' is already used by another user"
        return None

    # ------------------------------------------------------------ one record
    def _apply(self, run_id: str, emp: Employee, local: LocalUsers, links: Dict[str, Dict]) -> str:
        current = local.get(emp.emp_id)
        if current is None:
            return self._create(run_id, emp, local, links)

        emp_id = current["emp_id"]
        link = links.get(emp_id.upper())
        baseline: Dict[str, Any] = dict(link["synced_values"]) if link else {}
        new_baseline = dict(baseline)
        changes: Dict[str, Any] = {}
        conflicts: List[str] = []
        notes: List[str] = list(emp.warnings)

        for field in list(self.s.owned_fields) + list(self.s.fill_empty_fields):
            incoming = emp.get(field)
            if incoming is None:
                continue  # Loyaltri has no value: never clear local data
            local_val = current.get(field)
            if _same(field, incoming, local_val):
                new_baseline[field] = incoming
                continue

            if field in self.s.fill_empty_fields:
                if _norm(field, local_val) is not None:
                    continue  # local value present: Switrus keeps it
            else:  # owned
                if field in baseline:
                    if _same(field, incoming, baseline[field]):
                        continue  # unchanged in Loyaltri; the local edit stands
                    if not _same(field, local_val, baseline[field]) and not self.s.overwrite_local_changes:
                        conflicts.append(field)
                        continue

            reason = self._check_value(field, incoming, emp_id, local)
            if reason:
                notes.append(reason)
                continue
            changes[field] = incoming

        if changes:
            before = {f: current.get(f) for f in changes}
            self._update_user(emp_id, changes)
            local.apply(emp_id, changes)
            new_baseline.update(changes)
            audit.record("user.update", emp_id, ACTOR, run_id, {
                "changes": {f: {"from": before[f], "to": v} for f, v in changes.items()},
                "source": "loyaltri",
            })

        h = _record_hash(emp)
        if link is None or new_baseline != baseline or link.get("source_hash") != h:
            self.store.upsert_link(emp_id, emp.source_id, new_baseline, h)
            links[emp_id.upper()] = {"source_id": emp.source_id, "synced_values": new_baseline, "source_hash": h}

        if conflicts:
            notes.insert(0, "Changed in both Loyaltri and Switrus since the last sync; "
                            "kept the Switrus value for: " + ", ".join(conflicts))
        if changes:
            self._item(run_id, emp_id, "updated", list(changes) + conflicts, "; ".join(notes) or None)
            return "updated+conflict" if conflicts else "updated"
        if conflicts:
            self._item(run_id, emp_id, "conflict", conflicts, "; ".join(notes))
            return "conflict"
        if notes:
            self._item(run_id, emp_id, "unchanged", None, "; ".join(notes))
        return "unchanged"

    def _create(self, run_id: str, emp: Employee, local: LocalUsers, links: Dict[str, Dict]) -> str:
        if self.s.create_mode != "disabled":
            self._item(run_id, emp.emp_id, "new", None,
                       "In Loyaltri but not in Switrus. Not imported (LOYALTRI_CREATE_MODE=off); "
                       "add the user in Switrus if they need access.")
            return "skipped"

        missing = [f for f in ("emp_name", "email", "department_id") if not emp.get(f)]
        if missing:
            self._item(run_id, emp.emp_id, "skipped", missing,
                       "Not imported: Loyaltri record lacks " + ", ".join(missing))
            return "skipped"
        if local.name_owner(emp.emp_name):
            self._item(run_id, emp.emp_id, "conflict", ["emp_name"],
                       f"Not imported: user name '{emp.emp_name}' already belongs to another user")
            return "conflict"

        manager = emp.manager_id if emp.manager_id and local.get(emp.manager_id) else None
        # Created disabled with an unusable random password: an admin reviews
        # and enables the account; the employee sets a password via OTP reset.
        ok, err = self._db().db_update(
            "INSERT INTO SW_USER (EmpID, EmpName, EmpPassword, Email, DepartmentID, Roles, "
            "BRANCH_ID, Phone, MANAGER_ID, DeptEmail, DeptPassword, IsDisabled) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (emp.emp_id, emp.emp_name, hash_password(secrets.token_urlsafe(32)), emp.email,
             emp.department_id, self.s.default_role, emp.branch_id, emp.phone, manager, None, None, 1),
        )
        if not ok:
            raise RuntimeError(f"Database error: {err}")

        values = {"emp_name": emp.emp_name, "email": emp.email, "phone": emp.phone,
                  "department_id": emp.department_id, "branch_id": emp.branch_id, "manager_id": manager}
        local.add(emp.emp_id, values)
        baseline = {f: values[f] for f in list(self.s.owned_fields) + list(self.s.fill_empty_fields)
                    if values.get(f) is not None}
        self.store.upsert_link(emp.emp_id, emp.source_id, baseline, _record_hash(emp))
        audit.record("user.create", emp.emp_id, ACTOR, run_id, {
            "emp_name": emp.emp_name, "department_id": emp.department_id,
            "role": self.s.default_role, "is_disabled": 1, "source": "loyaltri",
        })
        note = "Created as disabled; review and enable in Switrus"
        if emp.manager_id and not manager:
            note += f"; manager {emp.manager_id} is not a Switrus user so it was left empty"
        self._item(run_id, emp.emp_id, "created", None, note)
        return "created"
