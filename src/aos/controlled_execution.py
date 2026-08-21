"""Generic Controlled Single-Worker Execution Engine for AOS-3."""

from __future__ import annotations

import datetime
import os
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

from aos.execution_authority import validate_execution_authority
from aos.git_workspace import GitWorkspace, normalize_github_repository_name
from aos.scope_guard import validate_scope
from aos.source_adapter import ProjectSourceAdapter
from aos.validate import validate_document
from aos.workers.antigravity import AntigravityWorkerAdapter
from aos.workers.base import WorkerAdapter, WorkerExecutionResult

GLOBAL_MAX_RETRY_CEILING = 2


class ControlledExecutionEngine:
    """Project-agnostic deterministic controlled execution engine for AOS-3."""

    def __init__(
        self,
        project_descriptor: Dict[str, Any],
        canonical_task: Dict[str, Any],
        source_adapter_factory: Optional[Callable[[str, str], ProjectSourceAdapter]] = None,
        git_workspace_factory: Optional[Callable[..., GitWorkspace]] = None,
        worker_adapter_factory: Optional[Callable[[], WorkerAdapter]] = None,
        verification_runner: Optional[Callable[[List[str], str, int], subprocess.CompletedProcess]] = None,
    ):
        self.descriptor = project_descriptor
        self.task = canonical_task
        self.source_adapter_factory = source_adapter_factory or (lambda repo, ref: ProjectSourceAdapter(repo, ref))
        self.git_workspace_factory = git_workspace_factory or (lambda repo, base, tid, branch: GitWorkspace(repo, base, tid, branch))
        self.worker_adapter_factory = worker_adapter_factory or (lambda: AntigravityWorkerAdapter())
        self.verification_runner = verification_runner or self._default_verification_runner

    def _default_verification_runner(self, argv: List[str], cwd: str, timeout_seconds: int) -> subprocess.CompletedProcess:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout_seconds)

    def execute(self, local_target_repo_path: Optional[str] = None) -> Dict[str, Any]:
        """Execute controlled single-worker workflow under LIVE_GUARD."""
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        errors: List[str] = []

        project_id = self.descriptor.get("project_id", "unknown")
        task_id = self.task.get("task_id", "unknown")
        gate = self.task.get("gate", "unknown")

        def _build_and_validate_result(
            disposition: str,
            control_sha: Optional[str] = None,
            exec_base_sha: Optional[str] = None,
            worker_branch: Optional[str] = None,
            initial_head: Optional[str] = None,
            final_head: Optional[str] = None,
            changed_paths: Optional[List[str]] = None,
            scope_valid: bool = False,
            violations: Optional[List[str]] = None,
            worker_adapter_name: str = "antigravity",
            exit_code: Optional[int] = None,
            timed_out: bool = False,
            verification_checks: Optional[List[Dict[str, Any]]] = None,
            mutation_performed: bool = False,
            err_list: Optional[List[str]] = None,
        ) -> Dict[str, Any]:
            finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            all_errors = (err_list or []) + errors

            allowed_p = self.task.get("allowed_scope", {}).get("paths", []) if isinstance(self.task.get("allowed_scope"), dict) else []
            forbidden_p = self.task.get("allowed_scope", {}).get("forbidden_paths", []) if isinstance(self.task.get("allowed_scope"), dict) else []

            res = {
                "schema_version": "0.1.0",
                "project_id": str(project_id) if project_id else "unknown",
                "task_id": str(task_id) if task_id else "unknown",
                "gate": str(gate) if gate else "unknown",
                "disposition": disposition,
                "control_source_sha": control_sha,
                "execution_base_sha": exec_base_sha,
                "worker_branch": worker_branch,
                "initial_head_sha": initial_head,
                "final_head_sha": final_head,
                "changed_paths": changed_paths or [],
                "scope_validation": {
                    "is_valid": scope_valid,
                    "allowed_paths": allowed_p,
                    "forbidden_paths": forbidden_p,
                    "violations": violations or [],
                },
                "worker_adapter": worker_adapter_name,
                "worker_exit_code": exit_code,
                "worker_timed_out": timed_out,
                "verification_checks": verification_checks or [],
                "mutation_performed": mutation_performed,
                "retry_count": 0,
                "started_at": started_at,
                "finished_at": finished_at,
                "errors": all_errors,
            }

            # Self-validate result against canonical schema
            val = validate_document("controlled_execution_result", res)
            if not val.is_valid:
                err_msgs = [str(e) for e in val.errors]
                raise RuntimeError(f"Internal execution contract error: generated result is invalid against schema: {'; '.join(err_msgs)}")

            return res

        # 0. Require local_target_repo_path explicitly
        if not local_target_repo_path or not isinstance(local_target_repo_path, str) or not local_target_repo_path.strip():
            return _build_and_validate_result("HOLD", err_list=["Missing required local_target_repo_path parameter"])

        # 1. Validate descriptor schema
        desc_val = validate_document("project_descriptor", self.descriptor)
        if not desc_val.is_valid:
            return _build_and_validate_result("HOLD", err_list=[f"Descriptor schema invalid: {str(e)}" for e in desc_val.errors])

        # 2. Verify target repository identity against descriptor
        expected_repo = normalize_github_repository_name(self.descriptor.get("repository", ""))
        dummy_ws = self.git_workspace_factory(local_target_repo_path, "0000000000000000000000000000000000000000", "dummy", None)
        actual_repo = dummy_ws.get_origin_repository_name()
        if actual_repo and normalize_github_repository_name(actual_repo) != expected_repo:
            return _build_and_validate_result(
                "HOLD",
                err_list=[f"Repository identity mismatch: local repo origin '{actual_repo}' does not match descriptor repository '{expected_repo}'"]
            )

        # 3. Validate task schema
        task_val = validate_document("task", self.task)
        if not task_val.is_valid:
            return _build_and_validate_result("HOLD", err_list=[f"Task schema invalid: {str(e)}" for e in task_val.errors])

        # 4. Check worker requirements & constraints
        worker_reqs = self.task.get("worker_requirements", {})
        if not worker_reqs.get("isolated_worktree"):
            return _build_and_validate_result("HOLD", err_list=["AOS-3 requires isolated_worktree = true"])

        if worker_reqs.get("adapter") != "antigravity":
            return _build_and_validate_result("HOLD", err_list=[f"AOS-3 requires worker adapter 'antigravity', got '{worker_reqs.get('adapter')}'"])

        # 5. Check worker adapter capability status (Fail-closed on UNPROVEN)
        worker_adapter = self.worker_adapter_factory()
        if getattr(worker_adapter, "capability_status", "UNPROVEN") == "UNPROVEN":
            return _build_and_validate_result(
                "HOLD",
                err_list=["Worker adapter capability status is UNPROVEN (live execution disabled until proven)"]
            )

        # 6. Check retry policy
        retry_policy = self.task.get("retry_policy", {})
        max_retries = retry_policy.get("max_retries", 0)
        if max_retries > GLOBAL_MAX_RETRY_CEILING:
            return _build_and_validate_result("HOLD", err_list=[f"Task retry_policy.max_retries ({max_retries}) exceeds global ceiling ({GLOBAL_MAX_RETRY_CEILING})"])

        # 7. Check required verification checks in evidence requirements
        req_checks = self.task.get("evidence_requirements", {}).get("required_checks", [])
        if not req_checks:
            return _build_and_validate_result("HOLD", err_list=["Initial AOS-3 R1 controlled execution requires at least one declared verification check"])

        desc_verification_checks = self.descriptor.get("verification", {}).get("checks", {})
        for check_id in req_checks:
            if check_id not in desc_verification_checks:
                return _build_and_validate_result("HOLD", err_list=[f"Unknown required verification check ID '{check_id}' not declared in descriptor verification registry"])

        # 8. LIVE_GUARD Pre-Worker: Resolve live control ref
        repo = self.descriptor.get("repository")
        control_ref = self.descriptor.get("control_ref")

        source_adapter = self.source_adapter_factory(repo, control_ref)
        try:
            live_control_sha = source_adapter.resolve_ref_to_sha()
        except Exception as e:
            return _build_and_validate_result("HOLD", err_list=[f"LIVE_GUARD: Failed to resolve control ref '{control_ref}': {e}"])

        # Fetch canonical context at exact control SHA & build snapshot
        paths_config = self.descriptor.get("control", {})
        projection_config = self.descriptor.get("projection", {})

        try:
            raw_contents, file_hashes = source_adapter.fetch_canonical_context(live_control_sha, paths_config)
            snapshot = source_adapter.build_normalized_snapshot(
                project_id=project_id,
                exact_sha=live_control_sha,
                raw_contents=raw_contents,
                file_hashes=file_hashes,
                projection_config=projection_config,
            )
        except Exception as e:
            return _build_and_validate_result("HOLD", control_sha=live_control_sha, err_list=[f"LIVE_GUARD: Failed to build snapshot at SHA {live_control_sha}: {e}"])

        # Validate snapshot schema
        snap_val = validate_document("canonical_project_snapshot", snapshot)
        if not snap_val.is_valid:
            return _build_and_validate_result("HOLD", control_sha=live_control_sha, err_list=[f"Snapshot schema invalid: {str(e)}" for e in snap_val.errors])

        # Validate execution authority
        auth_result = validate_execution_authority(snapshot, self.task)
        if not auth_result.is_valid:
            return _build_and_validate_result(
                "HOLD",
                control_sha=live_control_sha,
                exec_base_sha=snapshot.get("next_action_execution_base_sha"),
                err_list=auth_result.errors,
            )

        exec_base_sha = snapshot.get("next_action_execution_base_sha")

        # Re-verify LIVE_GUARD freshness immediately before workspace setup
        try:
            fresh_control_sha = source_adapter.resolve_ref_to_sha()
            if fresh_control_sha != live_control_sha:
                return _build_and_validate_result(
                    "HOLD",
                    control_sha=live_control_sha,
                    exec_base_sha=exec_base_sha,
                    err_list=[f"STALE_CONTROL_REVISION: Control ref moved from '{live_control_sha}' to '{fresh_control_sha}' during preparation"],
                )
        except Exception as e:
            return _build_and_validate_result("HOLD", control_sha=live_control_sha, exec_base_sha=exec_base_sha, err_list=[f"LIVE_GUARD freshness check failed: {e}"])

        # 9. Setup Isolated Git Workspace (Disposable Clone)
        workspace = self.git_workspace_factory(
            local_target_repo_path,
            exec_base_sha,
            task_id,
            self.task.get("branch_name"),
        )

        try:
            workspace_dir = workspace.setup()
            initial_head = workspace.get_current_head()
            worker_branch = workspace.worker_branch
        except Exception as e:
            workspace.cleanup()
            return _build_and_validate_result(
                "HOLD",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                err_list=[f"Failed to setup isolated workspace from base SHA '{exec_base_sha}': {e}"],
            )

        # 10. Invoke Worker Adapter
        timeout_sec = worker_reqs.get("timeout_seconds", 3600)
        allowed_scope = self.task.get("allowed_scope", {})

        worker_failed = False
        worker_err_msg = ""
        w_res: Optional[WorkerExecutionResult] = None

        try:
            w_res = worker_adapter.execute(
                task=self.task,
                workspace_path=workspace_dir,
                allowed_scope=allowed_scope,
                base_sha=exec_base_sha,
                timeout_seconds=timeout_sec,
            )
            if w_res.timed_out or (w_res.exit_code is not None and w_res.exit_code != 0):
                worker_failed = True
                worker_err_msg = f"Worker timed out after {timeout_sec}s" if w_res.timed_out else f"Worker exited with code {w_res.exit_code}"
        except Exception as e:
            worker_failed = True
            worker_err_msg = f"Worker execution exception: {e}"

        # Failure forensics before cleanup
        try:
            final_head = workspace.get_current_head()
            current_branch = workspace.get_current_branch()
            changed_paths = workspace.get_changed_files()
        except Exception as e:
            final_head = initial_head
            current_branch = worker_branch
            changed_paths = []
            if not worker_failed:
                worker_failed = True
                worker_err_msg = f"Failed to inspect post-worker workspace: {e}"

        if worker_failed:
            workspace.cleanup()
            return _build_and_validate_result(
                "WORKER_FAILED",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                worker_branch=worker_branch,
                initial_head=initial_head,
                final_head=final_head,
                changed_paths=changed_paths,
                worker_adapter_name=getattr(worker_adapter, "capability_status", "antigravity"),
                exit_code=w_res.exit_code if w_res else None,
                timed_out=w_res.timed_out if w_res else False,
                mutation_performed=len(changed_paths) > 0,
                err_list=[worker_err_msg],
            )

        # 11. Post-Worker Git Integrity Check (HEAD / Commit / Branch checks)
        git_integrity_errors = []
        if initial_head != exec_base_sha:
            git_integrity_errors.append(f"Initial HEAD SHA '{initial_head}' != expected base SHA '{exec_base_sha}'")
        if current_branch != worker_branch:
            git_integrity_errors.append(f"UNAUTHORIZED_WORKER_COMMIT_OR_HEAD_CHANGE: Current branch '{current_branch}' != expected '{worker_branch}'")
        if final_head != initial_head:
            git_integrity_errors.append(f"UNAUTHORIZED_WORKER_COMMIT_OR_HEAD_CHANGE: Worker created commit or moved HEAD (initial '{initial_head}' != final '{final_head}')")

        if git_integrity_errors:
            workspace.cleanup()
            return _build_and_validate_result(
                "VERIFICATION_FAILED",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                worker_branch=worker_branch,
                initial_head=initial_head,
                final_head=final_head,
                changed_paths=changed_paths,
                worker_adapter_name=w_res.worker_identity if w_res else "antigravity",
                exit_code=w_res.exit_code if w_res else 0,
                timed_out=w_res.timed_out if w_res else False,
                mutation_performed=len(changed_paths) > 0,
                err_list=git_integrity_errors,
            )

        # 12. Scope Guard
        scope_result = validate_scope(changed_paths, allowed_scope)
        if not scope_result.is_valid:
            workspace.cleanup()
            return _build_and_validate_result(
                "VERIFICATION_FAILED",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                worker_branch=worker_branch,
                initial_head=initial_head,
                final_head=final_head,
                changed_paths=changed_paths,
                scope_valid=False,
                violations=scope_result.violations,
                worker_adapter_name=w_res.worker_identity if w_res else "antigravity",
                exit_code=w_res.exit_code if w_res else 0,
                timed_out=w_res.timed_out if w_res else False,
                mutation_performed=len(changed_paths) > 0,
                err_list=scope_result.violations,
            )

        # 13. Execute Required Project Verification Checks (e.g. pytest, lint)
        verification_records: List[Dict[str, Any]] = []
        verification_failed = False
        verification_err_msgs: List[str] = []

        for check_id in req_checks:
            check_def = desc_verification_checks[check_id]
            argv = check_def["argv"]
            timeout = check_def.get("timeout_seconds", 300)
            try:
                c_res = self.verification_runner(argv, workspace_dir, timeout)
                if c_res.returncode == 0:
                    verification_records.append({"check_id": check_id, "status": "PASS", "message": "Exit code 0"})
                else:
                    verification_failed = True
                    msg = f"Check '{check_id}' failed with exit code {c_res.returncode}"
                    verification_err_msgs.append(msg)
                    verification_records.append({"check_id": check_id, "status": "FAIL", "message": msg})
            except Exception as e:
                verification_failed = True
                msg = f"Check '{check_id}' execution error or timeout: {e}"
                verification_err_msgs.append(msg)
                verification_records.append({"check_id": check_id, "status": "FAIL", "message": msg})

        workspace.cleanup()

        if verification_failed:
            return _build_and_validate_result(
                "VERIFICATION_FAILED",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                worker_branch=worker_branch,
                initial_head=initial_head,
                final_head=final_head,
                changed_paths=changed_paths,
                scope_valid=True,
                violations=[],
                worker_adapter_name=w_res.worker_identity if w_res else "antigravity",
                exit_code=w_res.exit_code if w_res else 0,
                timed_out=w_res.timed_out if w_res else False,
                verification_checks=verification_records,
                mutation_performed=len(changed_paths) > 0,
                err_list=verification_err_msgs,
            )

        # 14. Post-Worker LIVE_GUARD Freshness Check (Second Freshness Check)
        try:
            post_worker_control_sha = source_adapter.resolve_ref_to_sha()
            if post_worker_control_sha != live_control_sha:
                return _build_and_validate_result(
                    "HOLD",
                    control_sha=live_control_sha,
                    exec_base_sha=exec_base_sha,
                    worker_branch=worker_branch,
                    initial_head=initial_head,
                    final_head=final_head,
                    changed_paths=changed_paths,
                    scope_valid=True,
                    violations=[],
                    worker_adapter_name=w_res.worker_identity if w_res else "antigravity",
                    exit_code=w_res.exit_code if w_res else 0,
                    timed_out=w_res.timed_out if w_res else False,
                    verification_checks=verification_records,
                    mutation_performed=len(changed_paths) > 0,
                    err_list=[f"STALE_CONTROL_REVISION_POST_EXECUTION: Control ref moved from '{live_control_sha}' to '{post_worker_control_sha}' during execution"],
                )
        except Exception as e:
            return _build_and_validate_result(
                "HOLD",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                err_list=[f"Post-worker LIVE_GUARD check failed: {e}"]
            )

        # 15. All checks passed -> VERIFIED_CANDIDATE
        return _build_and_validate_result(
            "VERIFIED_CANDIDATE",
            control_sha=live_control_sha,
            exec_base_sha=exec_base_sha,
            worker_branch=worker_branch,
            initial_head=initial_head,
            final_head=final_head,
            changed_paths=changed_paths,
            scope_valid=True,
            violations=[],
            worker_adapter_name=w_res.worker_identity if w_res else "antigravity",
            exit_code=w_res.exit_code if w_res else 0,
            timed_out=w_res.timed_out if w_res else False,
            verification_checks=verification_records,
            mutation_performed=len(changed_paths) > 0,
        )
