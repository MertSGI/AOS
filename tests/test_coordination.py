"""Tests for AOS-5 Distributed Coordination Foundation — Stage 11A-R1.

Covers Stage 11A & 11A-R1 Required Test Matrix:
1. same worker + same session + same immutable identity idempotent
2. same worker + same session + contradictory tags fails
3. same worker + new session registers successfully
4. new session cannot inherit old active ownership
5. new session receives HELD_BY_OTHER while old session active
6. new session acquires only after old expiry
7. new session gets new lease_id
8. new session gets higher generation
9. stale prior-session heartbeat rejected
10. stale prior-session release rejected
11. heartbeat uses original lease TTL
12. heartbeat cannot change lease TTL
13. bool TTL rejected
14. NaN TTL rejected
15. +Infinity TTL rejected
16. -Infinity TTL rejected
17. zero/negative TTL rejected
18. bool generation rejected on heartbeat
19. bool generation rejected on release
20. release before expiry succeeds
21. release exactly at expiry fails
22. release after expiry fails
23. expired release does not create RELEASED ownership
24. two-thread exact-one-winner claim still passes
25. multiple contender exact-one-owner invariant still passes
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


# 1. same worker + same session + same immutable identity idempotent
def test_same_worker_same_session_idempotent():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1", ["c1"])
    backend.register_worker(w1)
    backend.register_worker(w1)
    assert backend.is_worker_registered("w1", "s1")


# 2. same worker + same session + contradictory tags fails
def test_same_worker_same_session_contradictory_tags_fails():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1", ["c1"])
    w1_contradictory = WorkerIdentity("w1", "s1", ["c2"])
    backend.register_worker(w1)
    with pytest.raises(InvalidIdentityError):
        backend.register_worker(w1_contradictory)


# 3. same worker + new session registers successfully
def test_same_worker_new_session_registration_succeeds():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1_s1 = WorkerIdentity("w1", "s1", ["c1"])
    w1_s2 = WorkerIdentity("w1", "s2", ["c1"])
    backend.register_worker(w1_s1)
    backend.register_worker(w1_s2)
    assert backend.is_worker_registered("w1", "s1")
    assert backend.is_worker_registered("w1", "s2")


# 4. new session cannot inherit old active ownership
# 5. new session receives HELD_BY_OTHER while old session active
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


# 6. new session acquires only after old expiry
# 7. new session gets new lease_id
# 8. new session gets higher generation
def test_new_session_acquires_after_old_expiry():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1_s1 = WorkerIdentity("w1", "s1")
    w1_s2 = WorkerIdentity("w1", "s2")
    backend.register_worker(w1_s1)
    backend.register_worker(w1_s2)

    res1 = backend.try_claim("task-1", w1_s1, 60.0)
    clock.advance(65.0)

    res2 = backend.try_claim("task-1", w1_s2, 60.0)
    assert res2.disposition == ClaimDisposition.ACQUIRED
    assert res2.lease.worker_id == "w1"
    assert res2.lease.session_id == "s2"
    assert res2.lease.lease_id != res1.lease.lease_id
    assert res2.lease.generation == res1.lease.generation + 1


# 9. stale prior-session heartbeat rejected
def test_stale_prior_session_heartbeat_rejected():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1_s1 = WorkerIdentity("w1", "s1")
    w1_s2 = WorkerIdentity("w1", "s2")
    backend.register_worker(w1_s1)
    backend.register_worker(w1_s2)

    res1 = backend.try_claim("task-1", w1_s1, 60.0)
    clock.advance(65.0)
    res2 = backend.try_claim("task-1", w1_s2, 60.0)

    stale_hb = backend.heartbeat(
        task_id="task-1",
        worker_id="w1",
        session_id="s1",
        lease_id=res1.lease.lease_id,
        generation=res1.lease.generation,
    )
    assert stale_hb is None
    assert backend.get_lease("task-1").session_id == "s2"


# 10. stale prior-session release rejected
def test_stale_prior_session_release_rejected():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1_s1 = WorkerIdentity("w1", "s1")
    w1_s2 = WorkerIdentity("w1", "s2")
    backend.register_worker(w1_s1)
    backend.register_worker(w1_s2)

    res1 = backend.try_claim("task-1", w1_s1, 60.0)
    clock.advance(65.0)
    res2 = backend.try_claim("task-1", w1_s2, 60.0)

    stale_rel = backend.release(
        task_id="task-1",
        worker_id="w1",
        session_id="s1",
        lease_id=res1.lease.lease_id,
        generation=res1.lease.generation,
    )
    assert stale_rel is False
    assert backend.get_lease("task-1").status == LeaseStatus.ACTIVE
    assert backend.get_lease("task-1").session_id == "s2"


# 11. heartbeat uses original lease TTL
# 12. heartbeat cannot change lease TTL
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


# 13. bool TTL rejected
# 14. NaN TTL rejected
# 15. +Infinity TTL rejected
# 16. -Infinity TTL rejected
# 17. zero/negative TTL rejected
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


# 18. bool generation rejected on heartbeat
# 19. bool generation rejected on release
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


# 20. release before expiry succeeds
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


# 21. release exactly at expiry fails
# 22. release after expiry fails
# 23. expired release does not create RELEASED ownership
def test_release_at_or_after_expiry_fails():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    res = backend.try_claim("task-1", w1, 60.0)

    # Exactly at expiry boundary
    clock.advance(60.0)
    rel_boundary = backend.release("task-1", "w1", "s1", res.lease.lease_id, res.lease.generation)
    assert rel_boundary is False
    assert backend.get_lease("task-1").status == LeaseStatus.EXPIRED

    # Reset with new claim
    w2 = WorkerIdentity("w2", "s2")
    backend.register_worker(w2)
    res2 = backend.try_claim("task-2", w2, 60.0)

    # Well past expiry
    clock.advance(70.0)
    rel_past = backend.release("task-2", "w2", "s2", res2.lease.lease_id, res2.lease.generation)
    assert rel_past is False
    assert backend.get_lease("task-2").status == LeaseStatus.EXPIRED


# 24. two-thread exact-one-winner claim still passes
# 25. multiple contender exact-one-owner invariant still passes
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


def test_unregistered_worker_cannot_claim():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    with pytest.raises(InvalidIdentityError):
        backend.try_claim("task-1", w1, 60.0)


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


def test_no_external_dependencies_or_imports():
    import aos.coordination as coord

    with open(coord.__file__, "r", encoding="utf-8") as f:
        content = f.read()

    forbidden = ["git", "sqlite", "postgres", "supabase", "requests", "urllib", "lari", "antigravity"]
    for word in forbidden:
        assert f"import {word}" not in content.lower()
        assert f"from {word}" not in content.lower()
