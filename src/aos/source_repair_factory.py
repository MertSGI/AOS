"""Construct the governed source-repair pipeline from native AOS capabilities."""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from aos import runtime_deploy
from aos.autonomous_host import build_execution_router
from aos.platform_recovery import SourceRepairResourceUnavailable
from aos.process_utils import run_headless
from aos.source_repair_pipeline import (
    BranchPublisher,
    ExactShaCertifier,
    IsolatedSourceRepairPipeline,
    ResourceBackedRepairDriver,
)
from extensions.autonomy_fabric.execution_backend import (
    ExecutionCapability,
    ExecutionRequest,
)
from extensions.autonomy_fabric.native_workers import GitHubCIWorker, NativeGitWorker


_FULL_SHA = re.compile(r"^[a-f0-9]{40}$")
_PENDING_CI_STATUSES = {"queued", "in_progress", "pending", "requested", "waiting"}
_TERMINAL_FAILURE_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "neutral",
    "skipped",
    "stale",
    "startup_failure",
    "timed_out",
}


def _git(repository: Path, *args: str, text: bool = True) -> Any:
    result = run_headless(
        ["git", "-C", str(repository), *args],
        timeout=120,
        check=False,
        text=text,
    )
    if result.returncode != 0:
        detail = result.stderr or result.stdout or "git command failed"
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise RuntimeError(str(detail).strip()[:1000])
    return result.stdout


def _make_publisher(
    worker: NativeGitWorker,
    remote_name: str = "origin",
) -> BranchPublisher:
    """Push normally, then independently resolve the exact remote branch ref."""

    def publisher(workspace: Path, branch: str, repair_sha: str) -> Mapping[str, Any]:
        push_result = worker.execute(ExecutionRequest(
            task_id=f"publish-{repair_sha[:8]}",
            project_id="aos-platform-recovery",
            workspace=str(workspace),
            operation_class="source_repair_publish",
            required_capabilities=[ExecutionCapability.GIT_WRITE],
            authority_id="PLATFORM_RECOVERY",
            read_scope=["."],
            write_scope=["."],
            network_policy="APPROVED_ENDPOINTS",
            timeout_seconds=120,
            payload={"action": "push", "args": [remote_name, branch]},
        ))
        if push_result.status != "SUCCESS":
            raise RuntimeError(
                f"Failed to push repair branch: {push_result.sanitized_errors}"
            )

        remote_ref = f"refs/heads/{branch}"
        observed = str(
            _git(workspace, "ls-remote", "--heads", remote_name, remote_ref)
        ).strip()
        matching = []
        for line in observed.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] == remote_ref:
                matching.append(fields[0].lower())
        if len(matching) != 1 or not _FULL_SHA.fullmatch(matching[0]):
            raise RuntimeError(
                f"Published repair branch ref was not independently observable: {remote_ref}"
            )
        remote_sha = matching[0]
        if remote_sha != repair_sha:
            raise ValueError(
                "Published repair branch SHA mismatch: "
                f"expected {repair_sha}, observed {remote_sha}"
            )
        return {
            "remote_sha": remote_sha,
            "remote_ref": remote_ref,
            "remote_observation": "git-ls-remote",
            "push_mode": "NORMAL",
        }

    return publisher


def _ci_observation(evidence: Mapping[str, Any]) -> tuple[str, str, str, int | None]:
    run: Mapping[str, Any] = {}
    runs = evidence.get("runs")
    if isinstance(runs, list) and runs and isinstance(runs[0], Mapping):
        run = runs[0]
    status = str(evidence.get("status") or run.get("status") or "").lower()
    conclusion = str(
        evidence.get("conclusion") or run.get("conclusion") or ""
    ).lower()
    observed_sha = str(
        evidence.get("sha")
        or evidence.get("head_sha")
        or evidence.get("headSha")
        or run.get("head_sha")
        or run.get("headSha")
        or ""
    ).lower()
    raw_run_id = (
        evidence.get("run_id")
        or evidence.get("databaseId")
        or evidence.get("id")
        or run.get("databaseId")
        or run.get("id")
    )
    try:
        run_id = int(raw_run_id) if raw_run_id is not None else None
    except (TypeError, ValueError):
        run_id = None
    return status, conclusion, observed_sha, run_id


