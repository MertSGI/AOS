"""Tests for AOS-5 Distributed Coordination Foundation — Stage 11A-R2.

Comprehensive test matrix restoring all valid Stage 11A tests, preserving all R1 regressions,
and adding Stage 11A-R2 extreme TTL / datetime overflow fail-closed tests.
"""

from datetime import datetime, timedelta, timezone
import math
import sys
import threading
import pytest

from aos.coordination import (
    ClaimDisposition,
    InMemoryCoordinationBackend,
    InvalidClockError,
    InvalidIdentityError,
    InvalidInputError,
    LeaseStatus,
    WorkerIdentity,
)


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


# -----------------------------------------------------------------------------
# RESTORED STAGE 11A & R1 WORKER IDENTITY & REGISTRATION TESTS
# -----------------------------------------------------------------------------

def test_valid_immutable_worker_identity():
    w = WorkerIdentity(worker_id="w1", session_id="s1", capability_tags=frozenset(["gpu", "linux"]))
    assert w.worker_id == "w1"
    assert w.session_id == "s1"
    assert w.capability_tags == frozenset(["gpu", "linux"])

    with pytest.raises(Exception):
        w.worker_id = "w2"


def test_capability_tag_preservation():
    w = WorkerIdentity(worker_id="w1", session_id="s1", capability_tags=["tag1", "tag2"])
    assert w.capability_tags == frozenset(["tag1", "tag2"])


def test_same_worker_same_session_idempotent():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1", ["c1"])
    backend.register_worker(w1)
    backend.register_worker(w1)
    assert backend.is_worker_registered("w1", "s1")


def test_same_worker_same_session_contradictory_tags_fails():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1", ["c1"])
    w1_contradictory = WorkerIdentity("w1", "s1", ["c2"])
    backend.register_worker(w1)
    with pytest.raises(InvalidIdentityError):
        backend.register_worker(w1_contradictory)


def test_same_worker_new_session_registration_succeeds():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1_s1 = WorkerIdentity("w1", "s1", ["c1"])
    w1_s2 = WorkerIdentity("w1", "s2", ["c1"])
    backend.register_worker(w1_s1)
    backend.register_worker(w1_s2)
    assert backend.is_worker_registered("w1", "s1")
    assert backend.is_worker_registered("w1", "s2")


def test_unregistered_worker_cannot_claim():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    with pytest.raises(InvalidIdentityError):
        backend.try_claim("task-1", w1, 60.0)


# -----------------------------------------------------------------------------
# RESTORED STAGE 11A & R1 CLAIM, RECLAIM, GENERATION TESTS
# -----------------------------------------------------------------------------

def test_first_claim_succeeds():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    res = backend.try_claim("task-1", w1, 60.0)
    assert res.disposition == ClaimDisposition.ACQUIRED
    assert res.lease is not None
    assert res.lease.task_id == "task-1"
    assert res.lease.worker_id == "w1"
    assert res.lease.session_id == "s1"
    assert res.lease.generation == 1
    assert res.lease.status == LeaseStatus.ACTIVE
    assert res.lease.acquired_at == clock.now()
    assert res.lease.expires_at == clock.now() + timedelta(seconds=60)


def test_same_exact_owner_reclaim_idempotent():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    res1 = backend.try_claim("task-1", w1, 60.0)
    clock.advance(10.0)
    res2 = backend.try_claim("task-1", w1, 60.0)
    assert res2.disposition == ClaimDisposition.ALREADY_OWNED
    assert res2.lease.lease_id == res1.lease.lease_id
    assert res2.lease.expires_at == res1.lease.expires_at


def test_new_session_cannot_inherit_old_active_ownership():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1_s1 = WorkerIdentity("w1", "s1")
    w1_s2 = WorkerIdentity("w1", "s2")
    backend.register_worker(w1_s1)
    backend.register_worker(w1_s2)

    res1 = backend.try_claim("task-1", w1_s1, 60.0)
    assert res1.disposition == ClaimDisposition.ACQUIRED

    res2 = backend.try_claim("task-1", w1_s2, 60.0)
    assert res2.disposition == ClaimDisposition.HELD_BY_OTHER
    assert res2.lease.session_id == "s1"
    assert res2.lease.lease_id == res1.lease.lease_id


