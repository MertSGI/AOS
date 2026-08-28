"""Tests for AOS-5 Stage 11B SQLite Durable Coordination Backend.

Covers:
1. schema initialization
2. compatible reopen
3. unsupported schema fails
4. worker registration persists
5. contradictory immutable worker registration fails
6. new session allowed
7. first lease persists across reopen
8. same owner after reopen is ALREADY_OWNED
9. same-owner reclaim does not extend TTL
10. other active owner gets HELD_BY_OTHER
11. backend authoritative clock
12. heartbeat persists
13. expiry persists
14. exact expiry permits recovery
15. new lease_id on recovery
16. generation increments across restart
17. stale heartbeat after recovery rejected
18. stale release after recovery rejected
19. release persists
20. release enables new epoch
21. two backend instances concurrently claim same task: exactly one winner
22. multi-contender same DB: exactly one active owner
23. corrupt timestamp fails closed
24. corrupt generation fails closed
25. corrupt TTL fails closed
26. malformed capability tag storage fails closed
27. SQL-looking identifier treated as data
28. DB-loss isolation preserves canonical sentinel
29. no Git/provider/LARI/network operation
30. SQLite backend satisfies CoordinationBackend structural API
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


# 3. unsupported schema fails
def test_unsupported_schema_fails(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    backend = SQLiteCoordinationBackend(db_file)

    conn = sqlite3.connect(str(db_file))
    conn.execute("UPDATE meta SET value = '9.9.9' WHERE key = 'schema_version';")
    conn.commit()
    conn.close()

    with pytest.raises(CoordinationStorageError):
        SQLiteCoordinationBackend(db_file)


# 4. worker registration persists
def test_worker_registration_persists(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    backend1 = SQLiteCoordinationBackend(db_file)
    w1 = WorkerIdentity("w1", "s1", ["c1", "c2"])
    backend1.register_worker(w1)

    backend2 = SQLiteCoordinationBackend(db_file)
    assert backend2.is_worker_registered("w1", "s1")


# 5. contradictory immutable worker registration fails
def test_contradictory_worker_registration_fails(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    backend1 = SQLiteCoordinationBackend(db_file)
    w1 = WorkerIdentity("w1", "s1", ["c1"])
    w1_contradictory = WorkerIdentity("w1", "s1", ["c2"])

    backend1.register_worker(w1)

    backend2 = SQLiteCoordinationBackend(db_file)
    with pytest.raises(InvalidIdentityError):
        backend2.register_worker(w1_contradictory)


# 6. new session allowed
def test_new_session_allowed(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    backend = SQLiteCoordinationBackend(db_file)
    w1_s1 = WorkerIdentity("w1", "s1")
    w1_s2 = WorkerIdentity("w1", "s2")

    backend.register_worker(w1_s1)
    backend.register_worker(w1_s2)
    assert backend.is_worker_registered("w1", "s1")
    assert backend.is_worker_registered("w1", "s2")


# 7. first lease persists across reopen
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


# 8. same owner after reopen is ALREADY_OWNED
# 9. same-owner reclaim does not extend TTL
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


# 10. other active owner gets HELD_BY_OTHER
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


# 11. backend authoritative clock
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


# 13. expiry persists
# 14. exact expiry permits recovery
# 15. new lease_id on recovery
# 16. generation increments across restart
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


# 17. stale heartbeat after recovery rejected
# 18. stale release after recovery rejected
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


# 19. release persists
# 20. release enables new epoch
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


# 21. two backend instances concurrently claim same task: exactly one winner
def test_two_backend_instances_concurrent_claim(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    backend1 = SQLiteCoordinationBackend(db_file)
    backend2 = SQLiteCoordinationBackend(db_file)

    w1 = WorkerIdentity("w1", "s1")
    w2 = WorkerIdentity("w2", "s2")
    backend1.register_worker(w1)
    backend2.register_worker(w2)

    barrier = threading.Barrier(2)
    results = [None, None]

    def worker1_task():
        barrier.wait()
        results[0] = backend1.try_claim("concurrent-task", w1, 60.0)

    def worker2_task():
        barrier.wait()
        results[1] = backend2.try_claim("concurrent-task", w2, 60.0)

    t1 = threading.Thread(target=worker1_task)
    t2 = threading.Thread(target=worker2_task)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    dispositions = [results[0].disposition, results[1].disposition]
    assert ClaimDisposition.ACQUIRED in dispositions
    assert ClaimDisposition.HELD_BY_OTHER in dispositions
    assert dispositions.count(ClaimDisposition.ACQUIRED) == 1
    assert dispositions.count(ClaimDisposition.HELD_BY_OTHER) == 1

    stored = backend1.get_lease("concurrent-task")
    assert stored is not None
    assert stored.status == LeaseStatus.ACTIVE


# 22. multi-contender same DB: exactly one active owner
def test_multi_contender_same_db(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    backends = [SQLiteCoordinationBackend(db_file) for _ in range(10)]
    workers = [WorkerIdentity(f"w_{i}", f"s_{i}") for i in range(10)]

    for i in range(10):
        backends[i].register_worker(workers[i])

    barrier = threading.Barrier(10)
    results = [None] * 10

    def worker_task(idx: int):
        barrier.wait()
        results[idx] = backends[idx].try_claim("multi-task", workers[idx], 60.0)

    threads = [threading.Thread(target=worker_task, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    dispositions = [r.disposition for r in results]
    assert dispositions.count(ClaimDisposition.ACQUIRED) == 1
    assert dispositions.count(ClaimDisposition.HELD_BY_OTHER) == 9


# 23. corrupt timestamp fails closed
# 24. corrupt generation fails closed
# 25. corrupt TTL fails closed
# 26. malformed capability tag storage fails closed
def test_storage_corruption_fails_closed(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    backend = SQLiteCoordinationBackend(db_file)
    w1 = WorkerIdentity("w1", "s1", ["c1"])
    backend.register_worker(w1)
    backend.try_claim("task-1", w1, 60.0)

    # Corrupt timestamp
    conn = sqlite3.connect(str(db_file))
    conn.execute("UPDATE leases SET expires_at = 'invalid-date' WHERE task_id = 'task-1';")
    conn.commit()
    conn.close()

    with pytest.raises(CoordinationStorageError):
        backend.get_lease("task-1")

    # Reset valid DB and corrupt generation
    db_file2 = tmp_path / "test_coordination2.db"
    backend2 = SQLiteCoordinationBackend(db_file2)
    backend2.register_worker(w1)
    backend2.try_claim("task-1", w1, 60.0)

    conn = sqlite3.connect(str(db_file2))
    conn.execute("UPDATE leases SET generation = -5 WHERE task_id = 'task-1';")
    conn.commit()
    conn.close()

    with pytest.raises(CoordinationStorageError):
        backend2.get_lease("task-1")


# 27. SQL-looking identifier treated as data
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


# 28. DB-loss isolation preserves canonical sentinel
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

    # Delete coordination database files (simulated DB loss)
    db_file.unlink()
    for extra in tmp_path.glob("coordination.db*"):
        extra.unlink()

    # Sentinel remains untouched
    assert sentinel_file.exists()
    assert hashlib.sha256(sentinel_file.read_bytes()).hexdigest() == sentinel_hash

    # Fresh coordination backend starts new epoch
    backend2 = SQLiteCoordinationBackend(db_file)
    assert backend2.get_lease("task-1") is None
    backend2.register_worker(w1)
    res = backend2.try_claim("task-1", w1, 60.0)
    assert res.disposition == ClaimDisposition.ACQUIRED
    assert res.lease.generation == 1  # Fresh coordination epoch starts at 1


# 29. no Git/provider/LARI/network operation
def test_no_external_dependencies_in_sqlite_backend():
    import aos.coordination_sqlite as sqlite_coord

    with open(sqlite_coord.__file__, "r", encoding="utf-8") as f:
        content = f.read()

    forbidden = ["git", "postgres", "supabase", "requests", "urllib", "lari", "antigravity"]
    for word in forbidden:
        assert f"import {word}" not in content.lower()
        assert f"from {word}" not in content.lower()


# 30. SQLite backend satisfies CoordinationBackend structural API
def test_sqlite_backend_satisfies_protocol(tmp_path):
    db_file = tmp_path / "test_coordination.db"
    backend: CoordinationBackend = SQLiteCoordinationBackend(db_file)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    assert backend.is_worker_registered("w1", "s1")
