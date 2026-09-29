"""Isolated, evidence-bound source repair pipeline for the platform recovery plane.

The pipeline owns Git isolation and lineage verification.  Bounded repair,
publication, and certification are injected capabilities so this layer never
silently acquires provider, remote, promotion, or activation authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, Mapping

from aos.platform_recovery import SourceRepairResourceUnavailable, SourceRepairResult
from aos.process_utils import run_headless
from aos.workspace_fingerprint import compute_workspace_fingerprint
from extensions.autonomy_fabric.execution_backend import (
    ExecutionCapability,
    ExecutionRequest,
)


_FULL_SHA = re.compile(r"^[a-f0-9]{40}$")
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")

RepairDriver = Callable[[Dict[str, Any], Path], Mapping[str, Any]]
BranchPublisher = Callable[[Path, str, str], Mapping[str, Any]]
ExactShaCertifier = Callable[[Path, str, str, Mapping[str, Any]], Mapping[str, Any]]


class ResourceBackedRepairDriver:
    """Dispatch one bounded source repair through the existing resource router."""

    def __init__(
        self,
        execution_router: Any,
        *,
        scarcity_policy: str = "AVOID_SCARCE",
        timeout_seconds: int = 1800,
    ) -> None:
        if scarcity_policy not in {"ALLOW_SCARCE", "AVOID_SCARCE"}:
            raise ValueError("unsupported source-repair scarcity policy")
        self.execution_router = execution_router
        self.scarcity_policy = scarcity_policy
        self.timeout_seconds = max(60, min(int(timeout_seconds), 3600))

    def __call__(self, request: Dict[str, Any], workspace: Path) -> Mapping[str, Any]:
        finding = request.get("finding") or {}
        proposed = finding.get("proposed_repair") or {}
        write_scope = [
            str(path).replace("\\", "/").strip("/")
            for path in proposed.get("files_likely_affected") or []
            if str(path).strip()
        ]
        if not write_scope:
            raise SourceRepairResourceUnavailable(
                "SOURCE_REPAIR_WRITE_SCOPE_UNAVAILABLE"
            )
        prompt = json.dumps({
            "task": "Apply the smallest source repair for this diagnosed AOS finding.",
            "finding": finding,
            "constraints": {
                "workspace": "Use only the provided isolated git worktree.",
                "write_scope": write_scope,
                "commit": "Do not commit or publish; the owning pipeline performs those steps.",
                "promotion": False,
                "activation": False,
                "production": "NO_GO",
                "paid_fallback": "DISABLED",
            },
        }, ensure_ascii=False, sort_keys=True)
        if len(prompt) > 64_000:
            raise ValueError("source-repair context exceeds the bounded resource envelope")
        execution_request = ExecutionRequest(
            task_id=str(request["job_id"]),
            project_id="aos-platform-recovery",
            workspace=str(workspace),
            operation_class="bounded_source_repair",
            required_capabilities=[
                ExecutionCapability.FILE_READ,
                ExecutionCapability.FILE_WRITE,
                ExecutionCapability.PATCH_APPLY,
                ExecutionCapability.PROCESS_EXEC,
                ExecutionCapability.GIT_READ,
                ExecutionCapability.TEST_EXECUTION,
                ExecutionCapability.LONG_HORIZON_AGENTIC_WORK,
            ],
            authority_id=str(finding.get("repair_authority") or "PLATFORM_RECOVERY"),
            read_scope=["."],
            write_scope=write_scope,
            network_policy="APPROVED_ENDPOINTS",
            timeout_seconds=self.timeout_seconds,
            payload={
                "prompt": prompt,
                "source_sha": str(request["required_base_sha"]),
                "resource_requirements": {
                    "scarcity_policy": self.scarcity_policy,
                    "agentic_planning_allowed": True,
                },
            },
        )
        result = self.execution_router.execute_with_failover(execution_request)
        telemetry = list(getattr(self.execution_router, "last_attempt_telemetry", []) or [])
        if result.status != "SUCCESS":
            detail = "; ".join(str(item) for item in result.sanitized_errors[:3])
            if result.backend_id == "router" or result.status in {"DENIED", "DEGRADED"}:
                raise SourceRepairResourceUnavailable(
                    f"NO_ELIGIBLE_SOURCE_REPAIR_RESOURCE:{detail or result.status}"
                )
            raise RuntimeError(
                f"SOURCE_REPAIR_RESOURCE_FAILED:{result.backend_id}:{detail or result.status}"
            )
        return {
            "resource_backend_id": result.backend_id,
            "attempt_telemetry": {
                "attempt_count": len(telemetry) or 1,
                "attempts": telemetry,
                "selected_backend_id": result.backend_id,
            },
            "changed_paths": list(result.changed_paths),
            "evidence": dict(result.evidence_payload),
        }


class IsolatedSourceRepairPipeline:
    """Create a new worktree, prove the repair commit, and stop at certification."""

    def __init__(
        self,
        repository: Path,
        worktree_root: Path,
        *,
        repair_driver: RepairDriver,
        publisher: BranchPublisher,
        certifier: ExactShaCertifier,
    ) -> None:
        self.repository = repository.expanduser().resolve()
        self.worktree_root = worktree_root.expanduser().resolve()
        try:
            self.worktree_root.relative_to(self.repository)
        except ValueError:
            pass
        else:
            raise ValueError("source repair worktree root must be isolated from the repository")
        self.repair_driver = repair_driver
        self.publisher = publisher
        self.certifier = certifier

    @staticmethod
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

    def __call__(self, request: Dict[str, Any]) -> SourceRepairResult:
        job_id = str(request.get("job_id") or "")
        base_sha = str(request.get("required_base_sha") or "").lower()
        requirements = request.get("requirements") or {}
        if not _SAFE_JOB_ID.fullmatch(job_id):
            raise ValueError("source repair job_id is not a safe bounded identifier")
        if not _FULL_SHA.fullmatch(base_sha):
            raise ValueError("source repair requires an exact lowercase base SHA")
        if requirements != {
            "isolated_worktree": True,
            "exact_sha_ci": True,
            "immutable_candidate": True,
            "promotion": False,
            "activation": False,
        }:
            raise ValueError("source repair requirements do not preserve the promotion boundary")
        self._git(self.repository, "cat-file", "-e", f"{base_sha}^{{commit}}")

        branch = f"repair/aos-system-{job_id}"
        workspace = (self.worktree_root / job_id).resolve()
        if workspace.exists():
            raise FileExistsError(f"isolated repair workspace already exists: {workspace}")
        existing = run_headless(
            ["git", "-C", str(self.repository), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            timeout=30,
            check=False,
        )
        if existing.returncode == 0:
            raise FileExistsError(f"isolated repair branch already exists: {branch}")
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self._git(self.repository, "worktree", "add", "-b", branch, str(workspace), base_sha)

        driver_evidence = dict(self.repair_driver(request, workspace))
        precommit_sha = str(self._git(workspace, "rev-parse", "HEAD")).strip().lower()
        dirty = str(self._git(workspace, "status", "--porcelain=v1")).strip()
        if precommit_sha != base_sha and dirty:
            raise ValueError("repair resource left a committed repair with additional dirty changes")
        if precommit_sha == base_sha:
            if not dirty:
                raise ValueError("repair resource produced no source change")
            self._git(workspace, "add", "--all")
            self._git(
                workspace,
                "commit",
                "-m",
                f"fix(platform): bounded repair {job_id}",
            )
        repair_sha = str(self._git(workspace, "rev-parse", "HEAD")).strip().lower()
        if not _FULL_SHA.fullmatch(repair_sha) or repair_sha == base_sha:
            raise ValueError("repair driver did not produce a new exact source commit")
        self._git(workspace, "merge-base", "--is-ancestor", base_sha, repair_sha)
        if str(self._git(workspace, "status", "--porcelain=v1")).strip():
            raise ValueError("isolated source repair workspace is not clean")

        diff_bytes = self._git(
            workspace, "diff", "--binary", f"{base_sha}..{repair_sha}", text=False
        )
        diff_sha = hashlib.sha256(diff_bytes).hexdigest()
        fingerprint = compute_workspace_fingerprint(
            workspace, source_sha=repair_sha
        ).sha256

        publish_evidence = dict(self.publisher(workspace, branch, repair_sha))
        if str(publish_evidence.get("remote_sha") or "").lower() != repair_sha:
            raise ValueError("published source branch is not bound to the repair SHA")
        if str(publish_evidence.get("push_mode") or "").upper() not in {
            "NORMAL", "FAST_FORWARD_ONLY"
        }:
            raise ValueError("source repair publication must be normal and non-force")
        certification = dict(
            self.certifier(workspace, branch, repair_sha, publish_evidence)
        )
        manifest = dict(certification.get("candidate_manifest") or {})
        manifest_source_sha = (
            manifest.get("candidate_source_sha") or manifest.get("source_sha")
        )
        if manifest_source_sha != repair_sha:
            raise ValueError("candidate manifest is not bound to the repair SHA")
        if (
            "build_source_sha" in manifest
            and manifest.get("build_source_sha") != repair_sha
        ):
            raise ValueError("candidate build manifest is not bound to the repair SHA")

        return SourceRepairResult(
            isolated_worktree=str(workspace),
            branch=branch,
            base_sha=base_sha,
            repair_sha=repair_sha,
            workspace_fingerprint=fingerprint,
            resource_backend_id=str(driver_evidence.get("resource_backend_id") or ""),
            attempt_telemetry=dict(driver_evidence.get("attempt_telemetry") or {}),
            git_diff_sha256=diff_sha,
            candidate_manifest=manifest,
            rollback_information={"base_sha": base_sha, "branch": branch},
            tests_passed=bool(certification.get("tests_passed")),
            evidence_valid=bool(certification.get("evidence_valid")),
            exact_sha_ci_status=str(certification.get("exact_sha_ci_status") or "UNKNOWN"),
            candidate_materialized=bool(certification.get("candidate_materialized")),
            promotion_performed=False,
            activation_performed=False,
            evidence={
                "driver": driver_evidence,
                "publication": publish_evidence,
                "certification": certification.get("evidence") or {},
            },
        )