def test_ordinary_contention_zero_ownership_mutation():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    w2 = WorkerIdentity("w2", "s2")
    backend.register_worker(w1)
    backend.register_worker(w2)

    res1 = backend.try_claim("task-1", w1, 60.0)
    res2 = backend.try_claim("task-1", w2, 60.0)
    current = backend.get_lease("task-1")

    assert current.worker_id == "w1"
    assert current.session_id == "s1"
    assert current.lease_id == res1.lease.lease_id
    assert current.generation == 1


def test_expired_lease_can_be_reclaimed():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    w2 = WorkerIdentity("w2", "s2")
    backend.register_worker(w1)
    backend.register_worker(w2)

    res1 = backend.try_claim("task-1", w1, 60.0)
    clock.advance(65.0)

    res2 = backend.try_claim("task-1", w2, 60.0)
    assert res2.disposition == ClaimDisposition.ACQUIRED
    assert res2.lease.worker_id == "w2"
    assert res2.lease.session_id == "s2"
    assert res2.lease.lease_id != res1.lease.lease_id
    assert res2.lease.generation == res1.lease.generation + 1


def test_generation_remains_monotonic():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    w2 = WorkerIdentity("w2", "s2")
    backend.register_worker(w1)
    backend.register_worker(w2)

    r1 = backend.try_claim("task-1", w1, 60.0)
    assert r1.lease.generation == 1
    backend.release("task-1", "w1", "s1", r1.lease.lease_id, r1.lease.generation)

    r2 = backend.try_claim("task-1", w2, 60.0)
    assert r2.lease.generation == 2

    clock.advance(70.0)
    r3 = backend.try_claim("task-1", w1, 60.0)
    assert r3.lease.generation == 3


# -----------------------------------------------------------------------------
# RESTORED STAGE 11A & R1 HEARTBEAT & RELEASE TESTS
# -----------------------------------------------------------------------------

def test_heartbeat_uses_stored_lease_ttl():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)

    res1 = backend.try_claim("task-1", w1, 60.0)
    assert res1.lease.ttl_seconds == 60.0

    clock.advance(20.0)
    hb_lease = backend.heartbeat(
        task_id="task-1",
        worker_id="w1",
        session_id="s1",
        lease_id=res1.lease.lease_id,
        generation=res1.lease.generation,
    )
    assert hb_lease is not None
    assert hb_lease.ttl_seconds == 60.0
    assert hb_lease.last_heartbeat_at == clock.now()
    assert hb_lease.expires_at == clock.now() + timedelta(seconds=60)


def test_exact_expiry_boundary():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    res = backend.try_claim("task-1", w1, 60.0)

    clock.advance(60.0)
    w2 = WorkerIdentity("w2", "s2")
    backend.register_worker(w2)
    claim2 = backend.try_claim("task-1", w2, 60.0)
    assert claim2.disposition == ClaimDisposition.ACQUIRED


def test_expired_heartbeat_cannot_resurrect():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    res = backend.try_claim("task-1", w1, 60.0)

    clock.advance(60.0)
    hb_res = backend.heartbeat(
        task_id="task-1",
        worker_id="w1",
        session_id="s1",
        lease_id=res.lease.lease_id,
        generation=res.lease.generation,
    )
    assert hb_res is None
    curr = backend.get_lease("task-1")
    assert curr.status == LeaseStatus.EXPIRED


def test_stale_old_heartbeat_rejected_after_reclaim():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    w2 = WorkerIdentity("w2", "s2")
    backend.register_worker(w1)
    backend.register_worker(w2)

    res1 = backend.try_claim("task-1", w1, 60.0)
    clock.advance(65.0)
    res2 = backend.try_claim("task-1", w2, 60.0)

    stale_hb = backend.heartbeat(
        task_id="task-1",
        worker_id="w1",
        session_id="s1",
        lease_id=res1.lease.lease_id,
        generation=res1.lease.generation,
    )
    assert stale_hb is None
    assert backend.get_lease("task-1").lease_id == res2.lease.lease_id


