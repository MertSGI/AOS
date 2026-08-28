"""AOS-5 Distributed Multi-PC Coordination — Stage 11B SQLite Coordination Backend.

Durable local SQLite reference implementation of CoordinationBackend:
- Versioned SQLite storage (schema version 0.1.0)
- Structural compatibility with CoordinationBackend
- Transactional cross-instance atomic claim (BEGIN IMMEDIATE / COMMIT / ROLLBACK)
- Explicit database path required
- Restart persistence (registered workers, leases, fencing generations)
- Non-canonical coordination store isolation (zero Git/project mutation)
- Fail-closed storage corruption handling
- SQL parameter binding for caller identifiers
"""

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading
from typing import Callable, Optional, Union

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

INIT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    capability_tags_json TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (worker_id, session_id)
);

CREATE TABLE IF NOT EXISTS leases (
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
"""


def _iso_to_datetime(val_str: str) -> datetime:
    if not isinstance(val_str, str) or not val_str.strip():
        raise CoordinationStorageError("Persisted datetime must be a non-empty string")
    try:
        dt = datetime.fromisoformat(val_str.strip())
    except (ValueError, TypeError) as exc:
        raise CoordinationStorageError(f"Persisted timestamp '{val_str}' is malformed") from exc

    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise CoordinationStorageError(f"Persisted timestamp '{val_str}' is naive; timezone-aware required")

    return dt.astimezone(timezone.utc)


def _datetime_to_iso(dt: datetime) -> str:
    if not isinstance(dt, datetime):
        raise InvalidClockError("Clock provider must return a datetime instance")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise InvalidClockError("Clock provider returned naive datetime; timezone-aware required")
    return dt.astimezone(timezone.utc).isoformat()


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

        self._db_path = Path(db_path).resolve()
        self._clock_provider = clock_provider or default_utc_clock
        self._busy_timeout_ms = busy_timeout_ms

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
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=self._busy_timeout_ms / 1000.0,
            isolation_level=None,  # Autocommit mode; explicit BEGIN IMMEDIATE used
        )
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms};")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(INIT_SCHEMA_SQL)
            conn.execute("BEGIN IMMEDIATE;")
            cursor = conn.execute("SELECT value FROM meta WHERE key = 'schema_version';")
            row = cursor.fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?);",
                    (SQLITE_SCHEMA_VERSION,),
                )
            else:
                current_ver = row[0]
                if current_ver != SQLITE_SCHEMA_VERSION:
                    conn.execute("ROLLBACK;")
                    raise CoordinationStorageError(
                        f"Unsupported SQLite coordination schema version '{current_ver}'; expected '{SQLITE_SCHEMA_VERSION}'"
                    )

            conn.execute("COMMIT;")
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
                "SELECT capability_tags_json FROM workers WHERE worker_id = ? AND session_id = ?;",
                (identity.worker_id, identity.session_id),
            )
            row = cursor.fetchone()
            if row is not None:
                try:
                    existing_tags = set(json.loads(row[0]))
                except Exception as exc:
                    conn.execute("ROLLBACK;")
                    raise CoordinationStorageError("Corrupt capability_tags_json in database") from exc

                if existing_tags == set(identity.capability_tags):
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
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def is_worker_registered(self, worker_id: str, session_id: str) -> bool:
        if not isinstance(worker_id, str) or not isinstance(session_id, str):
            return False

        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT 1 FROM workers WHERE worker_id = ? AND session_id = ?;",
                (worker_id.strip(), session_id.strip()),
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()

    def _parse_lease_row(self, row: tuple) -> LeaseSnapshot:
        # task_id, worker_id, session_id, lease_id, acquired_at, last_heartbeat_at, expires_at, ttl_seconds, generation, status
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

        if not isinstance(task_id, str) or not task_id:
            raise CoordinationStorageError("Corrupt task_id in lease row")
        if not isinstance(worker_id, str) or not worker_id:
            raise CoordinationStorageError("Corrupt worker_id in lease row")
        if not isinstance(session_id, str) or not session_id:
            raise CoordinationStorageError("Corrupt session_id in lease row")
        if not isinstance(lease_id, str) or not lease_id:
            raise CoordinationStorageError("Corrupt lease_id in lease row")

        acquired_at = _iso_to_datetime(acquired_at_str)
        last_hb = _iso_to_datetime(last_hb_str)
        expires_at = _iso_to_datetime(expires_at_str)

        if not isinstance(ttl_sec, (int, float)) or math.isnan(ttl_sec) or math.isinf(ttl_sec) or ttl_sec <= 0:
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
                "SELECT capability_tags_json FROM workers WHERE worker_id = ? AND session_id = ?;",
                (identity.worker_id, identity.session_id),
            )
            row = cursor.fetchone()
            if row is None:
                conn.execute("ROLLBACK;")
                raise InvalidIdentityError("Worker identity must be registered prior to claiming tasks")

            try:
                registered_tags = set(json.loads(row[0]))
            except Exception as exc:
                conn.execute("ROLLBACK;")
                raise CoordinationStorageError("Corrupt capability_tags_json in database") from exc

            if registered_tags != set(identity.capability_tags):
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
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
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
                # Mark EXPIRED
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
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
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
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            conn.close()
