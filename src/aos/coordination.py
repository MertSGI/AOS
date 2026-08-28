"""AOS-5 Distributed Multi-PC Coordination — Stage 11A Coordination Foundation.

Pure Python standard-library backend-agnostic coordination foundation:
- WorkerIdentity (immutable worker & session distinction, capability tags)
- Worker Registration
- Lease Model (immutable LeaseSnapshot, fencing token / generation)
- Authoritative Time Contract (dependency-injected clock, UTC timezone-aware)
- Atomic Claim Contract & In-Memory Reference Backend
- Heartbeat Contract
- Release Contract
- Expiry / Recovery & Stale-Owner Fencing
- Fail-Closed Input Validation
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import threading
from typing import Callable, Dict, FrozenSet, Optional, Tuple, Union


class CoordinationError(Exception):
    """Base exception for invalid/malformed coordination input."""


class InvalidIdentityError(CoordinationError):
    """Raised when WorkerIdentity input is invalid or inconsistent."""


class InvalidInputError(CoordinationError):
    """Raised when task, worker, session, or TTL input is malformed."""


class InvalidClockError(CoordinationError):
    """Raised when injected clock returns naive datetime or invalid time."""


class LeaseStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class ClaimDisposition(str, Enum):
    ACQUIRED = "ACQUIRED"
    ALREADY_OWNED = "ALREADY_OWNED"
    HELD_BY_OTHER = "HELD_BY_OTHER"


@dataclass(frozen=True)
class WorkerIdentity:
    worker_id: str
    session_id: str
    capability_tags: FrozenSet[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.worker_id, str) or not self.worker_id.strip():
            raise InvalidIdentityError("worker_id must be a non-empty string")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise InvalidIdentityError("session_id must be a non-empty string")
        if not isinstance(self.capability_tags, (frozenset, set, list, tuple)):
            raise InvalidIdentityError("capability_tags must be a collection of strings")

        tags = set()
        for tag in self.capability_tags:
            if not isinstance(tag, str) or not tag.strip():
                raise InvalidIdentityError("capability_tags entries must be non-empty strings")
            tags.add(tag.strip())

        # Freeze normalized capability tags
        object.__setattr__(self, "worker_id", self.worker_id.strip())
        object.__setattr__(self, "session_id", self.session_id.strip())
        object.__setattr__(self, "capability_tags", frozenset(tags))


@dataclass(frozen=True)
class LeaseSnapshot:
    task_id: str
    worker_id: str
    session_id: str
    lease_id: str
    acquired_at: datetime
    last_heartbeat_at: datetime
    expires_at: datetime
    generation: int
    status: LeaseStatus


@dataclass(frozen=True)
class ClaimResult:
    disposition: ClaimDisposition
    lease: Optional[LeaseSnapshot] = None


def default_utc_clock() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryCoordinationBackend:
    """Reference in-memory coordination backend with thread-safe atomic critical section."""

    def __init__(self, clock_provider: Optional[Callable[[], datetime]] = None) -> None:
        self._clock_provider = clock_provider or default_utc_clock
        self._lock = threading.Lock()
        # worker_id -> WorkerIdentity
        self._registered_workers: Dict[str, WorkerIdentity] = {}
        # task_id -> current LeaseSnapshot
        self._leases: Dict[str, LeaseSnapshot] = {}
        # task_id -> last_generation
        self._task_generations: Dict[str, int] = {}
        # sequence counter for unique lease_id generation
        self._lease_counter = 0

    def _now(self) -> datetime:
        now = self._clock_provider()
        if not isinstance(now, datetime):
            raise InvalidClockError("Clock provider must return a datetime instance")
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            raise InvalidClockError("Clock provider returned naive datetime; timezone-aware required")
        return now

    def register_worker(self, identity: WorkerIdentity) -> None:
        if not isinstance(identity, WorkerIdentity):
            raise InvalidIdentityError("identity must be a WorkerIdentity instance")

        with self._lock:
            existing = self._registered_workers.get(identity.worker_id)
            if existing is not None:
                if existing == identity:
                    # Exact duplicate registration is idempotent
                    return
                else:
                    # Contradictory registration for same worker_id fails closed
                    raise InvalidIdentityError(
                        f"Contradictory registration for worker_id {identity.worker_id}"
                    )
            self._registered_workers[identity.worker_id] = identity

    def is_worker_registered(self, worker_id: str, session_id: str) -> bool:
        with self._lock:
            reg = self._registered_workers.get(worker_id)
            return reg is not None and reg.session_id == session_id

    def try_claim(self, task_id: str, identity: WorkerIdentity, ttl_seconds: float) -> ClaimResult:
        if not isinstance(task_id, str) or not task_id.strip():
            raise InvalidInputError("task_id must be a non-empty string")
        if not isinstance(identity, WorkerIdentity):
            raise InvalidIdentityError("identity must be a WorkerIdentity instance")
        if not isinstance(ttl_seconds, (int, float)) or ttl_seconds <= 0:
            raise InvalidInputError("ttl_seconds must be a positive number")

        task_id = task_id.strip()

        with self._lock:
            # Require registered worker identity
            registered = self._registered_workers.get(identity.worker_id)
            if registered is None or registered != identity:
                raise InvalidIdentityError("Worker identity must be registered prior to claiming tasks")

            now = self._now()
            current_lease = self._leases.get(task_id)

            # Check existing active ownership
            if current_lease is not None:
                is_expired = now >= current_lease.expires_at
                if current_lease.status == LeaseStatus.ACTIVE and not is_expired:
                    # Active lease exists
                    if (
                        current_lease.worker_id == identity.worker_id
                        and current_lease.session_id == identity.session_id
                    ):
                        # CASE D: Same exact current owner already owns active lease
                        return ClaimResult(
                            disposition=ClaimDisposition.ALREADY_OWNED,
                            lease=current_lease,
                        )
                    else:
                        # CASE E: Another active worker/session owns it
                        return ClaimResult(
                            disposition=ClaimDisposition.HELD_BY_OTHER,
                            lease=current_lease,
                        )

            # CASE A, B, C: Unowned, Expired, or Released -> Acquire new epoch
            next_gen = self._task_generations.get(task_id, 0) + 1
            self._lease_counter += 1
            new_lease_id = f"lease-{task_id}-{next_gen}-{self._lease_counter}"

            expires_at = datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=timezone.utc)

            new_lease = LeaseSnapshot(
                task_id=task_id,
                worker_id=identity.worker_id,
                session_id=identity.session_id,
                lease_id=new_lease_id,
                acquired_at=now,
                last_heartbeat_at=now,
                expires_at=expires_at,
                generation=next_gen,
                status=LeaseStatus.ACTIVE,
            )

            self._task_generations[task_id] = next_gen
            self._leases[task_id] = new_lease

            return ClaimResult(
                disposition=ClaimDisposition.ACQUIRED,
                lease=new_lease,
            )

    def heartbeat(
        self,
        task_id: str,
        worker_id: str,
        session_id: str,
        lease_id: str,
        generation: int,
        ttl_seconds: float,
    ) -> Optional[LeaseSnapshot]:
        if not isinstance(task_id, str) or not task_id.strip():
            raise InvalidInputError("task_id must be a non-empty string")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise InvalidInputError("worker_id must be a non-empty string")
        if not isinstance(session_id, str) or not session_id.strip():
            raise InvalidInputError("session_id must be a non-empty string")
        if not isinstance(lease_id, str) or not lease_id.strip():
            raise InvalidInputError("lease_id must be a non-empty string")
        if not isinstance(generation, int) or generation <= 0:
            raise InvalidInputError("generation must be a positive integer")
        if not isinstance(ttl_seconds, (int, float)) or ttl_seconds <= 0:
            raise InvalidInputError("ttl_seconds must be a positive number")

        task_id = task_id.strip()

        with self._lock:
            now = self._now()
            current = self._leases.get(task_id)

            if current is None:
                return None

            # Verify exact current ownership match & status
            if (
                current.worker_id != worker_id.strip()
                or current.session_id != session_id.strip()
                or current.lease_id != lease_id.strip()
                or current.generation != generation
                or current.status != LeaseStatus.ACTIVE
            ):
                return None

            # Check exact expiry boundary: now >= expires_at means expired
            if now >= current.expires_at:
                # Expired lease heartbeat must fail and cannot resurrect lease
                # Mark status as EXPIRED if not already
                updated_expired = LeaseSnapshot(
                    task_id=current.task_id,
                    worker_id=current.worker_id,
                    session_id=current.session_id,
                    lease_id=current.lease_id,
                    acquired_at=current.acquired_at,
                    last_heartbeat_at=current.last_heartbeat_at,
                    expires_at=current.expires_at,
                    generation=current.generation,
                    status=LeaseStatus.EXPIRED,
                )
                self._leases[task_id] = updated_expired
                return None

            # Successful heartbeat: update last_heartbeat_at and expires_at
            new_expires_at = datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=timezone.utc)
            updated_lease = LeaseSnapshot(
                task_id=current.task_id,
                worker_id=current.worker_id,
                session_id=current.session_id,
                lease_id=current.lease_id,
                acquired_at=current.acquired_at,
                last_heartbeat_at=now,
                expires_at=new_expires_at,
                generation=current.generation,
                status=LeaseStatus.ACTIVE,
            )
            self._leases[task_id] = updated_lease
            return updated_lease

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
        if not isinstance(generation, int) or generation <= 0:
            raise InvalidInputError("generation must be a positive integer")

        task_id = task_id.strip()

        with self._lock:
            current = self._leases.get(task_id)
            if current is None:
                return False

            # Verify exact current active owner match
            if (
                current.worker_id != worker_id.strip()
                or current.session_id != session_id.strip()
                or current.lease_id != lease_id.strip()
                or current.generation != generation
                or current.status != LeaseStatus.ACTIVE
            ):
                return False

            released_lease = LeaseSnapshot(
                task_id=current.task_id,
                worker_id=current.worker_id,
                session_id=current.session_id,
                lease_id=current.lease_id,
                acquired_at=current.acquired_at,
                last_heartbeat_at=current.last_heartbeat_at,
                expires_at=current.expires_at,
                generation=current.generation,
                status=LeaseStatus.RELEASED,
            )
            self._leases[task_id] = released_lease
            return True

    def get_lease(self, task_id: str) -> Optional[LeaseSnapshot]:
        with self._lock:
            return self._leases.get(task_id)
