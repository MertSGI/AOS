"""Tests for AOS-5 Stage 11B-R1 SQLite Durable Coordination Backend Integrity.

Covers all Stage 11B baseline requirements plus Stage 11B-R1 controller findings:
A. Unsupported schema non-mutation proof
B. Same-version missing table/column fail closed without silent repair
C. Capability tag decoder corruption matrix (object, scalar, non-string, adversarial)
D. Lease parser corruption matrix (TTL, generation, status, naive/non-UTC ISO, empty string, time order)
E. Non-SQLite existing file fails closed as CoordinationStorageError
F. Invalid busy_timeout_ms input validation
G. Concurrent winner exact-binding proof (two-instance and multi-contender)
H. Thread failure observability across concurrent test barriers
"""

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3
import threading
import pytest

from aos.coordination import (
    ClaimDisposition,
    CoordinationBackend,
    CoordinationStorageError,
    InvalidIdentityError,
    InvalidInputError,
    LeaseStatus,
    WorkerIdentity,
)
from aos.coordination_sqlite import SQLiteCoordinationBackend, SQLITE_SCHEMA_VERSION


class ControlledClock:

    def __init__(self, start_time: datetime = None) -> None:
        if start_time is None:
            self.current_time = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)
        else:
            self.current_time = start_time

    def now(self) -> datetime:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += timedelta(seconds=seconds)


# 1. schema initialization
def test_schema_initialization(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    backend = SQLiteCoordinationBackend(db_file)
    assert db_file.exists()

    conn = sqlite3.connect(str(db_file))
    cursor = conn.execute("SELECT value FROM meta WHERE key = 'schema_version';")
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == SQLITE_SCHEMA_VERSION
    conn.close()


# 2. compatible reopen
def test_compatible_reopen(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    backend1 = SQLiteCoordinationBackend(db_file)
    backend2 = SQLiteCoordinationBackend(db_file)
    assert db_file.exists()


# 3. unsupported schema fails without mutation
def test_unsupported_schema_open_is_nonmutating(tmp_path):
    db_file = tmp_path / "test_unsupported.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '9.9.9');")
    conn.commit()

    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables_before = {row[0] for row in cursor.fetchall()}
    conn.close()

    with pytest.raises(CoordinationStorageError):
        SQLiteCoordinationBackend(db_file)

    conn2 = sqlite3.connect(str(db_file))
    cursor2 = conn2.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables_after = {row[0] for row in cursor2.fetchall()}
    cursor3 = conn2.execute("SELECT value FROM meta WHERE key = 'schema_version';")
    ver_after = cursor3.fetchone()[0]
    conn2.close()

    assert tables_after == tables_before
    assert ver_after == "9.9.9"
    assert "workers" not in tables_after
    assert "leases" not in tables_after


# 4. same-version missing required table fails without repair
def test_same_version_missing_required_table_fails_without_repair(tmp_path):
    db_file = tmp_path / "test_missing_table.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?);", (SQLITE_SCHEMA_VERSION,))
    conn.commit()
    conn.close()

    with pytest.raises(CoordinationStorageError):
        SQLiteCoordinationBackend(db_file)

    conn2 = sqlite3.connect(str(db_file))
    cursor = conn2.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables_after = {row[0] for row in cursor.fetchall()}
    conn2.close()
    assert "workers" not in tables_after
    assert "leases" not in tables_after


# 5. same-version missing ownership column fails without repair
def test_same_version_missing_ownership_column_fails_without_repair(tmp_path):
    db_file = tmp_path / "test_missing_col.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?);", (SQLITE_SCHEMA_VERSION,))
    conn.execute("CREATE TABLE workers (worker_id TEXT PRIMARY KEY);")
    conn.execute("CREATE TABLE leases (task_id TEXT PRIMARY KEY);")
    conn.commit()
    conn.close()

    with pytest.raises(CoordinationStorageError):
        SQLiteCoordinationBackend(db_file)


