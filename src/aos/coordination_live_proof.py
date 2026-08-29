"""AOS-5 Hosted Multi-Machine Proof Harness and Live Authorization Boundary."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jsonschema import Draft202012Validator, FormatChecker

from aos.coordination import CoordinationStorageError, WorkerIdentity
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


def load_proof_schema() -> Dict[str, Any]:
    if not SCHEMA_PATH.is_file():
        raise FileNotFoundError(f"Proof schema file not found: {SCHEMA_PATH}")
    return load_json_strict(SCHEMA_PATH)


def validate_proof_artifact(data: Any) -> Tuple[bool, List[str]]:
    schema = load_proof_schema()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = [err.message for err in validator.iter_errors(data)]
    return len(errors) == 0, errors


def check_forbidden_secret_keys(obj: Any, live_dsn_val: Optional[str] = None) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in FORBIDDEN_SECRET_KEYS:
                raise ValueError(f"Forbidden secret-like key detected in artifact: {k}")
            if isinstance(v, str) and live_dsn_val and live_dsn_val in v and len(live_dsn_val) > 5:
                raise ValueError(f"Raw DSN value detected in artifact field: {k}")
            check_forbidden_secret_keys(v, live_dsn_val)
    elif isinstance(obj, list):
        for item in obj:
            check_forbidden_secret_keys(item, live_dsn_val)


def parse_utc_datetime(iso_str: str) -> datetime.datetime:
    try:
        dt = datetime.datetime.fromisoformat(iso_str)
    except Exception as e:
        raise ValueError(f"Invalid ISO datetime string: '{iso_str}'") from e

    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"Datetime must be UTC-aware (found naive datetime): '{iso_str}'")

    return dt.astimezone(datetime.timezone.utc)


def compute_proof_scoped_machine_fingerprint(proof_id: str, machine_label: str) -> str:
    raw_identity = f"{proof_id}:{machine_label}:{platform.node()}:{sys.platform}"
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
        # Only untracked runtime dir allowed if any, otherwise check if modified tracked files exist
        lines = [line for line in status_out.splitlines() if line and not line.startswith("?? .aos-runtime")]
        is_clean = len(lines) == 0
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


def run_live_worker(
    request_dict: Dict[str, Any],
    role: str,
    output_path: Optional[str | Path] = None,
    backend_override: Any = None,
    time_func: Any = None,
    sleep_func: Any = None,
) -> Dict[str, Any]:
    validate_proof_request_dict(request_dict)

    if not request_dict.get("authorized", False):
        raise CoordinationStorageError("Live proof execution HOLD: request.authorized is false")

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

    _time = time_func or time.time
    _sleep = sleep_func or time.sleep

    fingerprint = compute_proof_scoped_machine_fingerprint(
        request_dict["proof_id"], current_worker["machine_label"]
    )

    curr_identity = WorkerIdentity(
        worker_id=current_worker["worker_id"],
        session_id=current_worker["session_id"],
    )
    peer_identity = WorkerIdentity(
        worker_id=peer_worker["worker_id"],
        session_id=peer_worker["session_id"],
    )

    backend.register_worker(curr_identity)

    # Wait boundedly for peer worker registration
    peer_registered = False
    peer_wait_deadline = _time() + 30.0
    while _time() < peer_wait_deadline:
        if backend.is_worker_registered(peer_identity.worker_id, peer_identity.session_id):
            peer_registered = True
            break
        _sleep(0.1)

    if not peer_registered:
        raise CoordinationStorageError("Live proof execution HOLD: Peer worker registration not observed within timeout")

    # Wait boundedly until start_at_utc
    start_dt = parse_utc_datetime(request_dict["start_at_utc"])
    start_ts = start_dt.timestamp()
    while _time() < start_ts:
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

    initial_disposition = initial_claim["status"]
    observed_lease = initial_claim["lease"]

    obs_lease_dict = {
        "owner_worker_id": observed_lease["owner_worker_id"],
        "owner_session_id": observed_lease["owner_session_id"],
        "lease_id": observed_lease["lease_id"],
        "generation": int(observed_lease["generation"]),
        "acquired_at": observed_lease["acquired_at"],
        "expires_at": observed_lease["expires_at"],
        "ttl_seconds": float(observed_lease["ttl_seconds"]),
        "status": observed_lease["status"],
    }

    if initial_disposition == "ACQUIRED":
        # Initial winner: NO heartbeat, NO release
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
        # Initial loser: Wait until observed lease expires, then recover
        exp_dt = parse_utc_datetime(observed_lease["expires_at"])
        exp_ts = exp_dt.timestamp()

        rec_start_dt = datetime.datetime.fromtimestamp(_time(), tz=datetime.timezone.utc)
        rec_start_iso = rec_start_dt.isoformat()

        # Wait boundedly for lease expiry
        expiry_deadline = exp_ts + request_dict["ttl_seconds"] + 10.0
        while _time() < exp_ts:
            if _time() > expiry_deadline:
                raise CoordinationStorageError("Live proof execution HOLD: Observed lease recovery timed out waiting for expiry")
            _sleep(0.1)

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

        if recovery_claim["status"] != "ACQUIRED":
            raise CoordinationStorageError(f"Live proof execution HOLD: Recovery claim failed with status {recovery_claim['status']}")

        rec_lease = recovery_claim["lease"]
        rec_lease_dict = {
            "owner_worker_id": rec_lease["owner_worker_id"],
            "owner_session_id": rec_lease["owner_session_id"],
            "lease_id": rec_lease["lease_id"],
            "generation": int(rec_lease["generation"]),
            "acquired_at": rec_lease["acquired_at"],
            "expires_at": rec_lease["expires_at"],
            "ttl_seconds": float(rec_lease["ttl_seconds"]),
            "status": rec_lease["status"],
        }

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

    # Pure offline checks
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

    # Verify roles are worker_a and worker_b
    roles = {result_a.get("role"), result_b.get("role")}
    if roles != {"worker_a", "worker_b"}:
        reasons.append(f"Result pair roles must be exactly {{'worker_a', 'worker_b'}}, found {roles}")

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