def test_stale_old_release_rejected_after_reclaim():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    w2 = WorkerIdentity("w2", "s2")
    backend.register_worker(w1)
    backend.register_worker(w2)

    res1 = backend.try_claim("task-1", w1, 60.0)
    clock.advance(65.0)
    res2 = backend.try_claim("task-1", w2, 60.0)

    stale_rel = backend.release(
        task_id="task-1",
        worker_id="w1",
        session_id="s1",
        lease_id=res1.lease.lease_id,
        generation=res1.lease.generation,
    )
    assert stale_rel is False
    assert backend.get_lease("task-1").status == LeaseStatus.ACTIVE
    assert backend.get_lease("task-1").worker_id == "w2"


def test_release_before_expiry_succeeds():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    res = backend.try_claim("task-1", w1, 60.0)

    clock.advance(30.0)
    rel = backend.release("task-1", "w1", "s1", res.lease.lease_id, res.lease.generation)
    assert rel is True
    assert backend.get_lease("task-1").status == LeaseStatus.RELEASED


def test_release_at_or_after_expiry_fails():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    res = backend.try_claim("task-1", w1, 60.0)

    clock.advance(60.0)
    rel_boundary = backend.release("task-1", "w1", "s1", res.lease.lease_id, res.lease.generation)
    assert rel_boundary is False
    assert backend.get_lease("task-1").status == LeaseStatus.EXPIRED


# -----------------------------------------------------------------------------
# CONCURRENCY TESTS
# -----------------------------------------------------------------------------

def test_two_thread_concurrent_claim_exact_one_winner():
    backend = InMemoryCoordinationBackend()
    w1 = WorkerIdentity("worker_a", "session_a")
    w2 = WorkerIdentity("worker_b", "session_b")
    backend.register_worker(w1)
    backend.register_worker(w2)

    barrier = threading.Barrier(2)
    results = [None, None]

    def worker_task(idx: int, identity: WorkerIdentity):
        barrier.wait()
        res = backend.try_claim("concurrent-task", identity, 60.0)
        results[idx] = res

    t1 = threading.Thread(target=worker_task, args=(0, w1))
    t2 = threading.Thread(target=worker_task, args=(1, w2))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    dispositions = [results[0].disposition, results[1].disposition]
    assert ClaimDisposition.ACQUIRED in dispositions
    assert ClaimDisposition.HELD_BY_OTHER in dispositions

    winner_count = dispositions.count(ClaimDisposition.ACQUIRED)
    loser_count = dispositions.count(ClaimDisposition.HELD_BY_OTHER)
    assert winner_count == 1
    assert loser_count == 1

    stored_lease = backend.get_lease("concurrent-task")
    assert stored_lease is not None
    assert stored_lease.status == LeaseStatus.ACTIVE

    winner_res = results[0] if results[0].disposition == ClaimDisposition.ACQUIRED else results[1]
    assert stored_lease.lease_id == winner_res.lease.lease_id


def test_multi_worker_concurrent_contention():
    backend = InMemoryCoordinationBackend()
    workers = [WorkerIdentity(f"w_{i}", f"s_{i}") for i in range(10)]
    for w in workers:
        backend.register_worker(w)

    barrier = threading.Barrier(10)
    results = [None] * 10

    def worker_task(idx: int, identity: WorkerIdentity):
        barrier.wait()
        results[idx] = backend.try_claim("multi-contention-task", identity, 60.0)

    threads = [threading.Thread(target=worker_task, args=(i, workers[i])) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    dispositions = [r.disposition for r in results]
    assert dispositions.count(ClaimDisposition.ACQUIRED) == 1
    assert dispositions.count(ClaimDisposition.HELD_BY_OTHER) == 9

    stored = backend.get_lease("multi-contention-task")
    assert stored is not None
    assert stored.status == LeaseStatus.ACTIVE


# -----------------------------------------------------------------------------
# STAGE 11A-R2 EXTREME TTL & DATETIME OVERFLOW FAIL-CLOSED MATRIX
# -----------------------------------------------------------------------------

def test_strict_ttl_validation():
    backend = InMemoryCoordinationBackend()
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)

    with pytest.raises(InvalidInputError):
        backend.try_claim("task-1", w1, True)
    with pytest.raises(InvalidInputError):
        backend.try_claim("task-1", w1, False)
    with pytest.raises(InvalidInputError):
        backend.try_claim("task-1", w1, float("nan"))
    with pytest.raises(InvalidInputError):
        backend.try_claim("task-1", w1, float("inf"))
    with pytest.raises(InvalidInputError):
        backend.try_claim("task-1", w1, float("-inf"))
    with pytest.raises(InvalidInputError):
        backend.try_claim("task-1", w1, 0)
    with pytest.raises(InvalidInputError):
        backend.try_claim("task-1", w1, -10.0)
    with pytest.raises(InvalidInputError):
        backend.try_claim("task-1", w1, "60")


