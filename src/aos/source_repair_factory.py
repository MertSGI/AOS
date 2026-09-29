"""Factory for creating a real SourceRepairExecutor wired to the native execution fabric.

This module provides a single entry point to construct an IsolatedSourceRepairPipeline
that uses the actual resource-backed repair driver, GitHub CI observation, and
git-based publication - all using the existing autonomous host infrastructure.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from aos.platform_recovery import SourceRepairResourceUnavailable
from aos.process_utils import run_headless
from extensions.autonomy_fabric.execution_backend import (
    ExecutionCapability,
    ExecutionRequest,
)
from extensions.autonomy_fabric.execution_router import ExecutionRouter
from extensions.autonomy_fabric.native_workers import (
    GitHubCIWorker,
    NativeGitWorker,
)

from aos.autonomous_host import build_execution_router
from aos.source_repair_pipeline import (
    ExactShaCertifier,
    BranchPublisher,
    IsolatedSourceRepairPipeline,
    RepairDriver,
    ResourceBackedRepairDriver,
    SourceRepairResult,
)


_FULL_SHA = re.compile(r"^[a-f0-9]{40}$")


def _ensure_repo_extensions_importable() -> None:
    """Make the repository-shipped extensions package importable."""
    candidates: list[Path] = []
    aos_home = os.environ.get("AOS_HOME")
    if aos_home:
        candidates.append(Path(aos_home))
    candidates.extend([
        Path(__file__).resolve().parents[1],
        Path(__file__).resolve().parents[2],
        Path.cwd(),
    ])
    for candidate in candidates:
        if (candidate / "extensions" / "__init__.py").exists():
            resolved = str(candidate.resolve())
            if resolved not in sys.path:
                sys.path.insert(0, resolved)
            return


def _git(repository: Path, *args: str, text: bool = True) -> Any:
    """Execute a git command in the given repository."""
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


def _make_publisher(worker: NativeGitWorker, remote_name: str = "origin") -> BranchPublisher:
    """Create a BranchPublisher that uses NativeGitWorker to push the repair branch."""
    def publisher(workspace: Path, branch: str, repair_sha: str) -> Mapping[str, Any]:
        # Push the branch to remote (non-force)
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
            payload={
                "action": "push",
                "args": [remote_name, branch],
            },
        ))
        if push_result.status != "SUCCESS":
            raise RuntimeError(f"Failed to push repair branch: {push_result.sanitized_errors}")
        return {"remote_sha": repair_sha, "push_mode": "NORMAL"}
    return publisher


def _make_certifier(worker: GitHubCIWorker, repo: str = "MertSGI/AOS") -> ExactShaCertifier:
    """Create an ExactShaCertifier that uses GitHubCIWorker to observe exact-SHA CI."""
    def certifier(workspace: Path, branch: str, repair_sha: str, publication: Mapping[str, Any]) -> Mapping[str, Any]:
        ci_result = worker.execute(ExecutionRequest(
            task_id=f"certify-{repair_sha[:8]}",
            project_id="aos-platform-recovery",
            workspace=str(workspace),
            operation_class="source_repair_certify",
            required_capabilities=[ExecutionCapability.CI_OBSERVE, ExecutionCapability.GITHUB_READ],
            authority_id="PLATFORM_RECOVERY",
            read_scope=["."],
            write_scope=[],
            network_policy="APPROVED_ENDPOINTS",
            timeout_seconds=600,
            payload={
                "action": "observe_run",
                "sha": repair_sha,
                "repo": repo,
            },
        ))
        
        # Map CI status to certification fields
        ci_status = "UNKNOWN"
        if ci_result.status == "SUCCESS":
            ci_status = "SUCCESS"
        elif ci_result.status in ("FAILED", "DEGRADED"):
            ci_status = "FAILURE"
        elif ci_result.status == "DENIED":
            ci_status = "CI_OBSERVATION_DENIED"
        
        return {
            "candidate_manifest": {"source_sha": repair_sha, "immutable": True},
            "tests_passed": ci_status == "SUCCESS",
            "evidence_valid": ci_status == "SUCCESS",
            "exact_sha_ci_status": ci_status,
            "candidate_materialized": ci_status == "SUCCESS",
            "evidence": dict(ci_result.evidence_payload),
        }
    return certifier


def create_source_repair_executor(
    repository: Path,
    worktree_root: Path,
    policy_path: Path,
    runtime_dir: Path,
    *,
    scarcity_policy: str = "AVOID_SCARCE",
    timeout_seconds: int = 1800,
    remote_name: str = "origin",
    github_repo: str = "MertSGI/AOS",
) -> IsolatedSourceRepairPipeline:
    """Create a fully-wired SourceRepairExecutor (IsolatedSourceRepairPipeline).

    This factory constructs all the required components:
    - ExecutionRouter: uses the same build_execution_router as the autonomous host
    - ResourceBackedRepairDriver: dispatches repair through the resource router
    - BranchPublisher: uses NativeGitWorker to push repair branches
    - ExactShaCertifier: uses GitHubCIWorker to observe exact-SHA CI

    Args:
        repository: Path to the git repository
        worktree_root: Root directory for isolated repair worktrees (must be outside repo)
        policy_path: Path to the routing policy JSON (e.g., descriptors/nemotron.planner-policy.json)
        runtime_dir: Runtime directory for resource OS (ledger, quota, snapshots)
        scarcity_policy: "AVOID_SCARCE" or "ALLOW_SCARCE" for resource selection
        timeout_seconds: Timeout for repair execution
        remote_name: Git remote name for publication
        github_repo: GitHub repository in "owner/repo" format for CI observation

    Returns:
        An IsolatedSourceRepairPipeline ready to execute source repairs.
    """
    _ensure_repo_extensions_importable()

    # 1. Build the execution router (same as autonomous host)
    execution_router = build_execution_router(policy_path, runtime_dir)

    # 2. Create the resource-backed repair driver
    repair_driver = ResourceBackedRepairDriver(
        execution_router,
        scarcity_policy=scarcity_policy,
        timeout_seconds=timeout_seconds,
    )

    # 3. Create the publisher using NativeGitWorker
    git_worker = NativeGitWorker()
    publisher = _make_publisher(git_worker, remote_name)

    # 4. Create the certifier using GitHubCIWorker
    ci_worker = GitHubCIWorker()
    certifier = _make_certifier(ci_worker, github_repo)

    # 5. Construct and return the pipeline
    return IsolatedSourceRepairPipeline(
        repository=repository,
        worktree_root=worktree_root,
        repair_driver=repair_driver,
        publisher=publisher,
        certifier=certifier,
    )