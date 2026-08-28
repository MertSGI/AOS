"""AOS-5 Distributed Multi-PC Coordination — Stage 11B-R2 Final SQLite Coordination Backend.

Durable local SQLite reference implementation of CoordinationBackend:
- Versioned SQLite storage (schema version 0.1.0)
- Strict non-destructive pre-validation of SQLite primary keys:
  - meta: PRIMARY KEY (key) [pk ordinal 1]
  - workers: COMPOSITE PRIMARY KEY (worker_id, session_id) [pk ordinals 1, 2]
  - leases: PRIMARY KEY (task_id) [pk ordinal 1]
- Strict _parse_worker_row decoder validating registered_at on all worker read paths
- Full LeaseSnapshot equality binding for concurrent claim results
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from typing import Callable, FrozenSet, Optional, Union

from aos.coordination import (
    ClaimDisposition,
    ClaimResult,
    CoordinationBackend,
    CoordinationStorageError,
    InvalidClockError,
    InvalidIdentityError,
    InvalidInputError,
    LeaseSnapshot,
    LeaseStatus,
    WorkerIdentity,
    _compute_safe_expiry,
    _validate_generation,
    _validate_ttl,
    default_utc_clock,
)

SQLITE_SCHEMA_VERSION = "0.1.0"

EXPECTED_TABLES = {"meta", "workers", "leases"}

EXPECTED_COLUMNS = {
    "meta": {"key", "value"},
    "workers": {"worker_id", "session_id", "capability_tags_json", "registered_at"},
    "leases": {
        "task_id",
        "worker_id",
        "session_id",
        "lease_id",
        "acquired_at",
        "last_heartbeat_at",
        "expires_at",
        "ttl_seconds",
        "generation",
        "status",
    },
}

EXPECTED_PRIMARY_KEYS = {
    "meta": [("key", 1)],
    "workers": [("worker_id", 1), ("session_id", 2)],
    "leases": [("task_id", 1)],
}

INIT_SCHEMA_SQL_STATEMENTS = [
    """
    CREATE TABLE meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE workers (
        worker_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        capability_tags_json TEXT NOT NULL,
        registered_at TEXT NOT NULL,
        PRIMARY KEY (worker_id, session_id)
    );
    """,
    """
    CREATE TABLE leases (
        task_id TEXT PRIMARY KEY,
        worker_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        lease_id TEXT NOT NULL,
        acquired_at TEXT NOT NULL,
        last_heartbeat_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        ttl_seconds REAL NOT NULL,
        generation INTEGER NOT NULL,
        status TEXT NOT NULL
    );
    """,
]


@dataclass(frozen=True)
class WorkerStorageRow:
    worker_id: str
    session_id: str
    capability_tags: FrozenSet[str]
    registered_at: datetime


def _validate_busy_timeout_ms(val: int) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        raise InvalidInputError("busy_timeout_ms must be an integer")
    if val <= 0 or val > 60000:
        raise InvalidInputError("busy_timeout_ms must be a positive integer <= 60000")
    return val


def _iso_to_datetime(val_str: str) -> datetime:
    if not isinstance(val_str, str) or not val_str.strip():
        raise CoordinationStorageError("Persisted datetime must be a non-empty string")

    val = val_str.strip()
    try:
        dt = datetime.fromisoformat(val)
    except (ValueError, TypeError) as exc:
        raise CoordinationStorageError(f"Persisted timestamp '{val_str}' is malformed") from exc

    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise CoordinationStorageError(f"Persisted timestamp '{val_str}' is naive; timezone-aware required")

    # Require ISO string representation to end with UTC offset (+00:00 or Z)
    if not (val.endswith("+00:00") or val.endswith("Z") or val.endswith("+0000")):
        raise CoordinationStorageError(f"Persisted timestamp '{val_str}' is not UTC normalized")

    return dt.astimezone(timezone.utc)


def _datetime_to_iso(dt: datetime) -> str:
    if not isinstance(dt, datetime):
        raise InvalidClockError("Clock provider must return a datetime instance")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise InvalidClockError("Clock provider returned naive datetime; timezone-aware required")
    return dt.astimezone(timezone.utc).isoformat()


def _decode_capability_tags_json(raw_json: str) -> FrozenSet[str]:
    if not isinstance(raw_json, str):
        raise CoordinationStorageError("Corrupt capability_tags_json: not a string")
    try:
        data = json.loads(raw_json)
    except Exception as exc:
        raise CoordinationStorageError("Corrupt capability_tags_json: invalid JSON syntax") from exc

    if not isinstance(data, list):
        raise CoordinationStorageError("Corrupt capability_tags_json: JSON data must be a list")

    seen = set()
    for item in data:
        if not isinstance(item, str):
            raise CoordinationStorageError("Corrupt capability_tags_json: list elements must be strings")
        if not item or item != item.strip():
            raise CoordinationStorageError("Corrupt capability_tags_json: strings must be non-empty and trimmed")
        if item in seen:
            raise CoordinationStorageError("Corrupt capability_tags_json: duplicate tags found")
        seen.add(item)

    # Canonical writer stores sorted tags
    if data != sorted(list(seen)):
        raise CoordinationStorageError("Corrupt capability_tags_json: tags not deterministically sorted")

    return frozenset(seen)


def _parse_worker_row(row: tuple) -> WorkerStorageRow:
    if len(row) != 4:
        raise CoordinationStorageError("Corrupt worker row: incorrect column count")

    worker_id, session_id, tags_json, registered_at_str = row

    if not isinstance(worker_id, str) or not worker_id.strip():
        raise CoordinationStorageError("Corrupt worker_id in worker row")
    if not isinstance(session_id, str) or not session_id.strip():
        raise CoordinationStorageError("Corrupt session_id in worker row")

    tags = _decode_capability_tags_json(tags_json)
    registered_at = _iso_to_datetime(registered_at_str)

    return WorkerStorageRow(
        worker_id=worker_id,
        session_id=session_id,
        capability_tags=tags,
        registered_at=registered_at,
    )


class SQLiteCoordinationBackend:
    """Durable local SQLite reference backend implementing CoordinationBackend protocol."""

    def __init__(
        self,
        db_path: Union[str, Path],
        clock_provider: Optional[Callable[[], datetime]] = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        if db_path is None or str(db_path).strip() == "":
            raise InvalidInputError("db_path must be an explicit non-empty file path")

        self._busy_timeout_ms = _validate_busy_timeout_ms(busy_timeout_ms)
        self._db_path = Path(db_path).resolve()
        self._clock_provider = clock_provider or default_utc_clock

        # Ensure parent directory exists
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _now(self) -> datetime:
        now = self._clock_provider()
        if not isinstance(now, datetime):
            raise InvalidClockError("Clock provider must return a datetime instance")
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            raise InvalidClockError("Clock provider returned naive datetime; timezone-aware required")
        return now

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                str(self._db_path),
                timeout=self._busy_timeout_ms / 1000.0,
                isolation_level=None,  # Autocommit mode; explicit BEGIN IMMEDIATE used
            )
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms};")
            return conn
        except sqlite3.Error as exc:
            raise CoordinationStorageError(f"Failed to open SQLite database at '{self._db_path}'") from exc

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
            existing_tables = {row[0] for row in cursor.fetchall()}

            if not existing_tables:
                # NEW STORE: Transactional atomic initialization
                conn.execute("BEGIN IMMEDIATE;")
                for stmt in INIT_SCHEMA_SQL_STATEMENTS:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?);",
                    (SQLITE_SCHEMA_VERSION,),
                )
                conn.execute("COMMIT;")
                return

            # EXISTING STORE: Validate structure, primary keys, and schema version BEFORE ANY MUTATION
            if "meta" not in existing_tables:
                raise CoordinationStorageError("Existing database is missing required 'meta' table")

            if not EXPECTED_TABLES.issubset(existing_tables):
                raise CoordinationStorageError("Existing database is missing required AOS tables")

            # Check column structure and primary key contract
            for tbl, req_cols in EXPECTED_COLUMNS.items():
                cursor = conn.execute(f"PRAGMA table_info({tbl});")
                table_info = cursor.fetchall()
                # table_info row: (cid, name, type, notnull, dflt_value, pk)
                cols = {row[1] for row in table_info}
                if not req_cols.issubset(cols):
                    raise CoordinationStorageError(f"Existing table '{tbl}' missing required columns")

                pk_cols = sorted(
                    [(row[1], row[5]) for row in table_info if row[5] > 0],
                    key=lambda x: x[1],
                )
                expected_pks = EXPECTED_PRIMARY_KEYS.get(tbl, [])
                if pk_cols != expected_pks:
                    raise CoordinationStorageError(
                        f"Existing table '{tbl}' primary key structure {pk_cols} does not match expected {expected_pks}"
                    )

            # Check schema version
            cursor = conn.execute("SELECT value FROM meta WHERE key = 'schema_version';")
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise CoordinationStorageError("Existing database meta table does not contain exactly one schema_version")

            current_ver = rows[0][0]
            if current_ver != SQLITE_SCHEMA_VERSION:
                raise CoordinationStorageError(
                    f"Unsupported SQLite coordination schema version '{current_ver}'; expected '{SQLITE_SCHEMA_VERSION}'"
                )

        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise CoordinationStorageError(f"Database error during database initialization: {exc}") from exc
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def register_worker(self, identity: WorkerIdentity) -> None:
        if not isinstance(identity, WorkerIdentity):
            raise InvalidIdentityError("identity must be a WorkerIdentity instance")

        now_str = _datetime_to_iso(self._now())
        tags_json = json.dumps(sorted(list(identity.capability_tags)))

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cursor = conn.execute(
                "SELECT worker_id, session_id, capability_tags_json, registered_at FROM workers WHERE worker_id = ? AND session_id = ?;",
                (identity.worker_id, identity.session_id),
            )
            row = cursor.fetchone()
            if row is not None:
                worker_row = _parse_worker_row(row)
                if worker_row.capability_tags == identity.capability_tags:
                    # Idempotent re-registration
                    conn.execute("COMMIT;")
                    return
                else:
                    conn.execute("ROLLBACK;")
                    raise InvalidIdentityError(
                        f"Contradictory identity registration for worker ({identity.worker_id}, {identity.session_id})"
                    )

            conn.execute(
                "INSERT INTO workers (worker_id, session_id, capability_tags_json, registered_at) VALUES (?, ?, ?, ?);",
                (identity.worker_id, identity.session_id, tags_json, now_str),
            )
            conn.execute("COMMIT;")
        except (InvalidIdentityError, InvalidInputError, InvalidClockError, CoordinationStorageError):
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise CoordinationStorageError(f"Database error during register_worker: {exc}") from exc
        finally:
            conn.close()

    def is_worker_registered(self, worker_id: str, session_id: str) -> bool:
        if not isinstance(worker_id, str) or not isinstance(session_id, str):
            return False

        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT worker_id, session_id, capability_tags_json, registered_at FROM workers WHERE worker_id = ? AND session_id = ?;",
                (worker_id.strip(), session_id.strip()),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            _parse_worker_row(row)
            return True
        except (CoordinationStorageError, InvalidClockError):
            raise
        except sqlite3.Error as exc:
            raise CoordinationStorageError(f"Database error during is_worker_registered: {exc}") from exc
        finally:
            conn.close()

    def _parse_lease_row(self, row: tuple) -> LeaseSnapshot:
        if len(row) != 10:
            raise CoordinationStorageError("Corrupt lease row: incorrect column count")

        (
            task_id,
            worker_id,
            session_id,
            lease_id,
            acquired_at_str,
            last_hb_str,
            expires_at_str,
            ttl_sec,
            gen,
            status_str,
        ) = row

        if not isinstance(task_id, str) or not task_id.strip():
            raise CoordinationStorageError("Corrupt task_id in lease row")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise CoordinationStorageError("Corrupt worker_id in lease row")
        if not isinstance(session_id, str) or not session_id.strip():
            raise CoordinationStorageError("Corrupt session_id in lease row")
        if not isinstance(lease_id, str) or not lease_id.strip():
            raise CoordinationStorageError("Corrupt lease_id in lease row")

        acquired_at = _iso_to_datetime(acquired_at_str)
        last_hb = _iso_to_datetime(last_hb_str)
        expires_at = _iso_to_datetime(expires_at_str)

        if acquired_at > last_hb:
            raise CoordinationStorageError("Corrupt lease row: acquired_at > last_heartbeat_at")
        if last_hb >= expires_at:
            raise CoordinationStorageError("Corrupt lease row: last_heartbeat_at >= expires_at")

        if isinstance(ttl_sec, bool) or not isinstance(ttl_sec, (int, float)) or math.isnan(ttl_sec) or math.isinf(ttl_sec) or ttl_sec <= 0:
            raise CoordinationStorageError(f"Corrupt ttl_seconds '{ttl_sec}' in lease row")

        if isinstance(gen, bool) or not isinstance(gen, int) or gen <= 0:
            raise CoordinationStorageError(f"Corrupt generation '{gen}' in lease row")

        try:
            status = LeaseStatus(status_str)
        except ValueError as exc:
            raise CoordinationStorageError(f"Corrupt status '{status_str}' in lease row") from exc

        return LeaseSnapshot(
            task_id=task_id,
            worker_id=worker_id,
            session_id=session_id,
            lease_id=lease_id,
            acquired_at=acquired_at,
            last_heartbeat_at=last_hb,
            expires_at=expires_at,
            ttl_seconds=float(ttl_sec),
            generation=gen,
            status=status,
        )

    def get_lease(self, task_id: str) -> Optional[LeaseSnapshot]:
        if not isinstance(task_id, str) or not task_id.strip():
            return None

        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT task_id, worker_id, session_id, lease_id, acquired_at, last_heartbeat_at, expires_at, ttl_seconds, generation, status FROM leases WHERE task_id = ?;",
                (task_id.strip(),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._parse_lease_row(row)
        except (CoordinationStorageError, InvalidClockError):
            raise
        except sqlite3.Error as exc:
            raise CoordinationStorageError(f"Database error during get_lease: {exc}") from exc
        finally:
            conn.close()

    def try_claim(self, task_id: str, identity: WorkerIdentity, ttl_seconds: float) -> ClaimResult:
        if not isinstance(task_id, str) or not task_id.strip():
            raise InvalidInputError("task_id must be a non-empty string")
        if not isinstance(identity, WorkerIdentity):
            raise InvalidIdentityError("identity must be a WorkerIdentity instance")

        valid_ttl = _validate_ttl(ttl_seconds)
        task_id = task_id.strip()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")

            # Check registration
            cursor = conn.execute(
                "SELECT worker_id, session_id, capability_tags_json, registered_at FROM workers WHERE worker_id = ? AND session_id = ?;",
                (identity.worker_id, identity.session_id),
            )
            row = cursor.fetchone()
            if row is None:
                conn.execute("ROLLBACK;")
                raise InvalidIdentityError("Worker identity must be registered prior to claiming tasks")

            worker_row = _parse_worker_row(row)
            if worker_row.capability_tags != identity.capability_tags:
                conn.execute("ROLLBACK;")
                raise InvalidIdentityError("Worker identity tags do not match registered identity")

            now = self._now()
            expires_at = _compute_safe_expiry(now, valid_ttl)
            now_str = _datetime_to_iso(now)
            exp_str = _datetime_to_iso(expires_at)

            # Inspect current lease
            cursor = conn.execute(
                "SELECT task_id, worker_id, session_id, lease_id, acquired_at, last_heartbeat_at, expires_at, ttl_seconds, generation, status FROM leases WHERE task_id = ?;",
                (task_id,),
            )
            lease_row = cursor.fetchone()

            current_gen = 0
            if lease_row is not None:
                current_lease = self._parse_lease_row(lease_row)
                current_gen = current_lease.generation
                is_expired = now >= current_lease.expires_at

                if current_lease.status == LeaseStatus.ACTIVE and not is_expired:
                    if (
                        current_lease.worker_id == identity.worker_id
                        and current_lease.session_id == identity.session_id
                    ):
                        conn.execute("COMMIT;")
                        return ClaimResult(
                            disposition=ClaimDisposition.ALREADY_OWNED,
                            lease=current_lease,
                        )
                    else:
                        conn.execute("COMMIT;")
                        return ClaimResult(
                            disposition=ClaimDisposition.HELD_BY_OTHER,
                            lease=current_lease,
                        )

            # Acquire new lease epoch
            next_gen = current_gen + 1
            cursor = conn.execute("SELECT MAX(rowid) FROM leases;")
            max_row = cursor.fetchone()[0] or 0
            new_lease_id = f"lease-{task_id}-{next_gen}-{max_row + 1}"

            new_lease = LeaseSnapshot(
                task_id=task_id,
                worker_id=identity.worker_id,
                session_id=identity.session_id,
                lease_id=new_lease_id,
                acquired_at=now,
                last_heartbeat_at=now,
                expires_at=expires_at,
                ttl_seconds=valid_ttl,
                generation=next_gen,
                status=LeaseStatus.ACTIVE,
            )

            conn.execute(
                """
                INSERT INTO leases (task_id, worker_id, session_id, lease_id, acquired_at, last_heartbeat_at, expires_at, ttl_seconds, generation, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    worker_id=excluded.worker_id,
                    session_id=excluded.session_id,
                    lease_id=excluded.lease_id,
                    acquired_at=excluded.acquired_at,
                    last_heartbeat_at=excluded.last_heartbeat_at,
                    expires_at=excluded.expires_at,
                    ttl_seconds=excluded.ttl_seconds,
                    generation=excluded.generation,
                    status=excluded.status;
                """,
                (
                    task_id,
                    identity.worker_id,
                    identity.session_id,
                    new_lease_id,
                    now_str,
                    now_str,
                    exp_str,
                    valid_ttl,
                    next_gen,
                    LeaseStatus.ACTIVE.value,
                ),
            )

            conn.execute("COMMIT;")
            return ClaimResult(
                disposition=ClaimDisposition.ACQUIRED,
                lease=new_lease,
            )
        except (InvalidIdentityError, InvalidInputError, InvalidClockError, CoordinationStorageError):
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise CoordinationStorageError(f"Database error during try_claim: {exc}") from exc
        finally:
            conn.close()

    def heartbeat(
        self,
        task_id: str,
        worker_id: str,
        session_id: str,
        lease_id: str,
        generation: int,
    ) -> Optional[LeaseSnapshot]:
        if not isinstance(task_id, str) or not task_id.strip():
            raise InvalidInputError("task_id must be a non-empty string")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise InvalidInputError("worker_id must be a non-empty string")
        if not isinstance(session_id, str) or not session_id.strip():
            raise InvalidInputError("session_id must be a non-empty string")
        if not isinstance(lease_id, str) or not lease_id.strip():
            raise InvalidInputError("lease_id must be a non-empty string")

        valid_gen = _validate_generation(generation)
        task_id = task_id.strip()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cursor = conn.execute(
                "SELECT task_id, worker_id, session_id, lease_id, acquired_at, last_heartbeat_at, expires_at, ttl_seconds, generation, status FROM leases WHERE task_id = ?;",
                (task_id,),
            )
            row = cursor.fetchone()
            if row is None:
                conn.execute("COMMIT;")
                return None

            current = self._parse_lease_row(row)

            if (
                current.worker_id != worker_id.strip()
                or current.session_id != session_id.strip()
                or current.lease_id != lease_id.strip()
                or current.generation != valid_gen
                or current.status != LeaseStatus.ACTIVE
            ):
                conn.execute("COMMIT;")
                return None

            now = self._now()
            if now >= current.expires_at:
                conn.execute(
                    "UPDATE leases SET status = ? WHERE task_id = ?;",
                    (LeaseStatus.EXPIRED.value, task_id),
                )
                conn.execute("COMMIT;")
                return None

            new_expires_at = _compute_safe_expiry(now, current.ttl_seconds)
            now_str = _datetime_to_iso(now)
            exp_str = _datetime_to_iso(new_expires_at)

            conn.execute(
                "UPDATE leases SET last_heartbeat_at = ?, expires_at = ? WHERE task_id = ?;",
                (now_str, exp_str, task_id),
            )
            conn.execute("COMMIT;")

            return LeaseSnapshot(
                task_id=current.task_id,
                worker_id=current.worker_id,
                session_id=current.session_id,
                lease_id=current.lease_id,
                acquired_at=current.acquired_at,
                last_heartbeat_at=now,
                expires_at=new_expires_at,
                ttl_seconds=current.ttl_seconds,
                generation=current.generation,
                status=LeaseStatus.ACTIVE,
            )
        except (InvalidInputError, InvalidClockError, CoordinationStorageError):
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise CoordinationStorageError(f"Database error during heartbeat: {exc}") from exc
        finally:
            conn.close()

    def release(
        self,
        task_id: str,
        worker_id: str,
        session_id: str,
        lease_id: str,
        generation: int,
    ) -> bool:
        if not isinstance(task_id, str) or not task_id.strip():
            raise InvalidInputError("task_id must be a non-empty string")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise InvalidInputError("worker_id must be a non-empty string")
        if not isinstance(session_id, str) or not session_id.strip():
            raise InvalidInputError("session_id must be a non-empty string")
        if not isinstance(lease_id, str) or not lease_id.strip():
            raise InvalidInputError("lease_id must be a non-empty string")

        valid_gen = _validate_generation(generation)
        task_id = task_id.strip()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cursor = conn.execute(
                "SELECT task_id, worker_id, session_id, lease_id, acquired_at, last_heartbeat_at, expires_at, ttl_seconds, generation, status FROM leases WHERE task_id = ?;",
                (task_id,),
            )
            row = cursor.fetchone()
            if row is None:
                conn.execute("COMMIT;")
                return False

            current = self._parse_lease_row(row)

            if (
                current.worker_id != worker_id.strip()
                or current.session_id != session_id.strip()
                or current.lease_id != lease_id.strip()
                or current.generation != valid_gen
                or current.status != LeaseStatus.ACTIVE
            ):
                conn.execute("COMMIT;")
                return False

            now = self._now()
            if now >= current.expires_at:
                conn.execute(
                    "UPDATE leases SET status = ? WHERE task_id = ?;",
                    (LeaseStatus.EXPIRED.value, task_id),
                )
                conn.execute("COMMIT;")
                return False

            conn.execute(
                "UPDATE leases SET status = ? WHERE task_id = ?;",
                (LeaseStatus.RELEASED.value, task_id),
            )
            conn.execute("COMMIT;")
            return True
        except (InvalidInputError, InvalidClockError, CoordinationStorageError):
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise CoordinationStorageError(f"Database error during release: {exc}") from exc
        finally:
            conn.close()
