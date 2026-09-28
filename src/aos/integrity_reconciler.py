"""Durable cross-lineage execution integrity guard and reconciler.

The guard is deliberately provider-neutral.  It binds accepted work and active
exclusive write scopes to project, workspace generation, command lineage, and a
semantic task signature before a backend may mutate a workspace.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from aos.runtime_store import atomic_json, read_json


OUTCOME_BUCKETS = (
    "SUCCESS",
    "RETRYABLE_PARTIAL",
    "RESOURCE_WAIT",
    "HUMAN_HOLD",
    "TERMINAL_FAILURE",
    "REPLAN_NOOP",
)


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(_canonical(dict(value)) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _normalize_scope(scope: str) -> str:
    value = str(scope).replace("\\", "/").strip().strip("/").casefold()
    while "//" in value:
        value = value.replace("//", "/")
    return value or "."


def scopes_overlap(left: Iterable[str], right: Iterable[str]) -> bool:
    """Return True when two path scopes overlap by equality or ancestry."""
    left_values = [_normalize_scope(item) for item in left]
    right_values = [_normalize_scope(item) for item in right]
    for a in left_values:
        for b in right_values:
            if a == "." or b == "." or a == b:
                return True
            if a.startswith(b + "/") or b.startswith(a + "/"):
                return True
    return False


def discover_integrity_root(runtime_path: Path) -> Path:
    """Resolve one shared integrity root for every command in a runtime store."""
    resolved = runtime_path.expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name.casefold() == "commands":
            return candidate.parent / "integrity"
    return resolved / "integrity"


def semantic_task_signature(request: Any) -> str:
    source_sha = str(getattr(request, "payload", {}).get("source_sha") or "UNKNOWN").lower()
    payload = {
        "project_identity": str(getattr(request, "project_id", "")),
        "canonical_revision": source_sha,
        "operation_class": str(getattr(request, "operation_class", "")).upper(),
        "payload": dict(getattr(request, "payload", {}) or {}),
        "write_scope": sorted(_normalize_scope(item) for item in getattr(request, "write_scope", []) or []),
        "expected_artifacts": sorted(
            _normalize_scope(item) for item in getattr(request, "expected_artifacts", []) or []
        ),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IntegrityClaim:
    disposition: str
    lease_id: Optional[str]
    task_signature: str
    reason: Optional[str] = None
    accepted_record: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class IntegrityReport:
    evidence_status: str
    duplicate_completed_work: int | str
    lost_accepted_work: int | str
    cross_lane_write_scope_collision: int | str
    findings: tuple[Dict[str, Any], ...]
    outcome_counts: Dict[str, int]
    total_executed: int
    outcome_partition_valid: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class IntegrityReconciler:
    """Owns durable claims, accepted-work dedupe, and integrity calculation."""

    def __init__(
        self,
        root: Path,
        *,
        lease_ttl_seconds: int = 900,
        initialize: bool = True,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.leases_dir = self.root / "leases"
        self.accepted_path = self.root / "accepted-work.jsonl"
        self.outcomes_path = self.root / "execution-outcomes.jsonl"
        self.marker_path = self.root / "instrumentation.json"
        self.lock_path = self.root / ".integrity.lock"
        self.lease_ttl_seconds = int(lease_ttl_seconds)
        if initialize:
            self.leases_dir.mkdir(parents=True, exist_ok=True)
        if initialize and not self.marker_path.exists():
            atomic_json(self.marker_path, {
                "schema_version": "1.0.0",
                "status": "FULLY_INSTRUMENTED",
                "created_at": _utc_now(),
            })

    def _lock(self, timeout_seconds: float = 5.0):
        reconciler = self

        class _Lock:
            def __enter__(self) -> None:
                deadline = time.monotonic() + timeout_seconds
                while True:
                    try:
                        fd = os.open(
                            reconciler.lock_path,
                            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        )
                        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
                        os.close(fd)
                        return None
                    except FileExistsError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("INTEGRITY_LOCK_TIMEOUT")
                        time.sleep(0.02)

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                try:
                    reconciler.lock_path.unlink()
                except FileNotFoundError:
                    pass

        return _Lock()

    @staticmethod
    def _workspace_identity(request: Any) -> str:
        return hashlib.sha256(
            str(Path(getattr(request, "workspace", "")).expanduser().resolve())
            .casefold()
            .encode("utf-8")
        ).hexdigest()

    def _records(self, path: Path) -> tuple[list[Dict[str, Any]], bool]:
        if not path.is_file():
            return [], True
        records: list[Dict[str, Any]] = []
        valid = True
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("record must be object")
                records.append(value)
            except (ValueError, json.JSONDecodeError):
                valid = False
        return records, valid

    def _accepted_for(self, request: Any, signature: str) -> Optional[Dict[str, Any]]:
        records, valid = self._records(self.accepted_path)
        if not valid:
            return None
        workspace_identity = self._workspace_identity(request)
        for record in reversed(records):
            if (
                record.get("event") == "ACCEPTED"
                and record.get("task_signature") == signature
                and record.get("project_identity") == getattr(request, "project_id", None)
                and record.get("workspace_identity") == workspace_identity
            ):
                return record
        return None

    def begin(self, request: Any, *, command_lineage: str, execution_generation: int = 0) -> IntegrityClaim:
        signature = semantic_task_signature(request)
        with self._lock():
            accepted = self._accepted_for(request, signature)
            if accepted is not None:
                return IntegrityClaim("ALREADY_ACCEPTED", None, signature, accepted_record=accepted)

            now = time.time()
            workspace_identity = self._workspace_identity(request)
            requested_scope = list(getattr(request, "write_scope", []) or [])
            for path in self.leases_dir.glob("*.json"):
                lease = read_json(path, {})
                if lease.get("state") != "ACTIVE":
                    continue
                if float(lease.get("expires_at_epoch", 0) or 0) <= now:
                    lease["state"] = "EXPIRED"
                    lease["released_at"] = _utc_now()
                    atomic_json(path, lease)
                    continue
                if lease.get("workspace_identity") != workspace_identity:
                    continue
                if scopes_overlap(requested_scope, lease.get("write_scope", [])):
                    return IntegrityClaim(
                        "HOLD",
                        None,
                        signature,
                        reason=f"CROSS_LANE_WRITE_SCOPE_COLLISION:{lease.get('lease_id')}",
                    )

            lease_id = f"integrity-{uuid.uuid4().hex}"
            atomic_json(self.leases_dir / f"{lease_id}.json", {
                "schema_version": "1.0.0",
                "lease_id": lease_id,
                "state": "ACTIVE",
                "project_identity": str(getattr(request, "project_id", "")),
                "command_lineage": command_lineage,
                "task_id": str(getattr(request, "task_id", "")),
                "task_signature": signature,
                "workspace_identity": workspace_identity,
                "workspace": str(getattr(request, "workspace", "")),
                "write_scope": sorted(_normalize_scope(item) for item in requested_scope),
                "execution_generation": int(execution_generation),
                "acquired_at": _utc_now(),
                "expires_at_epoch": now + self.lease_ttl_seconds,
            })
            return IntegrityClaim("CLAIMED", lease_id, signature)

    def _release_lease(self, claim: IntegrityClaim, state: str) -> None:
        if not claim.lease_id:
            return
        path = self.leases_dir / f"{claim.lease_id}.json"
        lease = read_json(path, {})
        if lease:
            lease["state"] = state
            lease["released_at"] = _utc_now()
            atomic_json(path, lease)

    def record_execution(
        self,
        execution_id: str,
        *,
        task_signature: str,
        task_id: str,
    ) -> None:
        execution_id = str(execution_id or "").strip()
        if not execution_id:
            raise ValueError("execution_id is required")
        _append_jsonl(self.outcomes_path, {
            "schema_version": "1.1.0",
            "event": "EXECUTION_STARTED",
            "timestamp": _utc_now(),
            "execution_id": execution_id,
            "task_signature": task_signature,
            "task_id": task_id,
        })

    def record_outcome(
        self,
        bucket: str,
        *,
        execution_id: str,
        task_signature: str,
        task_id: str,
    ) -> None:
        if bucket not in OUTCOME_BUCKETS:
            raise ValueError(f"Unknown execution outcome bucket: {bucket}")
        execution_id = str(execution_id or "").strip()
        if not execution_id:
            raise ValueError("execution_id is required")
        _append_jsonl(self.outcomes_path, {
            "schema_version": "1.1.0",
            "event": "EXECUTION_OUTCOME",
            "timestamp": _utc_now(),
            "execution_id": execution_id,
            "bucket": bucket,
            "task_signature": task_signature,
            "task_id": task_id,
        })

    def accept(
        self,
        claim: IntegrityClaim,
        request: Any,
        result: Any,
        *,
        command_lineage: str,
        checkpoint_path: Optional[str] = None,
    ) -> None:
        record = {
            "schema_version": "1.0.0",
            "event": "ACCEPTED",
            "accepted_at": _utc_now(),
            "project_identity": str(getattr(request, "project_id", "")),
            "command_lineage": command_lineage,
            "task_id": str(getattr(request, "task_id", "")),
            "task_signature": claim.task_signature,
            "workspace_identity": self._workspace_identity(request),
            "workspace": str(getattr(request, "workspace", "")),
            "checkpoint_id": str(getattr(request, "payload", {}).get("checkpoint_id") or getattr(request, "request_id", "")),
            "checkpoint_path": checkpoint_path,
            "expected_artifacts": list(getattr(request, "expected_artifacts", []) or []),
            "artifact_hashes": dict(getattr(result, "artifact_hashes", {}) or {}),
        }
        with self._lock():
            execution_id = str(getattr(request, "request_id", "") or "").strip()
            self.record_execution(
                execution_id,
                task_signature=claim.task_signature,
                task_id=record["task_id"],
            )
            _append_jsonl(self.accepted_path, record)
            self._release_lease(claim, "ACCEPTED")
            self.record_outcome(
                "SUCCESS",
                execution_id=execution_id,
                task_signature=claim.task_signature,
                task_id=record["task_id"],
            )

    def release(
        self,
        claim: IntegrityClaim,
        *,
        bucket: str,
        execution_id: str,
        task_id: str,
    ) -> None:
        with self._lock():
            self.record_execution(
                execution_id,
                task_signature=claim.task_signature,
                task_id=task_id,
            )
            self._release_lease(claim, bucket)
            self.record_outcome(
                bucket,
                execution_id=execution_id,
                task_signature=claim.task_signature,
                task_id=task_id,
            )

    def reconcile(self) -> IntegrityReport:
        instrumented = read_json(self.marker_path, {}).get("status") == "FULLY_INSTRUMENTED"
        accepted, accepted_valid = self._records(self.accepted_path)
        outcomes, outcomes_valid = self._records(self.outcomes_path)
        findings: list[Dict[str, Any]] = []

        duplicate_count = 0
        seen: set[tuple[str, str, str]] = set()
        for record in accepted:
            if record.get("event") != "ACCEPTED":
                continue
            key = (
                str(record.get("project_identity")),
                str(record.get("workspace_identity")),
                str(record.get("task_signature")),
            )
            if key in seen:
                duplicate_count += 1
                findings.append({"type": "DUPLICATE_COMPLETED_WORK", "key": list(key)})
            seen.add(key)

        lost_count = 0
        for record in accepted:
            if record.get("event") != "ACCEPTED":
                continue
            missing: list[str] = []
            checkpoint_path = record.get("checkpoint_path")
            if checkpoint_path and not Path(str(checkpoint_path)).is_file():
                missing.append(str(checkpoint_path))
            workspace = Path(str(record.get("workspace") or "."))
            for relative in record.get("expected_artifacts", []) or []:
                target = workspace / str(relative)
                if not target.is_file():
                    missing.append(str(relative))
            if missing:
                lost_count += 1
                findings.append({
                    "type": "LOST_ACCEPTED_WORK",
                    "task_signature": record.get("task_signature"),
                    "missing": missing,
                })

        active_leases: list[Dict[str, Any]] = []
        now = time.time()
        for path in self.leases_dir.glob("*.json") if self.leases_dir.is_dir() else ():
            lease = read_json(path, {})
            if lease.get("state") == "ACTIVE" and float(lease.get("expires_at_epoch", 0) or 0) > now:
                active_leases.append(lease)
        collision_count = 0
        for index, left in enumerate(active_leases):
            for right in active_leases[index + 1:]:
                if (
                    left.get("workspace_identity") == right.get("workspace_identity")
                    and left.get("lease_id") != right.get("lease_id")
                    and scopes_overlap(left.get("write_scope", []), right.get("write_scope", []))
                ):
                    collision_count += 1
                    findings.append({
                        "type": "CROSS_LANE_WRITE_SCOPE_COLLISION",
                        "lease_ids": [left.get("lease_id"), right.get("lease_id")],
                    })

        counts = {bucket: 0 for bucket in OUTCOME_BUCKETS}
        known_execution_ids: set[str] = set()
        outcomes_by_execution: Dict[str, set[str]] = {}
        outcome_findings: list[Dict[str, Any]] = []
        for record in outcomes:
            event = record.get("event")
            execution_id = str(record.get("execution_id") or "").strip()
            if event == "EXECUTION_STARTED":
                if not execution_id:
                    outcome_findings.append({
                        "type": "INVALID_EXECUTION_ID",
                        "event": event,
                    })
                    continue
                known_execution_ids.add(execution_id)
                continue
            if event != "EXECUTION_OUTCOME":
                outcome_findings.append({
                    "type": "INVALID_OUTCOME_EVENT",
                    "event": event,
                })
                continue
            if not execution_id:
                outcome_findings.append({
                    "type": "INVALID_EXECUTION_ID",
                    "event": event,
                })
                continue
            bucket = str(record.get("bucket") or "")
            if bucket not in counts:
                outcome_findings.append({
                    "type": "INVALID_OUTCOME_BUCKET",
                    "execution_id": execution_id,
                    "bucket": bucket or None,
                })
                continue
            outcomes_by_execution.setdefault(execution_id, set()).add(bucket)

        for execution_id in sorted(outcomes_by_execution):
            if execution_id not in known_execution_ids:
                outcome_findings.append({
                    "type": "OUTCOME_WITHOUT_EXECUTION",
                    "execution_id": execution_id,
                })

        for execution_id in sorted(known_execution_ids):
            classifications = outcomes_by_execution.get(execution_id, set())
            if not classifications:
                outcome_findings.append({
                    "type": "MISSING_OUTCOME_CLASSIFICATION",
                    "execution_id": execution_id,
                })
            elif len(classifications) > 1:
                outcome_findings.append({
                    "type": "DOUBLE_OUTCOME_CLASSIFICATION",
                    "execution_id": execution_id,
                    "buckets": sorted(classifications),
                })
            else:
                counts[next(iter(classifications))] += 1

        if not outcomes_valid:
            outcome_findings.append({"type": "MALFORMED_OUTCOME_LOG"})
        findings.extend(outcome_findings)
        total = len(known_execution_ids)
        evidence_valid = instrumented and accepted_valid and outcomes_valid
        concrete_or_unknown = lambda value: value if evidence_valid else "UNKNOWN"
        return IntegrityReport(
            evidence_status="SUFFICIENT" if evidence_valid else "INSUFFICIENT",
            duplicate_completed_work=concrete_or_unknown(duplicate_count),
            lost_accepted_work=concrete_or_unknown(lost_count),
            cross_lane_write_scope_collision=concrete_or_unknown(collision_count),
            findings=tuple(findings),
            outcome_counts=counts,
            total_executed=total,
            outcome_partition_valid=(
                evidence_valid
                and not outcome_findings
                and sum(counts.values()) == total
            ),
        )