# 6. worker registration persists
def test_worker_registration_persists(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    backend1 = SQLiteCoordinationBackend(db_file)
    w1 = WorkerIdentity("w1", "s1", ["c1", "c2"])
    backend1.register_worker(w1)

    backend2 = SQLiteCoordinationBackend(db_file)
    assert backend2.is_worker_registered("w1", "s1")


# 7. contradictory immutable worker registration fails
def test_contradictory_worker_registration_fails(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    backend1 = SQLiteCoordinationBackend(db_file)
    w1 = WorkerIdentity("w1", "s1", ["c1"])
    w1_contradictory = WorkerIdentity("w1", "s1", ["c2"])

    backend1.register_worker(w1)

    backend2 = SQLiteCoordinationBackend(db_file)
    with pytest.raises(InvalidIdentityError):
        backend2.register_worker(w1_contradictory)


# 8. new session allowed
def test_new_session_allowed(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    backend = SQLiteCoordinationBackend(db_file)
    w1_s1 = WorkerIdentity("w1", "s1")
    w1_s2 = WorkerIdentity("w1", "s2")

    backend.register_worker(w1_s1)
    backend.register_worker(w1_s2)
    assert backend.is_worker_registered("w1", "s1")
    assert backend.is_worker_registered("w1", "s2")


# 9. first lease persists across reopen
def test_first_lease_persists_across_reopen(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    clock = ControlledClock()
    backend1 = SQLiteCoordinationBackend(db_file, clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend1.register_worker(w1)

    res = backend1.try_claim("task-1", w1, 60.0)
    assert res.disposition == ClaimDisposition.ACQUIRED

    backend2 = SQLiteCoordinationBackend(db_file, clock.now)
    lease2 = backend2.get_lease("task-1")
    assert lease2 is not None
    assert lease2.worker_id == "w1"
    assert lease2.session_id == "s1"
    assert lease2.lease_id == res.lease.lease_id
    assert lease2.generation == res.lease.generation
    assert lease2.ttl_seconds == 60.0


# 10. same owner after reopen is ALREADY_OWNED
def test_same_owner_after_reopen_already_owned(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    clock = ControlledClock()
    backend1 = SQLiteCoordinationBackend(db_file, clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend1.register_worker(w1)

    res1 = backend1.try_claim("task-1", w1, 60.0)
    clock.advance(15.0)

    backend2 = SQLiteCoordinationBackend(db_file, clock.now)
    res2 = backend2.try_claim("task-1", w1, 60.0)
    assert res2.disposition == ClaimDisposition.ALREADY_OWNED
    assert res2.lease.expires_at == res1.lease.expires_at


# 11. other active owner gets HELD_BY_OTHER
def test_other_active_owner_gets_held_by_other(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    clock = ControlledClock()
    backend = SQLiteCoordinationBackend(db_file, clock.now)
    w1 = WorkerIdentity("w1", "s1")
    w2 = WorkerIdentity("w2", "s2")
    backend.register_worker(w1)
    backend.register_worker(w2)

    backend.try_claim("task-1", w1, 60.0)
    res2 = backend.try_claim("task-1", w2, 60.0)
    assert res2.disposition == ClaimDisposition.HELD_BY_OTHER
    assert res2.lease.worker_id == "w1"


# 12. heartbeat persists
def test_heartbeat_persists(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    clock = ControlledClock()
    backend1 = SQLiteCoordinationBackend(db_file, clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend1.register_worker(w1)

    res = backend1.try_claim("task-1", w1, 60.0)
    clock.advance(20.0)

    hb_lease = backend1.heartbeat("task-1", "w1", "s1", res.lease.lease_id, res.lease.generation)
    assert hb_lease is not None

    backend2 = SQLiteCoordinationBackend(db_file, clock.now)
    lease2 = backend2.get_lease("task-1")
    assert lease2.last_heartbeat_at == clock.now()
    assert lease2.expires_at == clock.now() + timedelta(seconds=60)


# 13. expiry and recovery across restart
def test_expiry_and_recovery_across_restart(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    clock = ControlledClock()
    backend1 = SQLiteCoordinationBackend(db_file, clock.now)
    w1 = WorkerIdentity("w1", "s1")
    w2 = WorkerIdentity("w2", "s2")
    backend1.register_worker(w1)
    backend1.register_worker(w2)

    res1 = backend1.try_claim("task-1", w1, 60.0)
    clock.advance(60.0)

    backend2 = SQLiteCoordinationBackend(db_file, clock.now)
    res2 = backend2.try_claim("task-1", w2, 60.0)
    assert res2.disposition == ClaimDisposition.ACQUIRED
    assert res2.lease.worker_id == "w2"
    assert res2.lease.lease_id != res1.lease.lease_id
    assert res2.lease.generation == res1.lease.generation + 1


# 14. stale operations rejected after recovery
def test_stale_operations_after_recovery_rejected(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    clock = ControlledClock()
    backend1 = SQLiteCoordinationBackend(db_file, clock.now)
    w1 = WorkerIdentity("w1", "s1")
    w2 = WorkerIdentity("w2", "s2")
    backend1.register_worker(w1)
    backend1.register_worker(w2)

    res1 = backend1.try_claim("task-1", w1, 60.0)
    clock.advance(65.0)

    backend2 = SQLiteCoordinationBackend(db_file, clock.now)
    res2 = backend2.try_claim("task-1", w2, 60.0)

    hb_stale = backend2.heartbeat("task-1", "w1", "s1", res1.lease.lease_id, res1.lease.generation)
    assert hb_stale is None

    rel_stale = backend2.release("task-1", "w1", "s1", res1.lease.lease_id, res1.lease.generation)
    assert rel_stale is False


# 15. release persists and enables new epoch
def test_release_persists_and_enables_new_epoch(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    clock = ControlledClock()
    backend1 = SQLiteCoordinationBackend(db_file, clock.now)
    w1 = WorkerIdentity("w1", "s1")
    w2 = WorkerIdentity("w2", "s2")
    backend1.register_worker(w1)
    backend1.register_worker(w2)

    res1 = backend1.try_claim("task-1", w1, 60.0)
    rel = backend1.release("task-1", "w1", "s1", res1.lease.lease_id, res1.lease.generation)
    assert rel is True

    backend2 = SQLiteCoordinationBackend(db_file, clock.now)
    assert backend2.get_lease("task-1").status == LeaseStatus.RELEASED

    res2 = backend2.try_claim("task-1", w2, 60.0)
    assert res2.disposition == ClaimDisposition.ACQUIRED
    assert res2.lease.generation == res1.lease.generation + 1


# 16. Capability Tag Decoder Corruption Matrix
def test_corrupt_capability_json_syntax_fails_closed(tmp_path):
    db_file = tmp_path / "test_cap_syntax.db"
    backend = SQLiteCoordinationBackend(db_file)

    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO workers (worker_id, session_id, capability_tags_json, registered_at) VALUES ('w1', 's1', 'invalid-json', '2026-08-28T12:00:00+00:00');"
    )
    conn.commit()
    conn.close()

    w1 = WorkerIdentity("w1", "s1")
    with pytest.raises(CoordinationStorageError):
        backend.register_worker(w1)


def test_corrupt_capability_json_object_shape_fails_closed(tmp_path):
    db_file = tmp_path / "test_cap_obj.db"
    backend = SQLiteCoordinationBackend(db_file)

    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO workers (worker_id, session_id, capability_tags_json, registered_at) VALUES ('w1', 's1', '{\"c1\": 1}', '2026-08-28T12:00:00+00:00');"
    )
    conn.commit()
    conn.close()

    w1 = WorkerIdentity("w1", "s1", ["c1"])
    with pytest.raises(CoordinationStorageError):
        backend.register_worker(w1)

    with pytest.raises(CoordinationStorageError):
        backend.try_claim("task-1", w1, 60.0)


def test_corrupt_capability_json_scalar_fails_closed(tmp_path):
    db_file = tmp_path / "test_cap_scalar.db"
    backend = SQLiteCoordinationBackend(db_file)

    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO workers (worker_id, session_id, capability_tags_json, registered_at) VALUES ('w1', 's1', '12345', '2026-08-28T12:00:00+00:00');"
    )
    conn.commit()
    conn.close()

    w1 = WorkerIdentity("w1", "s1")
    with pytest.raises(CoordinationStorageError):
        backend.register_worker(w1)


def test_corrupt_capability_json_non_string_entry_fails_closed(tmp_path):
    db_file = tmp_path / "test_cap_nonstr.db"
    backend = SQLiteCoordinationBackend(db_file)

    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO workers (worker_id, session_id, capability_tags_json, registered_at) VALUES ('w1', 's1', '[\"c1\", 123]', '2026-08-28T12:00:00+00:00');"
    )
    conn.commit()
    conn.close()

    w1 = WorkerIdentity("w1", "s1", ["c1"])
    with pytest.raises(CoordinationStorageError):
        backend.register_worker(w1)


# 17. Lease Storage Corruption Matrix
def test_corrupt_persisted_ttl_fails_closed(tmp_path):
    db_file = tmp_path / "test_ttl.db"
    backend = SQLiteCoordinationBackend(db_file)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    backend.try_claim("task-1", w1, 60.0)

    conn = sqlite3.connect(str(db_file))
    conn.execute("UPDATE leases SET ttl_seconds = -10.0 WHERE task_id = 'task-1';")
    conn.commit()
    conn.close()

    with pytest.raises(CoordinationStorageError):
        backend.get_lease("task-1")


def test_corrupt_persisted_generation_fails_closed(tmp_path):
    db_file = tmp_path / "test_gen.db"
    backend = SQLiteCoordinationBackend(db_file)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    backend.try_claim("task-1", w1, 60.0)

    conn = sqlite3.connect(str(db_file))
    conn.execute("UPDATE leases SET generation = -1 WHERE task_id = 'task-1';")
    conn.commit()
    conn.close()

    with pytest.raises(CoordinationStorageError):
        backend.get_lease("task-1")


def test_corrupt_persisted_status_fails_closed(tmp_path):
    db_file = tmp_path / "test_status.db"
    backend = SQLiteCoordinationBackend(db_file)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    backend.try_claim("task-1", w1, 60.0)

    conn = sqlite3.connect(str(db_file))
    conn.execute("UPDATE leases SET status = 'INVALID_STATUS' WHERE task_id = 'task-1';")
    conn.commit()
    conn.close()

    with pytest.raises(CoordinationStorageError):
        backend.get_lease("task-1")


def test_corrupt_naive_timestamp_fails_closed(tmp_path):
    db_file = tmp_path / "test_naive.db"
    backend = SQLiteCoordinationBackend(db_file)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    backend.try_claim("task-1", w1, 60.0)

    conn = sqlite3.connect(str(db_file))
    conn.execute("UPDATE leases SET expires_at = '2026-08-28T12:00:00' WHERE task_id = 'task-1';")
    conn.commit()
    conn.close()

    with pytest.raises(CoordinationStorageError):
        backend.get_lease("task-1")


def test_corrupt_non_utc_timestamp_fails_closed(tmp_path):
    db_file = tmp_path / "test_non_utc.db"
    backend = SQLiteCoordinationBackend(db_file)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    backend.try_claim("task-1", w1, 60.0)

    conn = sqlite3.connect(str(db_file))
    conn.execute("UPDATE leases SET expires_at = '2026-08-28T12:00:00+03:00' WHERE task_id = 'task-1';")
    conn.commit()
    conn.close()

    with pytest.raises(CoordinationStorageError):
        backend.get_lease("task-1")


def test_corrupt_empty_required_ownership_field_fails_closed(tmp_path):
    db_file = tmp_path / "test_empty_field.db"
    backend = SQLiteCoordinationBackend(db_file)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    backend.try_claim("task-1", w1, 60.0)

    conn = sqlite3.connect(str(db_file))
    conn.execute("UPDATE leases SET worker_id = '   ' WHERE task_id = 'task-1';")
    conn.commit()
    conn.close()

    with pytest.raises(CoordinationStorageError):
        backend.get_lease("task-1")


def test_impossible_persisted_time_order_fails_closed(tmp_path):
    db_file = tmp_path / "test_time_order.db"
    backend = SQLiteCoordinationBackend(db_file)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    backend.try_claim("task-1", w1, 60.0)

    # Set acquired_at > last_heartbeat_at (acquired in future relative to last_hb and expires_at)
    conn = sqlite3.connect(str(db_file))
    conn.execute("UPDATE leases SET acquired_at = '2099-01-01T12:00:00+00:00' WHERE task_id = 'task-1';")
    conn.commit()
    conn.close()

    with pytest.raises(CoordinationStorageError):
        backend.get_lease("task-1")


# 18. Non-SQLite existing file fails closed
def test_non_sqlite_existing_file_fails_as_storage_error(tmp_path):
    db_file = tmp_path / "bad_file.db"
    db_file.write_bytes(b"NOT A SQLITE FILE")

    with pytest.raises(CoordinationStorageError):
        SQLiteCoordinationBackend(db_file)


# 19. Invalid busy_timeout_ms validation
def test_invalid_busy_timeout_ms_fails_closed(tmp_path):
    db_file = tmp_path / "test_timeout.db"
    with pytest.raises(InvalidInputError):
        SQLiteCoordinationBackend(db_file, busy_timeout_ms=-100)

    with pytest.raises(InvalidInputError):
        SQLiteCoordinationBackend(db_file, busy_timeout_ms=100000)

    with pytest.raises(InvalidInputError):
        SQLiteCoordinationBackend(db_file, busy_timeout_ms="5000")


# 20. Concurrent winner exact-binding proof
def test_two_backend_instances_concurrent_claim_exact_winner_binding(tmp_path):
    db_file = tmp_path / "test_concurrent_binding.db"
    backend1 = SQLiteCoordinationBackend(db_file)
    backend2 = SQLiteCoordinationBackend(db_file)

    w1 = WorkerIdentity("w1", "s1")
    w2 = WorkerIdentity("w2", "s2")
    backend1.register_worker(w1)
    backend2.register_worker(w2)

    barrier = threading.Barrier(2)
    results = [None, None]
    exceptions = [None, None]

    def worker1_task():
        try:
            barrier.wait()
            results[0] = backend1.try_claim("concurrent-task", w1, 60.0)
        except Exception as exc:
            exceptions[0] = exc

    def worker2_task():
        try:
            barrier.wait()
            results[1] = backend2.try_claim("concurrent-task", w2, 60.0)
        except Exception as exc:
            exceptions[1] = exc

    t1 = threading.Thread(target=worker1_task)
    t2 = threading.Thread(target=worker2_task)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert exceptions == [None, None]

    dispositions = [results[0].disposition, results[1].disposition]
    assert dispositions.count(ClaimDisposition.ACQUIRED) == 1
    assert dispositions.count(ClaimDisposition.HELD_BY_OTHER) == 1

    winner_result = results[0] if results[0].disposition == ClaimDisposition.ACQUIRED else results[1]
    loser_result = results[1] if results[0].disposition == ClaimDisposition.ACQUIRED else results[0]

    persisted = backend1.get_lease("concurrent-task")
    assert persisted is not None
    assert persisted.status == LeaseStatus.ACTIVE

    # Exact binding assertions
    assert persisted.task_id == winner_result.lease.task_id
    assert persisted.worker_id == winner_result.lease.worker_id
    assert persisted.session_id == winner_result.lease.session_id
    assert persisted.lease_id == winner_result.lease.lease_id
    assert persisted.generation == winner_result.lease.generation
    assert persisted.acquired_at == winner_result.lease.acquired_at
    assert persisted.expires_at == winner_result.lease.expires_at

    # Loser lease describes the same active owner epoch
    assert loser_result.lease.lease_id == winner_result.lease.lease_id
    assert loser_result.lease.worker_id == winner_result.lease.worker_id


# 21. Multi-contender exact-binding proof
def test_multi_contender_exact_winner_binding(tmp_path):
    db_file = tmp_path / "test_multi_binding.db"
    backends = [SQLiteCoordinationBackend(db_file) for _ in range(10)]
    workers = [WorkerIdentity(f"w_{i}", f"s_{i}") for i in range(10)]

    for i in range(10):
        backends[i].register_worker(workers[i])

    barrier = threading.Barrier(10)
    results = [None] * 10
    exceptions = [None] * 10

    def worker_task(idx: int):
        try:
            barrier.wait()
            results[idx] = backends[idx].try_claim("multi-task", workers[idx], 60.0)
        except Exception as exc:
            exceptions[idx] = exc

    threads = [threading.Thread(target=worker_task, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(e is None for e in exceptions)

    dispositions = [r.disposition for r in results]
    assert dispositions.count(ClaimDisposition.ACQUIRED) == 1
    assert dispositions.count(ClaimDisposition.HELD_BY_OTHER) == 9

    winner = next(r for r in results if r.disposition == ClaimDisposition.ACQUIRED)
    persisted = backends[0].get_lease("multi-task")

    assert persisted == winner.lease
    for loser in [r for r in results if r.disposition == ClaimDisposition.HELD_BY_OTHER]:
        assert loser.lease.lease_id == winner.lease.lease_id
        assert loser.lease.worker_id == winner.lease.worker_id


# 22. SQL-looking identifier treated as data
def test_sql_looking_identifier_treated_as_data(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    backend = SQLiteCoordinationBackend(db_file)
    sql_id = "task'; DROP TABLE leases; --"
    w1 = WorkerIdentity("w1'; DELETE FROM workers; --", "s1")
    backend.register_worker(w1)

    res = backend.try_claim(sql_id, w1, 60.0)
    assert res.disposition == ClaimDisposition.ACQUIRED

    stored = backend.get_lease(sql_id)
    assert stored is not None
    assert stored.task_id == sql_id
    assert stored.worker_id == w1.worker_id


# 23. DB-loss isolation preserves canonical sentinel
def test_db_loss_isolation_preserves_canonical_sentinel(tmp_path):
    sentinel_file = tmp_path / "sentinel_canonical_truth.json"
    sentinel_data = b'{"canonical": "truth", "important": 12345}'
    sentinel_file.write_bytes(sentinel_data)
    sentinel_hash = hashlib.sha256(sentinel_data).hexdigest()

    db_file = tmp_path / "coordination.db"
    backend1 = SQLiteCoordinationBackend(db_file)
    w1 = WorkerIdentity("w1", "s1")
    backend1.register_worker(w1)
    backend1.try_claim("task-1", w1, 60.0)

    db_file.unlink()
    for extra in tmp_path.glob("coordination.db*"):
        extra.unlink()

    assert sentinel_file.exists()
    assert hashlib.sha256(sentinel_file.read_bytes()).hexdigest() == sentinel_hash

    backend2 = SQLiteCoordinationBackend(db_file)
    assert backend2.get_lease("task-1") is None
    backend2.register_worker(w1)
    res = backend2.try_claim("task-1", w1, 60.0)
    assert res.disposition == ClaimDisposition.ACQUIRED
    assert res.lease.generation == 1


# 24. No external dependencies
def test_no_external_dependencies_in_sqlite_backend():
    import aos.coordination_sqlite as sqlite_coord

    with open(sqlite_coord.__file__, "r", encoding="utf-8") as f:
        content = f.read()

    forbidden = ["git", "postgres", "supabase", "requests", "urllib", "lari", "antigravity"]
    for word in forbidden:
        assert f"import {word}" not in content.lower()
        assert f"from {word}" not in content.lower()


# 25. SQLite backend satisfies CoordinationBackend structural API
def test_sqlite_backend_satisfies_protocol(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    backend: CoordinationBackend = SQLiteCoordinationBackend(db_file)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    assert backend.is_worker_registered("w1", "s1")
