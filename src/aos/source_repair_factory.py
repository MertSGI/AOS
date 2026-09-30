"""Construct the governed source-repair pipeline from native AOS capabilities."""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

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


@dataclass(frozen=True)
class SourceRepairAuthority:
    """Resolved AOS-system authority for Platform Recovery source repair."""

    repository: Path
    policy_path: Path
    worktree_root: Path
    runtime_dir: Path
    runtime_home: Path
    source_sha: str


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


def resolve_source_repair_authority(
    config: Mapping[str, Any],
) -> Optional[SourceRepairAuthority]:
    """Resolve and verify Platform Recovery authority without product fallback.

    The operations repository is authoritative when explicitly configured;
    otherwise the authoritative repository is used. A configured higher-priority
    path that is invalid does not silently fall through to another identity.
    """
    try:
        raw_repository = config.get("operations_repo_path")
        if not isinstance(raw_repository, str) or not raw_repository.strip():
            raw_repository = config.get("authoritative_repo_path")
        if not isinstance(raw_repository, str) or not raw_repository.strip():
            return None
        repository = Path(raw_repository).expanduser().resolve()

        projects = config.get("projects")
        if not isinstance(projects, Mapping):
            return None
        maintenance = projects.get("aos-maintenance")
        if not isinstance(maintenance, Mapping):
            return None
        raw_policy = maintenance.get("routing_policy_path")
        if not isinstance(raw_policy, str) or not raw_policy.strip():
            return None
        policy_path = Path(raw_policy).expanduser().resolve()
        if not policy_path.is_file():
            return None

        # Explicit AOS authority must never alias either named product workspace
        # or whichever product happens to be the default project.
        excluded_project_ids = {"lari", "lari-ui-v2"}
        default_project = config.get("default_project_id") or config.get("default_project")
        if isinstance(default_project, str) and default_project != "aos-maintenance":
            excluded_project_ids.add(default_project)
        for project_id in excluded_project_ids:
            project = projects.get(project_id)
            if not isinstance(project, Mapping):
                continue
            raw_workspace = project.get("workspace")
            if (
                isinstance(raw_workspace, str)
                and raw_workspace.strip()
                and Path(raw_workspace).expanduser().resolve() == repository
            ):
                return None

        source_sha = str(
            config.get("candidate_source_sha")
            or config.get("runtime_source_sha")
            or ""
        )
        if not _FULL_SHA.fullmatch(source_sha):
            return None

        raw_runtime_root = config.get("runtime_root")
        if not isinstance(raw_runtime_root, str) or not raw_runtime_root.strip():
            return None
        runtime_dir = Path(raw_runtime_root).expanduser().resolve()
        raw_runtime_home = config.get("runtime_home")
        runtime_home = (
            Path(raw_runtime_home).expanduser().resolve()
            if isinstance(raw_runtime_home, str) and raw_runtime_home.strip()
            else runtime_dir.parent
        )

        if str(_git(repository, "rev-parse", "--is-inside-work-tree")).strip() != "true":
            return None
        _git(repository, "cat-file", "-e", f"{source_sha}^{{commit}}")

        return SourceRepairAuthority(
            repository=repository,
            policy_path=policy_path,
            worktree_root=(runtime_dir / "worktrees").resolve(),
            runtime_dir=runtime_dir,
            runtime_home=runtime_home,
            source_sha=source_sha,
        )
    except Exception:
        return None


def create_source_repair_executor_from_config(
    config: Mapping[str, Any],
) -> Optional[IsolatedSourceRepairPipeline]:
    """Build the governed executor only from verified AOS source authority."""
    authority = resolve_source_repair_authority(config)
    if authority is None:
        return None
    try:
        return create_source_repair_executor(
            repository=authority.repository,
            worktree_root=authority.worktree_root,
            policy_path=authority.policy_path,
            runtime_dir=authority.runtime_dir,
            runtime_home=authority.runtime_home,
        )
    except Exception:
        return None


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
