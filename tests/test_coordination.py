"""Tests for AOS-5 Distributed Coordination Foundation — Stage 11A.

Covers:
1. valid immutable worker identity
2. capability tag preservation
3. exact duplicate registration is idempotent
4. contradictory same-session registration fails closed
5. unregistered worker cannot claim
6. first claim succeeds
7. same exact owner re-claim is idempotent
8. different active owner receives BUSY/HELD_BY_OTHER
9. ordinary contention makes zero ownership mutation
10. heartbeat by exact owner succeeds
11. heartbeat uses backend clock, not caller time
12. heartbeat extends expiry
13. now == expires_at is expired
14. expired heartbeat cannot resurrect lease
15. expired lease can be reclaimed
16. reclaim creates new lease_id
17. reclaim increments generation/fencing token
18. stale old heartbeat rejected after reclaim
19. stale old release rejected after reclaim
20. valid release succeeds
21. release allows new ownership epoch
22. release/reclaim generation remains monotonic
23. two-thread same-task claim yields exactly one winner
24. concurrent contention leaves exactly one active owner
25. coordination module performs no network/Git/provider/LARI operation
"""

from datetime import datetime, timedelta, timezone
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


# 1. valid immutable worker identity
def test_valid_immutable_worker_identity():
    w = WorkerIdentity(worker_id="w1", session_id="s1", capability_tags=frozenset(["gpu", "linux"]))
    assert w.worker_id == "w1"
    assert w.session_id == "s1"
    assert w.capability_tags == frozenset(["gpu", "linux"])

    with pytest.raises(Exception):
        w.worker_id = "w2"


# 2. capability tag preservation
def test_capability_tag_preservation():
    w = WorkerIdentity(worker_id="w1", session_id="s1", capability_tags=["tag1", "tag2"])
    assert w.capability_tags == frozenset(["tag1", "tag2"])


# 3. exact duplicate registration is idempotent
def test_exact_duplicate_registration_idempotent():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1", ["c1"])
    backend.register_worker(w1)
    backend.register_worker(w1)  # idempotent
    assert backend.is_worker_registered("w1", "s1")


# 4. contradictory same-session registration fails closed
def test_contradictory_registration_fails_closed():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1", ["c1"])
    w1_contradictory = WorkerIdentity("w1", "s1_new", ["c1"])
    backend.register_worker(w1)
    with pytest.raises(InvalidIdentityError):
        backend.register_worker(w1_contradictory)


# 5. unregistered worker cannot claim
def test_unregistered_worker_cannot_claim():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    with pytest.raises(InvalidIdentityError):
        backend.try_claim("task-1", w1, 60.0)


# 6. first claim succeeds
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


# 7. same exact owner re-claim is idempotent
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
    assert res2.lease.expires_at == res1.lease.expires_at  # TTL not silently extended


# 8. different active owner receives BUSY/HELD_BY_OTHER
def test_different_active_owner_receives_held_by_other():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    w2 = WorkerIdentity("w2", "s2")
    backend.register_worker(w1)
    backend.register_worker(w2)

    res1 = backend.try_claim("task-1", w1, 60.0)
    res2 = backend.try_claim("task-1", w2, 60.0)

    assert res1.disposition == ClaimDisposition.ACQUIRED
    assert res2.disposition == ClaimDisposition.HELD_BY_OTHER
    assert res2.lease.worker_id == "w1"


# 9. ordinary contention makes zero ownership mutation
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


# 10. heartbeat by exact owner succeeds
def test_heartbeat_by_exact_owner_succeeds():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    res = backend.try_claim("task-1", w1, 60.0)

    clock.advance(20.0)
    hb_lease = backend.heartbeat(
        task_id="task-1",
        worker_id="w1",
        session_id="s1",
        lease_id=res.lease.lease_id,
        generation=res.lease.generation,
        ttl_seconds=60.0,
    )
    assert hb_lease is not None
    assert hb_lease.last_heartbeat_at == clock.now()
    assert hb_lease.expires_at == clock.now() + timedelta(seconds=60)


# 11. heartbeat uses backend clock, not caller time
def test_heartbeat_uses_backend_clock():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    res = backend.try_claim("task-1", w1, 60.0)

    clock.advance(15.0)
    hb_lease = backend.heartbeat(
        task_id="task-1",
        worker_id="w1",
        session_id="s1",
        lease_id=res.lease.lease_id,
        generation=res.lease.generation,
        ttl_seconds=30.0,
    )
    assert hb_lease.last_heartbeat_at == clock.now()
    assert hb_lease.expires_at == clock.now() + timedelta(seconds=30)


# 12. heartbeat extends expiry
def test_heartbeat_extends_expiry():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    res = backend.try_claim("task-1", w1, 60.0)
    orig_exp = res.lease.expires_at

    clock.advance(40.0)
    hb_lease = backend.heartbeat(
        task_id="task-1",
        worker_id="w1",
        session_id="s1",
        lease_id=res.lease.lease_id,
        generation=res.lease.generation,
        ttl_seconds=60.0,
    )
    assert hb_lease.expires_at > orig_exp