def test_extreme_ttl_huge_integer_fails_domain_closed():
    backend = InMemoryCoordinationBackend()
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)

    # 10**400 cannot convert to float (Python OverflowError during float(10**400))
    huge_int = 10**400
    with pytest.raises(InvalidInputError):
        backend.try_claim("task-1", w1, huge_int)
    assert backend.get_lease("task-1") is None


def test_extreme_ttl_finite_float_overflow_fails_domain_closed():
    backend = InMemoryCoordinationBackend()
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)

    # 1e308 is finite float, but datetime + timedelta(seconds=1e308) overflows
    with pytest.raises(InvalidInputError):
        backend.try_claim("task-1", w1, 1e308)
    assert backend.get_lease("task-1") is None


def test_clock_near_datetime_max_overflow_fails_domain_closed():
    near_max = datetime(9999, 12, 31, 23, 50, 0, tzinfo=timezone.utc)
    clock = ControlledClock(near_max)
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)

    # 3600 seconds from 9999-12-31 23:50 exceeds datetime.MAXYEAR (year 10000)
    with pytest.raises(InvalidInputError):
        backend.try_claim("task-1", w1, 3600.0)
    assert backend.get_lease("task-1") is None


def test_heartbeat_expiry_overflow_preserves_current_lease():
    clock = ControlledClock(datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc))
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)

    # Acquire initial lease near datetime.max (expires at 9999-12-31 23:59:50)
    clock.current_time = datetime(9999, 12, 31, 23, 59, 0, tzinfo=timezone.utc)
    res = backend.try_claim("task-1", w1, 50.0)
    assert res.disposition == ClaimDisposition.ACQUIRED
    initial_lease = backend.get_lease("task-1")

    # Advance clock by 10s so lease is ACTIVE and unexpired (now is 23:59:10 < expires 23:59:50)
    clock.current_time = datetime(9999, 12, 31, 23, 59, 10, tzinfo=timezone.utc)

    # Heartbeat attempt with stored ttl_seconds=50 will compute 23:59:10 + 50s = 00:00:00 next day (overflow MAXYEAR 9999)
    with pytest.raises(InvalidInputError):
        backend.heartbeat("task-1", "w1", "s1", res.lease.lease_id, res.lease.generation)

    # Verify lease object state remains completely unmutated
    after_lease = backend.get_lease("task-1")
    assert after_lease == initial_lease


def test_bool_generation_rejected():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    res = backend.try_claim("task-1", w1, 60.0)

    with pytest.raises(InvalidInputError):
        backend.heartbeat("task-1", "w1", "s1", res.lease.lease_id, True)
    with pytest.raises(InvalidInputError):
        backend.release("task-1", "w1", "s1", res.lease.lease_id, True)


def test_no_external_dependencies_or_imports():
    import aos.coordination as coord

    with open(coord.__file__, "r", encoding="utf-8") as f:
        content = f.read()

    forbidden = ["git", "sqlite", "postgres", "supabase", "requests", "urllib", "lari", "antigravity"]
    for word in forbidden:
        assert f"import {word}" not in content.lower()
        assert f"from {word}" not in content.lower()
