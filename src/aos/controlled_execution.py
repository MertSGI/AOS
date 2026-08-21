"""Generic Controlled Single-Worker Execution Engine for AOS-3."""

from __future__ import annotations

import datetime
from typing import Any, Callable, Dict, List, Optional

from aos.execution_authority import validate_execution_authority
from aos.git_workspace import GitWorkspace
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
    ):
        self.descriptor = project_descriptor
        self.task = canonical_task
        self.source_adapter_factory = source_adapter_factory or (lambda repo, ref: ProjectSourceAdapter(repo, ref))
        self.git_workspace_factory = git_workspace_factory or (lambda repo, base, tid, branch: GitWorkspace(repo, base, tid, branch))
        self.worker_adapter_factory = worker_adapter_factory or (lambda: AntigravityWorkerAdapter())

    def execute(self, local_target_repo_path: Optional[str] = None) -> Dict[str, Any]:
        """Execute controlled single-worker workflow under LIVE_GUARD."""
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        errors: List[str] = []

        project_id = self.descriptor.get("project_id", "unknown")
        task_id = self.task.get("task_id", "unknown")
        gate = self.task.get("gate", "unknown")

        def _build_result(
            disposition: str,
            control_sha: str = "0000000000000000000000000000000000000000",
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
            mutation_performed: bool = False,
            err_list: Optional[List[str]] = None,
        ) -> Dict[str, Any]:
            finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            all_errors = (err_list or []) + errors
            allowed_p = self.task.get("allowed_scope", {}).get("paths", [])
            forbidden_p = self.task.get("allowed_scope", {}).get("forbidden_paths", [])
            res = {
                "schema_version": "0.1.0",
                "project_id": project_id,
                "task_id": task_id,
                "gate": gate,
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
                "verification_checks": ["schema_validation", "execution_authority", "live_guard", "scope_guard"],
                "mutation_performed": mutation_performed,
                "retry_count": 0,
                "started_at": started_at,
                "finished_at": finished_at,
                "errors": all_errors,
            }
            return res

        # 1. Validate descriptor schema
        desc_val = validate_document("project_descriptor", self.descriptor)
        if not desc_val.is_valid:
            return _build_result("HOLD", err_list=[f"Descriptor schema invalid: {e}" for e in desc_val.errors])

        # 2. Validate task schema
        task_val = validate_document("task", self.task)
        if not task_val.is_valid:
            return _build_result("HOLD", err_list=[f"Task schema invalid: {e}" for e in task_val.errors])

        # 3. Check worker requirements & constraints
        worker_reqs = self.task.get("worker_requirements", {})
        if not worker_reqs.get("isolated_worktree"):
            return _build_result("HOLD", err_list=["AOS-3 requires isolated_worktree = true"])

        if worker_reqs.get("adapter") != "antigravity":
            return _build_result("HOLD", err_list=[f"AOS-3 requires worker adapter 'antigravity', got '{worker_reqs.get('adapter')}'"])

        # 4. Check retry policy
        retry_policy = self.task.get("retry_policy", {})
        max_retries = retry_policy.get("max_retries", 0)
        if max_retries > GLOBAL_MAX_RETRY_CEILING:
            return _build_result("HOLD", err_list=[f"Task retry_policy.max_retries ({max_retries}) exceeds global ceiling ({GLOBAL_MAX_RETRY_CEILING})"])

        # 5. LIVE_GUARD: Resolve live control ref
        repo = self.descriptor.get("repository")
        control_ref = self.descriptor.get("control_ref")

        source_adapter = self.source_adapter_factory(repo, control_ref)
        try:
            live_control_sha = source_adapter.resolve_ref_to_sha()
        except Exception as e:
            return _build_result("HOLD", err_list=[f"LIVE_GUARD: Failed to resolve control ref '{control_ref}': {e}"])

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
            return _build_result("HOLD", control_sha=live_control_sha, err_list=[f"LIVE_GUARD: Failed to build snapshot at SHA {live_control_sha}: {e}"])

        # Validate snapshot schema
        snap_val = validate_document("canonical_project_snapshot", snapshot)
        if not snap_val.is_valid:
            return _build_result("HOLD", control_sha=live_control_sha, err_list=[f"Snapshot schema invalid: {e}" for e in snap_val.errors])

        # Validate execution authority
        auth_result = validate_execution_authority(snapshot, self.task)
        if not auth_result.is_valid:
            return _build_result(
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
                return _build_result(
                    "HOLD",
                    control_sha=live_control_sha,
                    exec_base_sha=exec_base_sha,
                    err_list=[f"STALE_CONTROL_REVISION: Control ref moved from '{live_control_sha}' to '{fresh_control_sha}' during preparation"],
                )
        except Exception as e:
            return _build_result("HOLD", control_sha=live_control_sha, exec_base_sha=exec_base_sha, err_list=[f"LIVE_GUARD freshness check failed: {e}"])

        # Target repository path for git workspace
        target_repo_path = local_target_repo_path or "."

        # 6. Setup Isolated Git Workspace
        workspace = self.git_workspace_factory(
            target_repo_path,
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
            return _build_result(
                "HOLD",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                err_list=[f"Failed to setup isolated workspace from base SHA '{exec_base_sha}': {e}"],
            )

        # 7. Invoke Worker Adapter
        worker_adapter = self.worker_adapter_factory()
        timeout_sec = worker_reqs.get("timeout_seconds", 3600)
        allowed_scope = self.task.get("allowed_scope", {})

        try:
            w_res: WorkerExecutionResult = worker_adapter.execute(
                task=self.task,
                workspace_path=workspace_dir,
                allowed_scope=allowed_scope,
                base_sha=exec_base_sha,
                timeout_seconds=timeout_sec,
            )
        except Exception as e:
            workspace.cleanup()
            return _build_result(
                "WORKER_FAILED",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                worker_branch=worker_branch,
                initial_head=initial_head,
                worker_adapter_name=worker_reqs.get("adapter", "antigravity"),
                err_list=[f"Worker adapter execution exception: {e}"],
            )

        if w_res.timed_out or (w_res.exit_code is not None and w_res.exit_code != 0):
            workspace.cleanup()
            disp = "WORKER_FAILED"
            msg = f"Worker timed out after {timeout_sec}s" if w_res.timed_out else f"Worker exited with code {w_res.exit_code}"
            return _build_result(
                disp,
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                worker_branch=worker_branch,
                initial_head=initial_head,
                worker_adapter_name=w_res.worker_identity,
                exit_code=w_res.exit_code,
                timed_out=w_res.timed_out,
                mutation_performed=w_res.mutation_attempted,
                err_list=[msg],
            )

        # 8. Inspect changes and run Scope Guard
        try:
            final_head = workspace.get_current_head()
            changed_paths = workspace.get_changed_files()
        except Exception as e:
            workspace.cleanup()
            return _build_result(
                "VERIFICATION_FAILED",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                worker_branch=worker_branch,
                initial_head=initial_head,
                worker_adapter_name=w_res.worker_identity,
                exit_code=w_res.exit_code,
                timed_out=w_res.timed_out,
                err_list=[f"Failed to inspect post-worker workspace state: {e}"],
            )

        scope_result = validate_scope(changed_paths, allowed_scope)
        workspace.cleanup()

        if not scope_result.is_valid:
            return _build_result(
                "VERIFICATION_FAILED",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                worker_branch=worker_branch,
                initial_head=initial_head,
                final_head=final_head,
                changed_paths=changed_paths,
                scope_valid=False,
                violations=scope_result.violations,
                worker_adapter_name=w_res.worker_identity,
                exit_code=w_res.exit_code,
                timed_out=w_res.timed_out,
                mutation_performed=len(changed_paths) > 0,
                err_list=scope_result.violations,
            )

        # Success: Scope checks passed -> VERIFIED_CANDIDATE
        return _build_result(
            "VERIFIED_CANDIDATE",
            control_sha=live_control_sha,
            exec_base_sha=exec_base_sha,
            worker_branch=worker_branch,
            initial_head=initial_head,
            final_head=final_head,
            changed_paths=changed_paths,
            scope_valid=True,
            violations=[],
            worker_adapter_name=w_res.worker_identity,
            exit_code=w_res.exit_code,
            timed_out=w_res.timed_out,
            mutation_performed=len(changed_paths) > 0,
        )
