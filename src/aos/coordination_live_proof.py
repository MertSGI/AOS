"""AOS-5 Hosted Multi-Machine Proof Harness and Live Authorization Boundary."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from jsonschema import Draft202012Validator, FormatChecker

from aos.coordination import (
    ClaimDisposition,
    ClaimResult,
    CoordinationStorageError,
    LeaseSnapshot,
    LeaseStatus,
    WorkerIdentity,
)
from aos.validate import DuplicateJSONKeyError, load_json_strict, loads_json_strict

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schemas" / "v0.1" / "aos5_multi_machine_proof.schema.json"

FORBIDDEN_SECRET_KEYS = {
    "dsn",
    "database_url",
    "password",
    "secret",
    "token",
    "credential",
    "api_key",
}

START_STALE_TOLERANCE_SECONDS = 5.0
MAX_START_WAIT_SECONDS = 120.0
PEER_REGISTRATION_TIMEOUT_SECONDS = 30.0
RECOVERY_GRACE_SECONDS = 15.0


def load_proof_schema() -> Dict[str, Any]:
    if not SCHEMA_PATH.is_file():
        raise FileNotFoundError(f"Proof schema file not found: {SCHEMA_PATH}")
    return load_json_strict(SCHEMA_PATH)


def validate_proof_artifact(data: Any) -> Tuple[bool, List[str]]:
    schema = load_proof_schema()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = [err.message for err in validator.iter_errors(data)]
    return len(errors) == 0, errors


def check_forbidden_secret_keys(obj: Any, path: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in FORBIDDEN_SECRET_KEYS:
                raise ValueError(f"Forbidden secret-like key '{k}' found at path '{path}.{k}'")
            check_forbidden_secret_keys(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            check_forbidden_secret_keys(item, f"{path}[{idx}]")


def parse_utc_datetime(iso_str: str) -> datetime.datetime:
    try:
        dt = datetime.datetime.fromisoformat(iso_str)
    except Exception as e:
        raise ValueError(f"Invalid ISO datetime string: '{iso_str}'") from e

    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"Datetime must be UTC-aware (found naive datetime): '{iso_str}'")

    return dt.astimezone(datetime.timezone.utc)


def _read_linux_boot_id() -> str:
    path = Path("/proc/sys/kernel/random/boot_id")
    if not path.is_file():
        raise CoordinationStorageError("Fail closed: /proc/sys/kernel/random/boot_id does not exist")
    try:
        content = path.read_text(encoding="utf-8").strip()
    except Exception as e:
        raise CoordinationStorageError("Fail closed: unreadable Linux boot_id") from e

    if not content:
        raise CoordinationStorageError("Fail closed: empty Linux boot_id")

    try:
        parsed_uuid = uuid.UUID(content)
    except Exception as e:
        raise CoordinationStorageError("Fail closed: malformed Linux boot_id") from e

    return str(parsed_uuid).lower()


def compute_proof_scoped_machine_fingerprint(proof_id: str) -> str:
    sys_name = platform.system().strip()
    mach = platform.machine().strip()
    if not sys_name or not mach:
        raise CoordinationStorageError("Fail closed: missing local machine identity material for fingerprint")

    if sys_name.lower() == "linux":
        boot_id = _read_linux_boot_id()
        raw_identity = f"{proof_id}:{sys_name}:{mach}:{boot_id}"
    else:
        node = platform.node().strip()
        if not node:
            raise CoordinationStorageError("Fail closed: missing local machine identity material for fingerprint")
        raw_identity = f"{proof_id}:{node}:{sys_name}:{mach}"

    return hashlib.sha256(raw_identity.encode("utf-8")).hexdigest()


def check_git_readiness(required_sha: str, required_branch: str) -> Dict[str, Any]:
    try:
        head_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.PIPE
        ).strip()
    except Exception as e:
        raise RuntimeError(f"Git readiness check failed reading HEAD: {e}") from e

    try:
        curr_branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True, stderr=subprocess.PIPE
        ).strip()
    except Exception as e:
        raise RuntimeError(f"Git readiness check failed reading branch: {e}") from e

    try:
        origin_sha = subprocess.check_output(
            ["git", "rev-parse", f"origin/{required_branch}"], text=True, stderr=subprocess.PIPE
        ).strip()
    except Exception as e:
        raise RuntimeError(f"Git readiness check failed reading origin/{required_branch}: {e}") from e

    try:
        status_out = subprocess.check_output(
            ["git", "status", "--porcelain"], text=True, stderr=subprocess.PIPE
        ).strip()
        is_clean = (status_out == "")
    except Exception as e:
        raise RuntimeError(f"Git readiness check failed checking status: {e}") from e

    reasons = []
    if curr_branch != required_branch:
        reasons.append(f"Current branch '{curr_branch}' does not match required '{required_branch}'")
    if head_sha != required_sha:
        reasons.append(f"HEAD SHA '{head_sha}' does not match required '{required_sha}'")
    if origin_sha != required_sha:
        reasons.append(f"origin/{required_branch} SHA '{origin_sha}' does not match required '{required_sha}'")
    if not is_clean:
        reasons.append("Working tree contains uncommitted changes")

    is_ready = len(reasons) == 0
    return {
        "is_ready": is_ready,
        "head_sha": head_sha,
        "origin_sha": origin_sha,
        "branch": curr_branch,
        "is_clean": is_clean,
        "reasons": reasons,
    }


def validate_proof_request_dict(req: Dict[str, Any]) -> Dict[str, Any]:
    check_forbidden_secret_keys(req)

    if "start_at_utc" in req and isinstance(req["start_at_utc"], str):
        parse_utc_datetime(req["start_at_utc"])

    is_valid, errors = validate_proof_artifact(req)
    if not is_valid:
        raise ValueError(f"Proof request failed schema validation: {errors}")

    if req.get("schema_version") != "0.1.0":
        raise ValueError(f"Invalid schema_version: {req.get('schema_version')}")
    if req.get("artifact_type") != "AOS5_MULTI_MACHINE_PROOF_REQUEST":
        raise ValueError(f"Invalid artifact_type: {req.get('artifact_type')}")
    if req.get("gate") != "AOS-5":
        raise ValueError(f"Invalid gate: {req.get('gate')}")
    if req.get("backend_kind") != "POSTGRES":
        raise ValueError(f"Invalid backend_kind: {req.get('backend_kind')}")
    if req.get("environment_class") != "NONPRODUCTION_HOSTED_TEST":
        raise ValueError(f"Invalid environment_class: {req.get('environment_class')}")
    if req.get("control_branch") != "feature/aos-5-distributed-coordination":
        raise ValueError(f"Invalid control_branch: {req.get('control_branch')}")

    source_sha = req.get("source_sha", "")
    if len(source_sha) != 40 or not all(c in "0123456789abcdef" for c in source_sha):
        raise ValueError(f"Invalid source_sha (must be 40 lowercase hex): {source_sha}")

    parse_utc_datetime(req["start_at_utc"])

    if req.get("expected_worker_count") != 2:
        raise ValueError("expected_worker_count must be 2")

    workers = req.get("workers", [])
    if len(workers) != 2:
        raise ValueError(f"workers array must contain exactly 2 entries, found {len(workers)}")

    roles = [w.get("role") for w in workers]
    if sorted(roles) != ["worker_a", "worker_b"]:
        raise ValueError(f"workers must have unique roles ['worker_a', 'worker_b'], found {roles}")

    identities = [(w.get("worker_id"), w.get("session_id")) for w in workers]
    if len(set(identities)) != 2:
        raise ValueError(f"workers must have unique worker_id/session_id pairs, found {identities}")

    labels = [w.get("machine_label") for w in workers]
    if len(set(labels)) != 2:
        raise ValueError(f"workers must have unique machine_label, found {labels}")

    if req.get("production_mutation_allowed") is not False:
        raise ValueError("production_mutation_allowed must be false")
    if req.get("destructive_operations_allowed") is not False:
        raise ValueError("destructive_operations_allowed must be false")
    if req.get("billing_activation_allowed") is not False:
        raise ValueError("billing_activation_allowed must be false")

    return req


def load_and_validate_request(request_file_path: str | Path) -> Dict[str, Any]:
    data = load_json_strict(request_file_path)
    return validate_proof_request_dict(data)


def execute_dry_run(request_dict: Dict[str, Any], role: str) -> Dict[str, Any]:
    validate_proof_request_dict(request_dict)

    roles = [w["role"] for w in request_dict["workers"]]
    if role not in roles:
        raise ValueError(f"Specified role '{role}' not in request workers roles {roles}")

    git_info = check_git_readiness(
        required_sha=request_dict["source_sha"],
        required_branch=request_dict["control_branch"],
    )

    if not git_info["is_ready"]:
        raise ValueError(f"Git readiness check failed during dry-run: {git_info['reasons']}")

    is_authorized = request_dict.get("authorized", False)
    status = (
        "DRY_RUN_SUCCESS_AUTHORIZED"
        if is_authorized
        else "READY_FOR_EXPLICIT_HUMAN_LIVE_AUTHORIZATION"
    )

    return {
        "status": status,
        "role": role,
        "authorized": is_authorized,
        "git_readiness": git_info,
        "backend_calls": 0,
        "network_calls": 0,
    }


def write_worker_result_atomic(result_dict: Dict[str, Any], output_path: str | Path, live_dsn: Optional[str] = None) -> None:
    check_forbidden_secret_keys(result_dict, live_dsn)

    is_valid, errors = validate_proof_artifact(result_dict)
    if not is_valid:
        raise ValueError(f"Worker result failed schema validation: {errors}")

    out_file = Path(output_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = out_file.with_suffix(f".tmp.{os.getpid()}")

    content = json.dumps(result_dict, indent=2) + "\n"
    with open(temp_file, "w", encoding="utf-8") as f:
        f.write(content)

    temp_file.replace(out_file)


def _serialize_lease_snapshot(lease: LeaseSnapshot) -> Dict[str, Any]:
    if not isinstance(lease, LeaseSnapshot):
        raise CoordinationStorageError(f"Expected canonical LeaseSnapshot, got {type(lease).__name__}")

    if isinstance(lease.acquired_at, datetime.datetime):
        acq_at = lease.acquired_at.isoformat()
    else:
        acq_at = str(lease.acquired_at)

    if isinstance(lease.expires_at, datetime.datetime):
        exp_at = lease.expires_at.isoformat()
    else:
        exp_at = str(lease.expires_at)

    status_str = lease.status.value if hasattr(lease.status, "value") else str(lease.status)

    return {
        "owner_worker_id": str(lease.worker_id),
        "owner_session_id": str(lease.session_id),
        "lease_id": str(lease.lease_id),
        "generation": int(lease.generation),
        "acquired_at": acq_at,
        "expires_at": exp_at,
        "ttl_seconds": float(lease.ttl_seconds),
        "status": status_str,
    }


def run_live_worker(
    request_dict: Dict[str, Any],
    role: str,
    output_path: Optional[str | Path] = None,
    backend_override: Any = None,
    time_func: Optional[Callable[[], float]] = None,
    monotonic_func: Optional[Callable[[], float]] = None,
    sleep_func: Optional[Callable[[float], None]] = None,
) -> Dict[str, Any]:
    validate_proof_request_dict(request_dict)

    if not request_dict.get("authorized", False):
        raise CoordinationStorageError("Live proof execution HOLD: request.authorized is false")

    _time = time_func or time.time
    _monotonic = monotonic_func or time.monotonic
    _sleep = sleep_func or time.sleep

    start_dt = parse_utc_datetime(request_dict["start_at_utc"])
    start_ts = start_dt.timestamp()
    now_ts = _time()
    start_delay = start_ts - now_ts

    if start_delay < -START_STALE_TOLERANCE_SECONDS:
        raise CoordinationStorageError(
            f"Live proof execution HOLD: Request start_at_utc is stale ({start_delay:.1f}s < -{START_STALE_TOLERANCE_SECONDS}s)"
        )
    if start_delay > MAX_START_WAIT_SECONDS:
        raise CoordinationStorageError(
            f"Live proof execution HOLD: Request start_at_utc is too far in future ({start_delay:.1f}s > {MAX_START_WAIT_SECONDS}s)"
        )

    git_info = check_git_readiness(
        required_sha=request_dict["source_sha"],
        required_branch=request_dict["control_branch"],
    )
    if not git_info["is_ready"]:
        raise CoordinationStorageError(f"Live proof execution HOLD: Git readiness failed: {git_info['reasons']}")

    matching_workers = [w for w in request_dict["workers"] if w["role"] == role]
    if not matching_workers:
        raise ValueError(f"Role '{role}' not found in request workers")
    current_worker = matching_workers[0]

    peer_workers = [w for w in request_dict["workers"] if w["role"] != role]
    peer_worker = peer_workers[0]

    live_dsn = os.environ.get("AOS_POSTGRES_LIVE_DSN")
    if backend_override is None and not live_dsn:
        raise CoordinationStorageError("Live proof execution HOLD: AOS_POSTGRES_LIVE_DSN environment variable missing")

    if backend_override is not None:
        backend = backend_override
    else:
        from aos.coordination_postgres import PostgresCoordinationBackend
        try:
            backend = PostgresCoordinationBackend(
                dsn=live_dsn,
                namespace_id=request_dict["namespace_id"],
            )
        except Exception as e:
            raise CoordinationStorageError("Failed to connect to PostgreSQL coordination backend") from None

    fingerprint = compute_proof_scoped_machine_fingerprint(request_dict["proof_id"])

    curr_identity = WorkerIdentity(
        worker_id=current_worker["worker_id"],
        session_id=current_worker["session_id"],
    )
    peer_identity = WorkerIdentity(
        worker_id=peer_worker["worker_id"],
        session_id=peer_worker["session_id"],
    )

    backend.register_worker(curr_identity)

    # Bounded wait for peer worker registration
    peer_registered = False
    peer_wait_deadline = _monotonic() + PEER_REGISTRATION_TIMEOUT_SECONDS
    while _monotonic() < peer_wait_deadline:
        if backend.is_worker_registered(peer_identity.worker_id, peer_identity.session_id):
            peer_registered = True
            break
        _sleep(0.05)

    if not peer_registered:
        raise CoordinationStorageError("Live proof execution HOLD: Peer worker registration not observed within monotonic timeout")

    # Bounded wait until start_at_utc if in future
    if start_delay > 0:
        start_wait_deadline = _monotonic() + start_delay + 1.0
        while _time() < start_ts:
            if _monotonic() > start_wait_deadline:
                raise CoordinationStorageError("Live proof execution HOLD: Timed out waiting for start_at_utc")
            _sleep(0.05)

    claim_start_dt = datetime.datetime.fromtimestamp(_time(), tz=datetime.timezone.utc)
    claim_start_iso = claim_start_dt.isoformat()

    try:
        initial_claim = backend.try_claim(
            task_id=request_dict["task_id"],
            identity=curr_identity,
            ttl_seconds=request_dict["ttl_seconds"],
        )
    except Exception as e:
        raise CoordinationStorageError("Failed to execute try_claim during live proof") from None

    claim_end_dt = datetime.datetime.fromtimestamp(_time(), tz=datetime.timezone.utc)
    claim_end_iso = claim_end_dt.isoformat()

    if not isinstance(initial_claim, ClaimResult):
        raise CoordinationStorageError("Claim result is not a canonical ClaimResult instance")

    disposition = initial_claim.disposition
    if disposition not in (ClaimDisposition.ACQUIRED, ClaimDisposition.HELD_BY_OTHER):
        raise CoordinationStorageError(f"Unexpected initial claim disposition: {disposition}")

    obs_lease_snapshot = initial_claim.lease
    if not isinstance(obs_lease_snapshot, LeaseSnapshot):
        raise CoordinationStorageError("Initial claim lease is not a canonical LeaseSnapshot instance")

    if obs_lease_snapshot.status != LeaseStatus.ACTIVE:
        raise CoordinationStorageError(f"Initial lease status must be ACTIVE, found {obs_lease_snapshot.status}")

    if not math.isfinite(obs_lease_snapshot.ttl_seconds) or obs_lease_snapshot.ttl_seconds <= 0:
        raise CoordinationStorageError("Initial lease ttl_seconds must be finite and positive")

    obs_lease_dict = _serialize_lease_snapshot(obs_lease_snapshot)

    if disposition == ClaimDisposition.ACQUIRED:
        if obs_lease_snapshot.worker_id != current_worker["worker_id"] or obs_lease_snapshot.session_id != current_worker["session_id"]:
            raise CoordinationStorageError("ACQUIRED initial lease owner does not match current worker")

        result = {
            "schema_version": "0.1.0",
            "artifact_type": "AOS5_MULTI_MACHINE_WORKER_RESULT",
            "gate": "AOS-5",
            "proof_id": request_dict["proof_id"],
            "source_sha": request_dict["source_sha"],
            "control_branch": request_dict["control_branch"],
            "namespace_id": request_dict["namespace_id"],
            "task_id": request_dict["task_id"],
            "role": role,
            "worker_id": current_worker["worker_id"],
            "session_id": current_worker["session_id"],
            "machine_label": current_worker["machine_label"],
            "proof_scoped_machine_fingerprint_sha256": fingerprint,
            "git_head_sha": git_info["head_sha"],
            "git_origin_branch_sha": git_info["origin_sha"],
            "working_tree_clean": git_info["is_clean"],
            "peer_registration_observed": True,
            "peer_worker_id": peer_worker["worker_id"],
            "peer_session_id": peer_worker["session_id"],
            "claim_started_at": claim_start_iso,
            "claim_completed_at": claim_end_iso,
            "initial_disposition": "ACQUIRED",
            "initial_observed_lease": obs_lease_dict,
            "recovery_attempted": False,
            "recovery_started_at": None,
            "recovery_completed_at": None,
            "recovery_disposition": None,
            "recovery_lease": None,
            "worker_terminal_role": "INITIAL_WINNER_NO_RELEASE",
        }
    else:
        if obs_lease_snapshot.worker_id != peer_worker["worker_id"] or obs_lease_snapshot.session_id != peer_worker["session_id"]:
            raise CoordinationStorageError("HELD_BY_OTHER initial lease owner does not match peer worker")

        exp_val = obs_lease_snapshot.expires_at
        if isinstance(exp_val, datetime.datetime):
            exp_dt = exp_val if exp_val.tzinfo is not None else exp_val.replace(tzinfo=datetime.timezone.utc)
        else:
            exp_dt = parse_utc_datetime(str(exp_val))

        exp_ts = exp_dt.timestamp()
        if exp_ts > _time() + request_dict["ttl_seconds"] + RECOVERY_GRACE_SECONDS + 30.0:
            raise CoordinationStorageError("Live proof execution HOLD: Observed lease expiry is implausibly far in future")

        rec_start_dt = datetime.datetime.fromtimestamp(_time(), tz=datetime.timezone.utc)
        rec_start_iso = rec_start_dt.isoformat()

        wait_seconds = max(0.0, exp_ts - _time())
        max_recovery_wait = wait_seconds + RECOVERY_GRACE_SECONDS
        rec_deadline = _monotonic() + max_recovery_wait

        while _time() < exp_ts:
            if _monotonic() >= rec_deadline:
                raise CoordinationStorageError("Live proof execution HOLD: Timed out waiting for initial lease expiry during recovery")
            _sleep(0.05)

        try:
            recovery_claim = backend.try_claim(
                task_id=request_dict["task_id"],
                identity=curr_identity,
                ttl_seconds=request_dict["ttl_seconds"],
            )
        except Exception as e:
            raise CoordinationStorageError("Failed to execute recovery try_claim during live proof") from None

        rec_end_dt = datetime.datetime.fromtimestamp(_time(), tz=datetime.timezone.utc)
        rec_end_iso = rec_end_dt.isoformat()

        if not isinstance(recovery_claim, ClaimResult):
            raise CoordinationStorageError("Recovery claim result is not a canonical ClaimResult instance")

        if recovery_claim.disposition != ClaimDisposition.ACQUIRED:
            raise CoordinationStorageError(f"Live proof execution HOLD: Recovery claim failed with disposition {recovery_claim.disposition}")

        rec_lease_snapshot = recovery_claim.lease
        if not isinstance(rec_lease_snapshot, LeaseSnapshot):
            raise CoordinationStorageError("Recovery lease is not a canonical LeaseSnapshot instance")

        if rec_lease_snapshot.status != LeaseStatus.ACTIVE:
            raise CoordinationStorageError(f"Recovery lease status must be ACTIVE, found {rec_lease_snapshot.status}")

        if rec_lease_snapshot.worker_id != current_worker["worker_id"] or rec_lease_snapshot.session_id != current_worker["session_id"]:
            raise CoordinationStorageError("Recovery lease owner does not match current worker (initial loser)")

        if rec_lease_snapshot.lease_id == obs_lease_snapshot.lease_id:
            raise CoordinationStorageError("Recovery lease_id must not equal initial lease_id")

        if rec_lease_snapshot.generation <= obs_lease_snapshot.generation:
            raise CoordinationStorageError(f"Recovery generation {rec_lease_snapshot.generation} must be strictly greater than initial generation {obs_lease_snapshot.generation}")

        rec_lease_dict = _serialize_lease_snapshot(rec_lease_snapshot)

        result = {
            "schema_version": "0.1.0",
            "artifact_type": "AOS5_MULTI_MACHINE_WORKER_RESULT",
            "gate": "AOS-5",
            "proof_id": request_dict["proof_id"],
            "source_sha": request_dict["source_sha"],
            "control_branch": request_dict["control_branch"],
            "namespace_id": request_dict["namespace_id"],
            "task_id": request_dict["task_id"],
            "role": role,
            "worker_id": current_worker["worker_id"],
            "session_id": current_worker["session_id"],
            "machine_label": current_worker["machine_label"],
            "proof_scoped_machine_fingerprint_sha256": fingerprint,
            "git_head_sha": git_info["head_sha"],
            "git_origin_branch_sha": git_info["origin_sha"],
            "working_tree_clean": git_info["is_clean"],
            "peer_registration_observed": True,
            "peer_worker_id": peer_worker["worker_id"],
            "peer_session_id": peer_worker["session_id"],
            "claim_started_at": claim_start_iso,
            "claim_completed_at": claim_end_iso,
            "initial_disposition": "HELD_BY_OTHER",
            "initial_observed_lease": obs_lease_dict,
            "recovery_attempted": True,
            "recovery_started_at": rec_start_iso,
            "recovery_completed_at": rec_end_iso,
            "recovery_disposition": "ACQUIRED",
            "recovery_lease": rec_lease_dict,
            "worker_terminal_role": "INITIAL_LOSER_RECOVERED",
        }

    if output_path is not None:
        write_worker_result_atomic(result, output_path, live_dsn)

    return result


def verify_pair_results(req: Dict[str, Any], result_a: Dict[str, Any], result_b: Dict[str, Any]) -> Dict[str, Any]:
    reasons: List[str] = []

    check_forbidden_secret_keys(result_a)
    check_forbidden_secret_keys(result_b)

    valid_req, req_errs = validate_proof_artifact(req)
    if not valid_req:
        reasons.append(f"Request schema validation failed: {req_errs}")

    valid_a, a_errs = validate_proof_artifact(result_a)
    if not valid_a:
        reasons.append(f"Result A schema validation failed: {a_errs}")

    valid_b, b_errs = validate_proof_artifact(result_b)
    if not valid_b:
        reasons.append(f"Result B schema validation failed: {b_errs}")

    if reasons:
        return {"is_valid": False, "status": "HOLD", "reasons": reasons}

    # Verify matching proof parameters
    for key in ["proof_id", "source_sha", "control_branch", "namespace_id", "task_id"]:
        if result_a.get(key) != req.get(key):
            reasons.append(f"Result A {key} '{result_a.get(key)}' does not match request '{req.get(key)}'")
        if result_b.get(key) != req.get(key):
            reasons.append(f"Result B {key} '{result_b.get(key)}' does not match request '{req.get(key)}'")

    roles = {result_a.get("role"), result_b.get("role")}
    if roles != {"worker_a", "worker_b"}:
        reasons.append(f"Result pair roles must be exactly {{'worker_a', 'worker_b'}}, found {roles}")

    # Bind result identities to request
    request_workers = {w.get("role"): w for w in req.get("workers", []) if isinstance(w, dict)}
    if "worker_a" not in request_workers or "worker_b" not in request_workers:
        reasons.append("Request workers array must contain worker_a and worker_b entries")

    for res, name in [(result_a, "Result A"), (result_b, "Result B")]:
        role = res.get("role")
        req_w = request_workers.get(role)
        if req_w:
            if res.get("worker_id") != req_w.get("worker_id"):
                reasons.append(f"{name} worker_id '{res.get('worker_id')}' does not match request worker_id '{req_w.get('worker_id')}' for role '{role}'")
            if res.get("session_id") != req_w.get("session_id"):
                reasons.append(f"{name} session_id '{res.get('session_id')}' does not match request session_id '{req_w.get('session_id')}' for role '{role}'")
            if res.get("machine_label") != req_w.get("machine_label"):
                reasons.append(f"{name} machine_label '{res.get('machine_label')}' does not match request machine_label '{req_w.get('machine_label')}' for role '{role}'")

        peer_role = "worker_b" if role == "worker_a" else "worker_a"
        req_peer = request_workers.get(peer_role)
        if req_peer:
            if res.get("peer_worker_id") != req_peer.get("worker_id"):
                reasons.append(f"{name} peer_worker_id '{res.get('peer_worker_id')}' does not match request worker_id '{req_peer.get('worker_id')}' for peer role '{peer_role}'")
            if res.get("peer_session_id") != req_peer.get("session_id"):
                reasons.append(f"{name} peer_session_id '{res.get('peer_session_id')}' does not match request session_id '{req_peer.get('session_id')}' for peer role '{peer_role}'")

    # Determine winner and loser objects
    if result_a.get("initial_disposition") == "ACQUIRED":
        winner = result_a
        loser = result_b
    else:
        winner = result_b
        loser = result_a

    # Check distinct identities & machine fingerprints
    if result_a.get("worker_id") == result_b.get("worker_id"):
        reasons.append("Worker IDs must be distinct")
    if result_a.get("session_id") == result_b.get("session_id"):
        reasons.append("Session IDs must be distinct")
    if result_a.get("proof_scoped_machine_fingerprint_sha256") == result_b.get("proof_scoped_machine_fingerprint_sha256"):
        reasons.append("Machine fingerprints must be distinct")

    # Check Git SHA and cleanliness
    req_sha = req.get("source_sha")
    for name, res in [("Result A", result_a), ("Result B", result_b)]:
        if res.get("git_head_sha") != req_sha:
            reasons.append(f"{name} git_head_sha '{res.get('git_head_sha')}' does not match request source_sha '{req_sha}'")
        if res.get("git_origin_branch_sha") != req_sha:
            reasons.append(f"{name} git_origin_branch_sha '{res.get('git_origin_branch_sha')}' does not match request source_sha '{req_sha}'")
        if res.get("working_tree_clean") is not True:
            reasons.append(f"{name} working_tree_clean is not true")
        if res.get("peer_registration_observed") is not True:
            reasons.append(f"{name} peer_registration_observed is not true")

    # Check initial dispositions
    if winner.get("initial_disposition") != "ACQUIRED":
        reasons.append(f"Winner initial_disposition must be ACQUIRED, found '{winner.get('initial_disposition')}'")
    if loser.get("initial_disposition") != "HELD_BY_OTHER":
        reasons.append(f"Loser initial_disposition must be HELD_BY_OTHER, found '{loser.get('initial_disposition')}'")

    # Check initial observed lease epochs are identical
    w_lease = winner.get("initial_observed_lease", {})
    l_lease = loser.get("initial_observed_lease", {})
    if w_lease != l_lease:
        reasons.append(f"Initial observed lease epochs differ between winner and loser: {w_lease} != {l_lease}")

    # Check initial lease owner equals winner
    if w_lease.get("owner_worker_id") != winner.get("worker_id") or w_lease.get("owner_session_id") != winner.get("session_id"):
        reasons.append("Initial observed lease owner does not match winner identity")

    # Check winner terminal role & recovery fields
    if winner.get("worker_terminal_role") != "INITIAL_WINNER_NO_RELEASE":
        reasons.append(f"Winner terminal role must be INITIAL_WINNER_NO_RELEASE, found '{winner.get('worker_terminal_role')}'")
    if winner.get("recovery_attempted") is not False:
        reasons.append("Winner recovery_attempted must be false")

    # Check loser terminal role & recovery fields
    if loser.get("worker_terminal_role") != "INITIAL_LOSER_RECOVERED":
        reasons.append(f"Loser terminal role must be INITIAL_LOSER_RECOVERED, found '{loser.get('worker_terminal_role')}'")
    if loser.get("recovery_attempted") is not True:
        reasons.append("Loser recovery_attempted must be true")
    if loser.get("recovery_disposition") != "ACQUIRED":
        reasons.append(f"Loser recovery_disposition must be ACQUIRED, found '{loser.get('recovery_disposition')}'")

    rec_lease = loser.get("recovery_lease")
    if not rec_lease:
        reasons.append("Loser recovery_lease must not be null")
    else:
        if rec_lease.get("owner_worker_id") != loser.get("worker_id") or rec_lease.get("owner_session_id") != loser.get("session_id"):
            reasons.append("Recovery lease owner does not match loser identity")
        if rec_lease.get("lease_id") == w_lease.get("lease_id"):
            reasons.append(f"Recovery lease_id '{rec_lease.get('lease_id')}' must not equal initial lease_id")
        if rec_lease.get("generation", 0) <= w_lease.get("generation", 0):
            reasons.append(f"Recovery generation {rec_lease.get('generation')} must be strictly greater than initial generation {w_lease.get('generation')}")

    is_valid = len(reasons) == 0
    return {
        "is_valid": is_valid,
        "status": "PASS" if is_valid else "HOLD",
        "reasons": reasons,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="AOS-5 Hosted Multi-Machine Proof Harness")
    parser.add_argument("--dry-run", action="store_true", help="Execute dry-run validation without network/DB calls")
    parser.add_argument("--request-file", required=True, help="Path to proof request JSON file")
    parser.add_argument("--role", required=True, choices=["worker_a", "worker_b"], help="Worker role")
    parser.add_argument("--output-file", help="Path to write worker result JSON file")

    args = parser.parse_args()

    try:
        req = load_and_validate_request(args.request_file)
        if args.dry_run:
            res = execute_dry_run(req, args.role)
            print(json.dumps(res, indent=2))
            sys.exit(0)
        else:
            res = run_live_worker(req, args.role, args.output_file)
            print(json.dumps(res, indent=2))
            sys.exit(0)
    except Exception as e:
        err_res = {
            "status": "HOLD",
            "error": str(e),
        }
        print(json.dumps(err_res, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