# 13. now == expires_at is expired
def test_exact_expiry_boundary():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    res = backend.try_claim("task-1", w1, 60.0)

    # Advance clock to exact expiry boundary
    clock.advance(60.0)
    # Claim from w2 should succeed because task is expired
    w2 = WorkerIdentity("w2", "s2")
    backend.register_worker(w2)
    claim2 = backend.try_claim("task-1", w2, 60.0)
    assert claim2.disposition == ClaimDisposition.ACQUIRED


# 14. expired heartbeat cannot resurrect lease
def test_expired_heartbeat_cannot_resurrect():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)
    res = backend.try_claim("task-1", w1, 60.0)

    clock.advance(60.0)  # now == expires_at
    hb_res = backend.heartbeat(
        task_id="task-1",
        worker_id="w1",
        session_id="s1",
        lease_id=res.lease.lease_id,
        generation=res.lease.generation,
        ttl_seconds=60.0,
    )
    assert hb_res is None
    curr = backend.get_lease("task-1")
    assert curr.status == LeaseStatus.EXPIRED


# 15. expired lease can be reclaimed
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


# 16. reclaim creates new lease_id
def test_reclaim_creates_new_lease_id():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    w2 = WorkerIdentity("w2", "s2")
    backend.register_worker(w1)
    backend.register_worker(w2)

    res1 = backend.try_claim("task-1", w1, 60.0)
    clock.advance(65.0)
    res2 = backend.try_claim("task-1", w2, 60.0)

    assert res2.lease.lease_id != res1.lease.lease_id


# 17. reclaim increments generation/fencing token
def test_reclaim_increments_generation():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    w2 = WorkerIdentity("w2", "s2")
    backend.register_worker(w1)
    backend.register_worker(w2)

    res1 = backend.try_claim("task-1", w1, 60.0)
    clock.advance(65.0)
    res2 = backend.try_claim("task-1", w2, 60.0)

    assert res2.lease.generation == res1.lease.generation + 1


# 18. stale old heartbeat rejected after reclaim
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
        ttl_seconds=60.0,
    )
    assert stale_hb is None
    assert backend.get_lease("task-1").lease_id == res2.lease.lease_id


# 19. stale old release rejected after reclaim
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


# 20. valid release succeeds
def test_valid_release_succeeds():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    backend.register_worker(w1)

    res1 = backend.try_claim("task-1", w1, 60.0)
    rel = backend.release(
        task_id="task-1",
        worker_id="w1",
        session_id="s1",
        lease_id=res1.lease.lease_id,
        generation=res1.lease.generation,
    )
    assert rel is True
    assert backend.get_lease("task-1").status == LeaseStatus.RELEASED


# 21. release allows new ownership epoch
def test_release_allows_new_ownership_epoch():
    clock = ControlledClock()
    backend = InMemoryCoordinationBackend(clock.now)
    w1 = WorkerIdentity("w1", "s1")
    w2 = WorkerIdentity("w2", "s2")
    backend.register_worker(w1)
    backend.register_worker(w2)

    res1 = backend.try_claim("task-1", w1, 60.0)
    backend.release(
        task_id="task-1",
        worker_id="w1",
        session_id="s1",
        lease_id=res1.lease.lease_id,
        generation=res1.lease.generation,
    )

    res2 = backend.try_claim("task-1", w2, 60.0)
    assert res2.disposition == ClaimDisposition.ACQUIRED
    assert res2.lease.worker_id == "w2"


# 22. release/reclaim generation remains monotonic
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


# 23. two-thread same-task claim yields exactly one winner
# 24. concurrent contention leaves exactly one active owner
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


# Fail-closed input validation tests (Section 13)
def test_fail_closed_input_validation():
    backend = InMemoryCoordinationBackend()
    w1 = WorkerIdentity("w1", "s1")

    # Empty worker_id / session_id
    with pytest.raises(InvalidIdentityError):
        WorkerIdentity("", "s1")
    with pytest.raises(InvalidIdentityError):
        WorkerIdentity("w1", "   ")

    # Empty task_id
    backend.register_worker(w1)
    with pytest.raises(InvalidInputError):
        backend.try_claim("", w1, 60.0)

    # ttl <= 0
    with pytest.raises(InvalidInputError):
        backend.try_claim("task-1", w1, 0)
    with pytest.raises(InvalidInputError):
        backend.try_claim("task-1", w1, -10)

    # Naive datetime returned by injected clock
    def naive_clock():
        return datetime(2026, 8, 28, 12, 0, 0)

    bad_backend = InMemoryCoordinationBackend(naive_clock)
    bad_backend.register_worker(w1)
    with pytest.raises(InvalidClockError):
        bad_backend.try_claim("task-1", w1, 60.0)


# 25. coordination module performs no network/Git/provider/LARI operation
def test_no_external_dependencies_or_imports():
    import aos.coordination as coord

    with open(coord.__file__, "r", encoding="utf-8") as f:
        content = f.read()

    forbidden = ["git", "sqlite", "postgres", "supabase", "requests", "urllib", "lari", "antigravity"]
    for word in forbidden:
        assert f"import {word}" not in content.lower()
        assert f"from {word}" not in content.lower()