def _wait_for_exact_sha_ci(
    worker: GitHubCIWorker,
    workspace: Path,
    repair_sha: str,
    repo: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Any, int, int]:
    """Wait within a strict bound for completed/success CI on one exact SHA."""
    timeout_seconds = max(1.0, min(float(timeout_seconds), 3600.0))
    poll_interval_seconds = max(0.1, min(float(poll_interval_seconds), 60.0))
    deadline = monotonic() + timeout_seconds
    attempts = 0

    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise SourceRepairResourceUnavailable(
                f"EXACT_SHA_CI_TIMEOUT:{repair_sha}"
            )
        attempts += 1
        ci_result = worker.execute(ExecutionRequest(
            task_id=f"certify-{repair_sha[:8]}-{attempts}",
            project_id="aos-platform-recovery",
            workspace=str(workspace),
            operation_class="source_repair_certify",
            required_capabilities=[
                ExecutionCapability.CI_OBSERVE,
                ExecutionCapability.GITHUB_READ,
            ],
            authority_id="PLATFORM_RECOVERY",
            read_scope=["."],
            write_scope=[],
            network_policy="APPROVED_ENDPOINTS",
            timeout_seconds=max(1, min(60, math.ceil(remaining))),
            payload={"action": "observe_run", "sha": repair_sha, "repo": repo},
        ))
        evidence = dict(ci_result.evidence_payload or {})
        status, conclusion, observed_sha, run_id = _ci_observation(evidence)

        if observed_sha and observed_sha != repair_sha:
            raise RuntimeError(
                "Exact-SHA CI observation mismatch: "
                f"expected {repair_sha}, observed {observed_sha}"
            )
        if status == "completed" and conclusion == "success":
            if observed_sha != repair_sha:
                raise RuntimeError("Completed CI success lacks exact-SHA proof")
            if run_id is None or run_id <= 0:
                raise SourceRepairResourceUnavailable(
                    f"EXACT_SHA_CI_RUN_ID_UNAVAILABLE:{repair_sha}"
                )
            return ci_result, run_id, attempts
        if status == "completed" or conclusion in _TERMINAL_FAILURE_CONCLUSIONS:
            raise RuntimeError(
                f"EXACT_SHA_CI_FAILED:{repair_sha}:status={status or 'unknown'}:"
                f"conclusion={conclusion or 'unknown'}"
            )

        errors = " ".join(str(item) for item in ci_result.sanitized_errors)
        no_run_yet = conclusion == "no_runs" or "NO_BOUND_CI_RUN_FOR_SHA" in errors
        pending = status in _PENDING_CI_STATUSES or no_run_yet
        if not pending:
            raise SourceRepairResourceUnavailable(
                f"EXACT_SHA_CI_UNAVAILABLE:{repair_sha}:{ci_result.status}"
            )

        remaining = deadline - monotonic()
        if remaining <= 0:
            raise SourceRepairResourceUnavailable(
                f"EXACT_SHA_CI_TIMEOUT:{repair_sha}"
            )
        sleep(min(poll_interval_seconds, remaining))


def _make_certifier(
    worker: GitHubCIWorker,
    runtime_home: Path,
    repo: str = "MertSGI/AOS",
    *,
    ci_timeout_seconds: float = 600,
    ci_poll_interval_seconds: float = 10,
) -> ExactShaCertifier:
    """Observe exact-SHA CI, then stage and validate a real immutable candidate."""
    runtime_home = runtime_home.expanduser().resolve()

    def certifier(
        workspace: Path,
        branch: str,
        repair_sha: str,
        publication: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ci_result, ci_run_id, poll_attempts = _wait_for_exact_sha_ci(
            worker,
            workspace,
            repair_sha,
            repo,
            timeout_seconds=ci_timeout_seconds,
            poll_interval_seconds=ci_poll_interval_seconds,
        )

        staged = runtime_deploy.stage(
            runtime_home,
            workspace,
            repair_sha,
            ci_run_id,
            repo,
        )
        validation = runtime_deploy.validate(runtime_home, repair_sha)
        candidate = (runtime_home / "candidate" / repair_sha).resolve()
        manifest_path = candidate / "candidate-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise RuntimeError("Candidate manifest is not a JSON object")
        if (
            manifest.get("candidate_source_sha") != repair_sha
            or manifest.get("build_source_sha") != repair_sha
        ):
            raise RuntimeError("Candidate manifest is not bound to the repair SHA")
        if Path(str(staged.get("candidate") or "")).resolve() != candidate:
            raise RuntimeError("Staged candidate path escaped the AOS runtime home")
        if Path(str(validation.get("candidate") or "")).resolve() != candidate:
            raise RuntimeError("Validated candidate path escaped the AOS runtime home")
        if staged.get("validation") != "PASS" or validation.get("validation") != "PASS":
            raise RuntimeError("Candidate materialization validation did not pass")

        return {
            "candidate_manifest": manifest,
            "tests_passed": True,
            "evidence_valid": True,
            "exact_sha_ci_status": "SUCCESS",
            "candidate_materialized": True,
            "evidence": {
                "ci_run_id": ci_run_id,
                "ci_poll_attempts": poll_attempts,
                "ci": dict(ci_result.evidence_payload or {}),
                "publication": dict(publication),
                "stage": dict(staged),
                "validation": dict(validation),
                "candidate_manifest_path": str(manifest_path),
            },
        }

    return certifier


def create_source_repair_executor(
    repository: Path,
    worktree_root: Path,
    policy_path: Path,
    runtime_dir: Path,
    runtime_home: Path,
    *,
    scarcity_policy: str = "AVOID_SCARCE",
    timeout_seconds: int = 1800,
    remote_name: str = "origin",
    github_repo: str = "MertSGI/AOS",
    ci_timeout_seconds: float = 600,
    ci_poll_interval_seconds: float = 10,
) -> IsolatedSourceRepairPipeline:
    """Create the bounded pipeline; this grants no promotion or activation authority."""
    execution_router = build_execution_router(policy_path, runtime_dir)
    repair_driver = ResourceBackedRepairDriver(
        execution_router,
        scarcity_policy=scarcity_policy,
        timeout_seconds=timeout_seconds,
    )
    publisher = _make_publisher(NativeGitWorker(), remote_name)
    certifier = _make_certifier(
        GitHubCIWorker(),
        runtime_home,
        github_repo,
        ci_timeout_seconds=ci_timeout_seconds,
        ci_poll_interval_seconds=ci_poll_interval_seconds,
    )
    return IsolatedSourceRepairPipeline(
        repository=repository,
        worktree_root=worktree_root,
        repair_driver=repair_driver,
        publisher=publisher,
        certifier=certifier,
    )
