"""AOS Local Autonomous Host bootstrap daemon.

This module is intentionally transport-agnostic. It watches a bounded local inbox for
validated AOS job envelopes and executes them through the existing Autonomous Host V1.
It never grants authority: each job is fresh-bound by run_host() to canonical project
state, exact source SHA, and (when configured) exact execution-base SHA.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from aos.autonomous_host import run_host
from aos.planning_kernel import DEFAULT_GOAL, run_autonomous_project
from aos.secure_store import hydrate_environment, provider_presence


JOB_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{1,96}$")
PROHIBITED_KEYS = {
    "password", "passwd", "secret", "api_key", "apikey", "access_token",
    "refresh_token", "private_key", "client_secret", "authorization", "bearer_token",
}


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower().replace("-", "_") in PROHIBITED_KEYS:
                return True
            if _contains_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _resolve_under(path_value: str, roots: Iterable[Path]) -> Path:
    candidate = Path(path_value).expanduser().resolve()
    for root in roots:
        root_resolved = root.expanduser().resolve()
        try:
            candidate.relative_to(root_resolved)
            return candidate
        except ValueError:
            continue
    raise ValueError(f"Path is outside authorized roots: {candidate}")


def load_config(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "1.0.0":
        raise ValueError("Local host config schema_version must be 1.0.0")
    roots = data.get("authorized_roots")
    if not isinstance(roots, list) or not roots:
        raise ValueError("Local host config requires non-empty authorized_roots")
    if data.get("production") != "NO_GO":
        raise ValueError("Local host production must remain NO_GO")
    if data.get("ag_backend_enabled") is not False:
        raise ValueError("Local host requires ag_backend_enabled=false")
    data["authorized_roots"] = [str(Path(item).expanduser().resolve()) for item in roots]
    return data


def validate_job(job: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(job, dict):
        raise ValueError("Job envelope must be an object")
    if job.get("schema_version") != "1.0.0":
        raise ValueError("Job schema_version must be 1.0.0")
    job_id = job.get("job_id")
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise ValueError("Invalid job_id")
    if job.get("production") != "NO_GO":
        raise ValueError("Job production must be NO_GO")
    if job.get("ag_backend_enabled") is not False:
        raise ValueError("Job must keep AG backend disabled")
    if _contains_secret_key(job):
        raise ValueError("Job envelope contains prohibited secret-bearing key")

    roots = [Path(item) for item in config["authorized_roots"]]
    descriptor = _resolve_under(job["descriptor_path"], roots)
    workspace = _resolve_under(job["workspace"], roots)
    routing_policy = _resolve_under(job["routing_policy_path"], roots)
    if not descriptor.is_file():
        raise ValueError(f"Descriptor does not exist: {descriptor}")
    if not workspace.is_dir():
        raise ValueError(f"Workspace does not exist: {workspace}")
    if not routing_policy.is_file():
        raise ValueError(f"Routing policy does not exist: {routing_policy}")

    plan = job.get("run_plan")
    goal = job.get("goal")
    has_plan = isinstance(plan, dict)
    has_goal = isinstance(goal, str) and bool(goal.strip())
    if has_plan == has_goal:
        raise ValueError("Job must provide exactly one of run_plan or goal")
    if has_plan:
        if plan.get("schema_version") != "1.0.0":
            raise ValueError("Embedded run_plan schema_version must be 1.0.0")
        if not isinstance(plan.get("tasks"), list) or not plan["tasks"]:
            raise ValueError("Embedded run_plan requires at least one task")
    else:
        if len(goal.strip()) > 8000:
            raise ValueError("Goal is too long")
        for key in ("constraints", "red_lines"):
            value = job.get(key, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"{key} must be an array of strings")
            if len(value) > 64:
                raise ValueError(f"{key} has too many items")

    normalized = dict(job)
    normalized["descriptor_path"] = str(descriptor)
    normalized["workspace"] = str(workspace)
    normalized["routing_policy_path"] = str(routing_policy)
    return normalized


def _import_download_jobs(
    config: Dict[str, Any], inbox: Path, processed: Path, failed: Path
) -> int:
    downloads = config.get("download_watch_dir")
    if not downloads:
        return 0
    watch = Path(downloads).expanduser()
    if not watch.is_dir():
        return 0
    imported = 0
    for source in sorted(watch.glob("*.aosjob.json")):
        target = inbox / source.name
        if target.exists() or (processed / source.name).exists() or (failed / source.name).exists():
            continue
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
            validate_job(raw, config)
            inbox.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            imported += 1
        except Exception:
            # Invalid downloads are deliberately ignored here; processing status remains fail-closed.
            continue
    return imported


def process_one(job_path: Path, config: Dict[str, Any], runtime_root: Path) -> Dict[str, Any]:
    raw = json.loads(job_path.read_text(encoding="utf-8"))
    job = validate_job(raw, config)
    job_id = job["job_id"]
    job_runtime = runtime_root / "jobs" / job_id
    job_runtime.mkdir(parents=True, exist_ok=True)
    if isinstance(job.get("run_plan"), dict):
        plan_path = job_runtime / "run-plan.json"
        _atomic_json(plan_path, job["run_plan"])
        receipt = run_host(
            descriptor_path=Path(job["descriptor_path"]),
            plan_path=plan_path,
            workspace=Path(job["workspace"]),
            runtime_dir=job_runtime / "host",
            routing_policy_path=Path(job["routing_policy_path"]),
            max_iterations=int(job.get("max_iterations", 20)),
        )
        success = float(receipt.get("progress", 0.0)) >= 100.0 and not receipt.get("failed_task_ids")
        status = "SUCCESS" if success else "HOLD_INCOMPLETE_OR_FAILED"
        mode = "MANUAL_RUN_PLAN"
    else:
        receipt = run_autonomous_project(
            descriptor_path=Path(job["descriptor_path"]),
            workspace=Path(job["workspace"]),
            runtime_dir=job_runtime / "autonomous",
            routing_policy_path=Path(job["routing_policy_path"]),
            goal=str(job.get("goal") or DEFAULT_GOAL),
            constraints=tuple(job.get("constraints", [])),
            red_lines=tuple(job.get("red_lines", [])) or tuple(),
            max_batches=int(job.get("max_batches", 12)),
            max_iterations_per_batch=int(job.get("max_iterations", 30)),
        )
        disposition = str(receipt.get("disposition", ""))
        status = "SUCCESS" if disposition == "PROJECT_COMPLETE" else disposition or "HOLD_INCOMPLETE_OR_FAILED"
        mode = "AUTONOMOUS_GOAL"
    result = {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "processed_at": _utc_now(),
        "status": status,
        "mode": mode,
        "receipt": receipt,
        "production": "NO_GO",
        "ag_invocation_count": receipt.get("ag_invocation_count", 0),
    }
    _atomic_json(job_runtime / "job-result.json", result)
    return result


def run_cycle(config_path: Path) -> Dict[str, Any]:
    config = load_config(config_path)
    # Refresh provider credentials from the Windows user vault every cycle so
    # rotations made through AOS Direct become effective without restarting.
    hydrate_environment(overwrite=True)
    runtime_root = Path(config["runtime_root"]).expanduser().resolve()
    inbox = runtime_root / "inbox"
    processed = runtime_root / "processed"
    failed = runtime_root / "failed"
    inbox.mkdir(parents=True, exist_ok=True)
    processed.mkdir(parents=True, exist_ok=True)
    failed.mkdir(parents=True, exist_ok=True)

    imported = _import_download_jobs(config, inbox, processed, failed)
    jobs = sorted(inbox.glob("*.aosjob.json"))
    status: Dict[str, Any] = {
        "schema_version": "1.0.0",
        "timestamp": _utc_now(),
        "state": "IDLE",
        "imported_download_jobs": imported,
        "pending_jobs": len(jobs),
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "providers": provider_presence(),
    }

    if jobs:
        job_path = jobs[0]
        try:
            result = process_one(job_path, config, runtime_root)
            if result["status"] == "SUCCESS":
                destination = processed / job_path.name
                state = "JOB_PROCESSED"
            elif result["status"] in ("WAITING_FOR_REASONING_PROVIDER", "BOUNDED_RUN_EXHAUSTED"):
                destination = processed / job_path.name
                state = result["status"]
            else:
                destination = failed / job_path.name
                state = "JOB_HOLD"
            os.replace(job_path, destination)
            status.update({"state": state, "last_job": result})
        except Exception as exc:
            destination = failed / job_path.name
            try:
                os.replace(job_path, destination)
            except OSError:
                pass
            status.update({
                "state": "JOB_HOLD",
                "last_job_file": job_path.name,
                "error_class": exc.__class__.__name__,
                "error": str(exc)[:1500],
            })

    _atomic_json(runtime_root / "host-status.json", status)
    return status




def _acquire_instance_lock(runtime_root: Path):
    """Hold an OS-level exclusive lock for the daemon lifetime."""
    runtime_root.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_root / "host.lock"
    lock_path.touch(exist_ok=True)
    handle = lock_path.open("r+b")
    try:
        if lock_path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                handle.close()
                raise RuntimeError("Another AOS local host instance is already running") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                raise RuntimeError("Another AOS local host instance is already running") from exc
        return handle
    except Exception:
        if not handle.closed:
            handle.close()
        raise

def daemon(config_path: Path) -> int:
    config = load_config(config_path)
    poll_seconds = max(5, int(config.get("poll_seconds", 20)))
    runtime_root = Path(config["runtime_root"]).expanduser().resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    _instance_lock = _acquire_instance_lock(runtime_root)
    pid_path = runtime_root / "host.pid"
    _atomic_json(pid_path, {"pid": os.getpid(), "started_at": _utc_now()})
    while True:
        try:
            run_cycle(config_path)
        except Exception as exc:
            _atomic_json(runtime_root / "host-status.json", {
                "schema_version": "1.0.0",
                "timestamp": _utc_now(),
                "state": "HOST_HOLD",
                "error_class": exc.__class__.__name__,
                "error": str(exc)[:1500],
                "production": "NO_GO",
                "ag_backend_enabled": False,
            })
        time.sleep(poll_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AOS Local Autonomous Host transport/execution daemon")
    parser.add_argument("--config", required=True)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()
    try:
        if args.daemon:
            return daemon(config_path)
        status = run_cycle(config_path)
        print(json.dumps(status, ensure_ascii=False, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"AOS_LOCAL_HOST_HOLD: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
