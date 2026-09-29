"""Isolated, evidence-bound source repair pipeline for the platform recovery plane.

The pipeline owns Git isolation and lineage verification.  Bounded repair,
publication, and certification are injected capabilities so this layer never
silently acquires provider, remote, promotion, or activation authority.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Dict, Mapping

from aos.platform_recovery import SourceRepairResult
from aos.process_utils import run_headless
from aos.workspace_fingerprint import compute_workspace_fingerprint


_FULL_SHA = re.compile(r"^[a-f0-9]{40}$")
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")

RepairDriver = Callable[[Dict[str, Any], Path], Mapping[str, Any]]
BranchPublisher = Callable[[Path, str, str], Mapping[str, Any]]
ExactShaCertifier = Callable[[Path, str, str, Mapping[str, Any]], Mapping[str, Any]]


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
        certification = dict(
            self.certifier(workspace, branch, repair_sha, publish_evidence)
        )
        manifest = dict(certification.get("candidate_manifest") or {})
        if manifest.get("source_sha") != repair_sha:
            raise ValueError("candidate manifest is not bound to the repair SHA")

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
