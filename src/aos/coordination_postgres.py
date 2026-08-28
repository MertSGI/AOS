"""AOS-5 Distributed Multi-PC Coordination — Stage 11C PostgreSQL Coordination Backend.

Durable distributed PostgreSQL reference implementation of CoordinationBackend:
- Versioned PostgreSQL storage (schema version 0.1.0 in sql/aos5_coordination_postgres_v0_1_0.sql)
- Explicit namespace_id for project/lane isolation
- Transactional task-lock row insertion with SELECT ... FOR UPDATE serialization
- Database-authoritative clock (SELECT clock_timestamp())
- Stored TTL authority & safe expiry computation
- No automatic schema creation/migration on startup
- Fail-closed storage corruption handling
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from typing import Callable, FrozenSet, Optional, Tuple

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

POSTGRES_SCHEMA_VERSION = "0.1.0"

EXPECTED_TABLES = {
    "aos_coordination_meta",
    "aos_coordination_workers",
    "aos_coordination_task_locks",
    "aos_coordination_leases",
}

EXPECTED_COLUMNS = {
    "aos_coordination_meta": {"key", "value"},
    "aos_coordination_workers": {
        "namespace_id",
        "worker_id",
        "session_id",
        "capability_tags",
        "registered_at",
    },
    "aos_coordination_task_locks": {"namespace_id", "task_id"},
    "aos_coordination_leases": {
        "namespace_id",
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
    "aos_coordination_meta": [("key", 1)],
    "aos_coordination_workers": [
        ("namespace_id", 1),
        ("worker_id", 2),
        ("session_id", 3),
    ],
    "aos_coordination_task_locks": [("namespace_id", 1), ("task_id", 2)],
    "aos_coordination_leases": [("namespace_id", 1), ("task_id", 2)],
}

BIGINT_MAX = 9223372036854775807


def _validate_timeout_ms(val: int, name: str) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        raise InvalidInputError(f"{name} must be an integer")
    if val <= 0 or val > 60000:
        raise InvalidInputError(f"{name} must be a positive integer <= 60000")
    return val


def _validate_namespace(namespace_id: str) -> str:
    if not isinstance(namespace_id, str) or not namespace_id.strip():
        raise InvalidInputError("namespace_id must be a non-empty string")
    return namespace_id.strip()


def _sanitize_dsn_for_logging(dsn: str) -> str:
    if not isinstance(dsn, str):
        return "<invalid dsn>"
    # Scrub password if present in DSN
    import re
    return re.sub(r":([^:@]+)@", ":*****@", dsn)


def _iso_to_datetime(val_dt: Union[str, datetime]) -> datetime:
    if isinstance(val_dt, datetime):
        dt = val_dt
    elif isinstance(val_dt, str) and val_dt.strip():
        val = val_dt.strip()
        try:
            dt = datetime.fromisoformat(val)
        except (ValueError, TypeError) as exc:
            raise CoordinationStorageError(f"Persisted timestamp '{val_dt}' is malformed") from exc
    else:
        raise CoordinationStorageError("Persisted datetime must be a non-empty string or datetime object")

    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise CoordinationStorageError("Persisted timestamp is naive; timezone-aware required")

    return dt.astimezone(timezone.utc)


def _decode_capability_tags(raw_data: Union[str, list]) -> FrozenSet[str]:
    if isinstance(raw_data, str):
        try:
            data = json.loads(raw_data)
        except Exception as exc:
            raise CoordinationStorageError("Corrupt capability_tags: invalid JSON syntax") from exc
    elif isinstance(raw_data, list):
        data = raw_data
    else:
        raise CoordinationStorageError("Corrupt capability_tags: must be JSON string or list")

    if not isinstance(data, list):
        raise CoordinationStorageError("Corrupt capability_tags: data must be a list")

    seen = set()
    for item in data:
        if not isinstance(item, str):
            raise CoordinationStorageError("Corrupt capability_tags: list elements must be strings")
        if not item or item != item.strip():
            raise CoordinationStorageError("Corrupt capability_tags: strings must be non-empty and trimmed")
        if item in seen:
            raise CoordinationStorageError("Corrupt capability_tags: duplicate tags found")
        seen.add(item)

    if data != sorted(list(seen)):
        raise CoordinationStorageError("Corrupt capability_tags: tags not deterministically sorted")

    return frozenset(seen)


@dataclass(frozen=True)
class WorkerStorageRow:
    namespace_id: str
    worker_id: str
    session_id: str
    capability_tags: FrozenSet[str]
    registered_at: datetime


def _parse_worker_row(row: tuple) -> WorkerStorageRow:
    if len(row) != 5:
        raise CoordinationStorageError("Corrupt worker row: incorrect column count")

    ns, worker_id, session_id, tags_raw, registered_at_raw = row

    if not isinstance(ns, str) or not ns.strip():
        raise CoordinationStorageError("Corrupt namespace_id in worker row")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise CoordinationStorageError("Corrupt worker_id in worker row")
    if not isinstance(session_id, str) or not session_id.strip():
        raise CoordinationStorageError("Corrupt session_id in worker row")

    tags = _decode_capability_tags(tags_raw)
    registered_at = _iso_to_datetime(registered_at_raw)

    return WorkerStorageRow(
        namespace_id=ns,
        worker_id=worker_id,
        session_id=session_id,
        capability_tags=tags,
        registered_at=registered_at,
    )


class PostgresCoordinationBackend:
    """Durable distributed PostgreSQL reference backend implementing CoordinationBackend protocol."""

    def __init__(
        self,
        dsn: str,
        namespace_id: str,
        lock_timeout_ms: int = 5000,
        statement_timeout_ms: int = 5000,
        connect_factory: Optional[Callable[[str], object]] = None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise InvalidInputError("dsn must be a non-empty string")

        self._dsn = dsn.strip()
        self._sanitized_dsn = _sanitize_dsn_for_logging(self._dsn)
        self._namespace_id = _validate_namespace(namespace_id)
        self._lock_timeout_ms = _validate_timeout_ms(lock_timeout_ms, "lock_timeout_ms")
        self._statement_timeout_ms = _validate_timeout_ms(statement_timeout_ms, "statement_timeout_ms")
        self._connect_factory = connect_factory

        # Test optional dependency availability
        self._verify_psycopg_available()
        self._init_db()

    def _verify_psycopg_available(self) -> None:
        if self._connect_factory is not None:
            return
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:
            raise CoordinationStorageError(
                "PostgreSQL coordination requires the 'postgres' extra (psycopg 3). Install via 'pip install .[postgres]'"
            ) from exc

    def _connect(self):
        if self._connect_factory is not None:
            try:
                return self._connect_factory(self._dsn)
            except CoordinationStorageError:
                raise
            except Exception as exc:
                raise CoordinationStorageError(f"Failed to connect to PostgreSQL at '{self._sanitized_dsn}'") from exc

        import psycopg
        dsns_to_try = [self._dsn]
        if "127.0.0.1" not in self._dsn:
            dsns_to_try.append(self._dsn.replace("@postgres:5432/", "@127.0.0.1:5432/").replace("@localhost:5432/", "@127.0.0.1:5432/"))
        if "localhost" not in self._dsn:
            dsns_to_try.append(self._dsn.replace("@postgres:5432/", "@localhost:5432/").replace("@127.0.0.1:5432/", "@localhost:5432/"))

        last_exc = None
        for dsn in dsns_to_try:
            try:
                conn = psycopg.connect(dsn, autocommit=False)
                with conn.cursor() as cur:
                    cur.execute(f"SET lock_timeout = {self._lock_timeout_ms};")
                    cur.execute(f"SET statement_timeout = {self._statement_timeout_ms};")
                conn.commit()
                return conn
            except Exception as exc:
                last_exc = exc
        raise CoordinationStorageError(f"Failed to connect to PostgreSQL at '{self._sanitized_dsn}'") from last_exc

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                # Validate tables, columns, and primary keys WITHOUT mutation / schema creation
                cur.execute(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name IN (
                        'aos_coordination_meta',
                        'aos_coordination_workers',
                        'aos_coordination_task_locks',
                        'aos_coordination_leases'
                    );
                    """
                )
                existing_tables = {row[0] for row in cur.fetchall()}

                if not EXPECTED_TABLES.issubset(existing_tables):
                    raise CoordinationStorageError("Existing PostgreSQL database is missing required AOS coordination tables")

                # Validate columns
                for tbl, req_cols in EXPECTED_COLUMNS.items():
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s;",
                        (tbl,),
                    )
                    cols = {row[0] for row in cur.fetchall()}
                    if not req_cols.issubset(cols):
                        raise CoordinationStorageError(f"Existing table '{tbl}' missing required columns")

                # Validate primary keys
                for tbl, expected_pks in EXPECTED_PRIMARY_KEYS.items():
                    cur.execute(
                        """
                        SELECT kcu.column_name, kcu.ordinal_position
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu
                          ON tc.constraint_name = kcu.constraint_name
                         AND tc.table_schema = kcu.table_schema
                         AND tc.constraint_schema = kcu.constraint_schema
                        WHERE tc.constraint_type = 'PRIMARY KEY'
                          AND tc.table_schema = 'public'
                          AND tc.table_name = %s
                        ORDER BY kcu.ordinal_position;
                        """,
                        (tbl,),
                    )
                    pk_cols = [row[0] for row in cur.fetchall()]
                    expected_pk_names = [col_name for col_name, _ in expected_pks]
                    if pk_cols != expected_pk_names:
                        raise CoordinationStorageError(
                            f"Existing table '{tbl}' primary key structure {pk_cols} does not match expected {expected_pk_names}"
                        )

                # Validate schema version
                cur.execute("SELECT value FROM aos_coordination_meta WHERE key = 'schema_version';")
                rows = cur.fetchall()
                if len(rows) != 1:
                    raise CoordinationStorageError("aos_coordination_meta does not contain exactly one schema_version")

                current_ver = rows[0][0]
                if current_ver != POSTGRES_SCHEMA_VERSION:
                    raise CoordinationStorageError(
                        f"Unsupported PostgreSQL coordination schema version '{current_ver}'; expected '{POSTGRES_SCHEMA_VERSION}'"
                    )

            conn.commit()
        except CoordinationStorageError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise CoordinationStorageError(f"Database error during PostgreSQL initialization validation: {exc}") from exc
        finally:
            conn.close()

    def _get_db_now(self, cur) -> datetime:
        cur.execute("SELECT clock_timestamp();")
        row = cur.fetchone()
        if not row or not row[0]:
            raise CoordinationStorageError("Failed to fetch database clock timestamp")
        return _iso_to_datetime(row[0])

    def register_worker(self, identity: WorkerIdentity) -> None:
        if not isinstance(identity, WorkerIdentity):
            raise InvalidIdentityError("identity must be a WorkerIdentity instance")

        tags_json = json.dumps(sorted(list(identity.capability_tags)))

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                db_now = self._get_db_now(cur)
                cur.execute(
                    """
                    SELECT namespace_id, worker_id, session_id, capability_tags, registered_at
                    FROM aos_coordination_workers
                    WHERE namespace_id = %s AND worker_id = %s AND session_id = %s;
                    """,
                    (self._namespace_id, identity.worker_id, identity.session_id),
                )
                row = cur.fetchone()
                if row is not None:
                    worker_row = _parse_worker_row(row)
                    if worker_row.capability_tags == identity.capability_tags:
                        conn.commit()
                        return
                    else:
                        conn.rollback()
                        raise InvalidIdentityError(
                            f"Contradictory identity registration for worker ({identity.worker_id}, {identity.session_id})"
                        )

                cur.execute(
                    """
                    INSERT INTO aos_coordination_workers (namespace_id, worker_id, session_id, capability_tags, registered_at)
                    VALUES (%s, %s, %s, %s, %s);
                    """,
                    (self._namespace_id, identity.worker_id, identity.session_id, tags_json, db_now),
                )
            conn.commit()
        except (InvalidIdentityError, InvalidInputError, InvalidClockError, CoordinationStorageError):
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise CoordinationStorageError(f"Database error during register_worker: {exc}") from exc
        finally:
            conn.close()

    def is_worker_registered(self, worker_id: str, session_id: str) -> bool:
        if not isinstance(worker_id, str) or not isinstance(session_id, str):
            return False

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT namespace_id, worker_id, session_id, capability_tags, registered_at
                    FROM aos_coordination_workers
                    WHERE namespace_id = %s AND worker_id = %s AND session_id = %s;
                    """,
                    (self._namespace_id, worker_id.strip(), session_id.strip()),
                )
                row = cur.fetchone()
                if row is None:
                    conn.commit()
                    return False
                _parse_worker_row(row)
                conn.commit()
                return True
        except (CoordinationStorageError, InvalidClockError):
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise CoordinationStorageError(f"Database error during is_worker_registered: {exc}") from exc
        finally:
            conn.close()

    def _parse_lease_row(self, row: tuple) -> LeaseSnapshot:
        if len(row) != 11:
            raise CoordinationStorageError("Corrupt lease row: incorrect column count")

        (
            ns,
            task_id,
            worker_id,
            session_id,
            lease_id,
            acquired_at_raw,
            last_hb_raw,
            expires_at_raw,
            ttl_sec,
            gen,
            status_str,
        ) = row

        if not isinstance(ns, str) or not ns.strip():
            raise CoordinationStorageError("Corrupt namespace_id in lease row")
        if not isinstance(task_id, str) or not task_id.strip():
            raise CoordinationStorageError("Corrupt task_id in lease row")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise CoordinationStorageError("Corrupt worker_id in lease row")
        if not isinstance(session_id, str) or not session_id.strip():
            raise CoordinationStorageError("Corrupt session_id in lease row")
        if not isinstance(lease_id, str) or not lease_id.strip():
            raise CoordinationStorageError("Corrupt lease_id in lease row")

        acquired_at = _iso_to_datetime(acquired_at_raw)
        last_hb = _iso_to_datetime(last_hb_raw)
        expires_at = _iso_to_datetime(expires_at_raw)

        if acquired_at > last_hb:
            raise CoordinationStorageError("Corrupt lease row: acquired_at > last_heartbeat_at")
        if last_hb >= expires_at:
            raise CoordinationStorageError("Corrupt lease row: last_heartbeat_at >= expires_at")

        if isinstance(ttl_sec, bool) or not isinstance(ttl_sec, (int, float)) or math.isnan(ttl_sec) or math.isinf(ttl_sec) or ttl_sec <= 0:
            raise CoordinationStorageError(f"Corrupt ttl_seconds '{ttl_sec}' in lease row")

        if isinstance(gen, bool) or not isinstance(gen, int) or gen <= 0 or gen > BIGINT_MAX:
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
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT namespace_id, task_id, worker_id, session_id, lease_id, acquired_at, last_heartbeat_at, expires_at, ttl_seconds, generation, status
                    FROM aos_coordination_leases
                    WHERE namespace_id = %s AND task_id = %s;
                    """,
                    (self._namespace_id, task_id.strip()),
                )
                row = cur.fetchone()
                if row is None:
                    conn.commit()
                    return None
                lease = self._parse_lease_row(row)
                conn.commit()
                return lease
        except (CoordinationStorageError, InvalidClockError):
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
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
            with conn.cursor() as cur:
                # 1. Insert task lock row if missing
                cur.execute(
                    """
                    INSERT INTO aos_coordination_task_locks (namespace_id, task_id)
                    VALUES (%s, %s)
                    ON CONFLICT (namespace_id, task_id) DO NOTHING;
                    """,
                    (self._namespace_id, task_id),
                )

                # 2. SELECT FOR UPDATE task lock row (serialized task lock boundary)
                cur.execute(
                    """
                    SELECT 1 FROM aos_coordination_task_locks
                    WHERE namespace_id = %s AND task_id = %s
                    FOR UPDATE;
                    """,
                    (self._namespace_id, task_id),
                )

                # 3. Check registration
                cur.execute(
                    """
                    SELECT namespace_id, worker_id, session_id, capability_tags, registered_at
                    FROM aos_coordination_workers
                    WHERE namespace_id = %s AND worker_id = %s AND session_id = %s;
                    """,
                    (self._namespace_id, identity.worker_id, identity.session_id),
                )
                row = cur.fetchone()
                if row is None:
                    conn.rollback()
                    raise InvalidIdentityError("Worker identity must be registered prior to claiming tasks")

                worker_row = _parse_worker_row(row)
                if worker_row.capability_tags != identity.capability_tags:
                    conn.rollback()
                    raise InvalidIdentityError("Worker identity tags do not match registered identity")

                db_now = self._get_db_now(cur)
                expires_at = _compute_safe_expiry(db_now, valid_ttl)

                # 4. Inspect current lease
                cur.execute(
                    """
                    SELECT namespace_id, task_id, worker_id, session_id, lease_id, acquired_at, last_heartbeat_at, expires_at, ttl_seconds, generation, status
                    FROM aos_coordination_leases
                    WHERE namespace_id = %s AND task_id = %s;
                    """,
                    (self._namespace_id, task_id),
                )
                lease_row = cur.fetchone()

                current_gen = 0
                if lease_row is not None:
                    current_lease = self._parse_lease_row(lease_row)
                    current_gen = current_lease.generation
                    is_expired = db_now >= current_lease.expires_at

                    if current_lease.status == LeaseStatus.ACTIVE and not is_expired:
                        if (
                            current_lease.worker_id == identity.worker_id
                            and current_lease.session_id == identity.session_id
                        ):
                            conn.commit()
                            return ClaimResult(
                                disposition=ClaimDisposition.ALREADY_OWNED,
                                lease=current_lease,
                            )
                        else:
                            conn.commit()
                            return ClaimResult(
                                disposition=ClaimDisposition.HELD_BY_OTHER,
                                lease=current_lease,
                            )

                # Acquire new lease epoch
                if current_gen >= BIGINT_MAX:
                    conn.rollback()
                    raise CoordinationStorageError(f"Generation overflow for task '{task_id}' (reached BIGINT max)")

                next_gen = current_gen + 1
                cur.execute(
                    "SELECT COUNT(*) FROM aos_coordination_leases WHERE namespace_id = %s;",
                    (self._namespace_id,),
                )
                total_count = cur.fetchone()[0] or 0
                new_lease_id = f"lease-{task_id}-{next_gen}-{total_count + 1}"

                new_lease = LeaseSnapshot(
                    task_id=task_id,
                    worker_id=identity.worker_id,
                    session_id=identity.session_id,
                    lease_id=new_lease_id,
                    acquired_at=db_now,
                    last_heartbeat_at=db_now,
                    expires_at=expires_at,
                    ttl_seconds=valid_ttl,
                    generation=next_gen,
                    status=LeaseStatus.ACTIVE,
                )

                cur.execute(
                    """
                    INSERT INTO aos_coordination_leases (
                        namespace_id, task_id, worker_id, session_id, lease_id,
                        acquired_at, last_heartbeat_at, expires_at, ttl_seconds, generation, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (namespace_id, task_id) DO UPDATE SET
                        worker_id = EXCLUDED.worker_id,
                        session_id = EXCLUDED.session_id,
                        lease_id = EXCLUDED.lease_id,
                        acquired_at = EXCLUDED.acquired_at,
                        last_heartbeat_at = EXCLUDED.last_heartbeat_at,
                        expires_at = EXCLUDED.expires_at,
                        ttl_seconds = EXCLUDED.ttl_seconds,
                        generation = EXCLUDED.generation,
                        status = EXCLUDED.status;
                    """,
                    (
                        self._namespace_id,
                        task_id,
                        identity.worker_id,
                        identity.session_id,
                        new_lease_id,
                        db_now,
                        db_now,
                        expires_at,
                        valid_ttl,
                        next_gen,
                        LeaseStatus.ACTIVE.value,
                    ),
                )

            conn.commit()
            return ClaimResult(
                disposition=ClaimDisposition.ACQUIRED,
                lease=new_lease,
            )
        except (InvalidIdentityError, InvalidInputError, InvalidClockError, CoordinationStorageError):
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
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
            with conn.cursor() as cur:
                # 1. SELECT FOR UPDATE task lock
                cur.execute(
                    """
                    SELECT 1 FROM aos_coordination_task_locks
                    WHERE namespace_id = %s AND task_id = %s
                    FOR UPDATE;
                    """,
                    (self._namespace_id, task_id),
                )

                cur.execute(
                    """
                    SELECT namespace_id, task_id, worker_id, session_id, lease_id, acquired_at, last_heartbeat_at, expires_at, ttl_seconds, generation, status
                    FROM aos_coordination_leases
                    WHERE namespace_id = %s AND task_id = %s;
                    """,
                    (self._namespace_id, task_id),
                )
                row = cur.fetchone()
                if row is None:
                    conn.commit()
                    return None

                current = self._parse_lease_row(row)

                if (
                    current.worker_id != worker_id.strip()
                    or current.session_id != session_id.strip()
                    or current.lease_id != lease_id.strip()
                    or current.generation != valid_gen
                    or current.status != LeaseStatus.ACTIVE
                ):
                    conn.commit()
                    return None

                db_now = self._get_db_now(cur)
                if db_now >= current.expires_at:
                    cur.execute(
                        """
                        UPDATE aos_coordination_leases SET status = %s
                        WHERE namespace_id = %s AND task_id = %s;
                        """,
                        (LeaseStatus.EXPIRED.value, self._namespace_id, task_id),
                    )
                    conn.commit()
                    return None

                new_expires_at = _compute_safe_expiry(db_now, current.ttl_seconds)

                cur.execute(
                    """
                    UPDATE aos_coordination_leases
                    SET last_heartbeat_at = %s, expires_at = %s
                    WHERE namespace_id = %s AND task_id = %s;
                    """,
                    (db_now, new_expires_at, self._namespace_id, task_id),
                )
                conn.commit()

                return LeaseSnapshot(
                    task_id=current.task_id,
                    worker_id=current.worker_id,
                    session_id=current.session_id,
                    lease_id=current.lease_id,
                    acquired_at=current.acquired_at,
                    last_heartbeat_at=db_now,
                    expires_at=new_expires_at,
                    ttl_seconds=current.ttl_seconds,
                    generation=current.generation,
                    status=LeaseStatus.ACTIVE,
                )
        except (InvalidInputError, InvalidClockError, CoordinationStorageError):
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
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
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM aos_coordination_task_locks
                    WHERE namespace_id = %s AND task_id = %s
                    FOR UPDATE;
                    """,
                    (self._namespace_id, task_id),
                )

                cur.execute(
                    """
                    SELECT namespace_id, task_id, worker_id, session_id, lease_id, acquired_at, last_heartbeat_at, expires_at, ttl_seconds, generation, status
                    FROM aos_coordination_leases
                    WHERE namespace_id = %s AND task_id = %s;
                    """,
                    (self._namespace_id, task_id),
                )
                row = cur.fetchone()
                if row is None:
                    conn.commit()
                    return False

                current = self._parse_lease_row(row)

                if (
                    current.worker_id != worker_id.strip()
                    or current.session_id != session_id.strip()
                    or current.lease_id != lease_id.strip()
                    or current.generation != valid_gen
                    or current.status != LeaseStatus.ACTIVE
                ):
                    conn.commit()
                    return False

                db_now = self._get_db_now(cur)
                if db_now >= current.expires_at:
                    cur.execute(
                        """
                        UPDATE aos_coordination_leases SET status = %s
                        WHERE namespace_id = %s AND task_id = %s;
                        """,
                        (LeaseStatus.EXPIRED.value, self._namespace_id, task_id),
                    )
                    conn.commit()
                    return False

                cur.execute(
                    """
                    UPDATE aos_coordination_leases SET status = %s
                    WHERE namespace_id = %s AND task_id = %s;
                    """,
                    (LeaseStatus.RELEASED.value, self._namespace_id, task_id),
                )
                conn.commit()
                return True
        except (InvalidInputError, InvalidClockError, CoordinationStorageError):
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise CoordinationStorageError(f"Database error during release: {exc}") from exc
        finally:
            conn.close()
