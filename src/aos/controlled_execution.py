"""Generic Controlled Single-Worker Execution Engine."""

from __future__ import annotations

import datetime
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from aos.candidate_store import (
    CandidateStoreError,
    persist_quarantine_candidate,
    persist_verified_candidate,
    persist_worker_failure_quarantine_candidate,
)

from aos.execution_authority import validate_execution_authority
from aos.git_workspace import (
    GitWorkspace,
    inspect_github_repository_identity,
    normalize_github_repository_name,
)
from aos.scope_guard import validate_scope
from aos.source_adapter import ProjectSourceAdapter
from aos.validate import validate_document
from aos.verification_workspace import (
    VerificationWorkspaceCopy,
    VerificationWorkspaceError,
    inspect_workspace_boundary_state,
)
from aos.workers.antigravity import AntigravityWorkerAdapter
from aos.workers.base import WorkerAdapter, WorkerExecutionResult

GLOBAL_MAX_RETRY_CEILING = 2
SENSITIVE_ENV_VARS = {"OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"}
DIAGNOSTIC_TAIL_MAX_CHARS = 4096


def sanitize_diagnostic_text(
    text: str,
    worker_ws_path: Optional[str] = None,
    verify_ws_path: Optional[str] = None,
    source_repo_path: Optional[str] = None,
) -> str:
    """Sanitize diagnostic text by redacting absolute filesystem paths and sensitive environment variable values."""
    if not text:
        return ""

    sanitized = text

    # Redact sensitive environment variables values if present
    for var_name in SENSITIVE_ENV_VARS:
        val = os.environ.get(var_name)
        if val and len(val.strip()) > 3 and val in sanitized:
            sanitized = sanitized.replace(val, f"<{var_name}_REDACTED>")

    # Redact absolute paths
    # Note: Replace longest matching paths first
    path_replacements: List[Tuple[str, str]] = []
    if verify_ws_path:
        path_replacements.append((verify_ws_path, "<VERIFY_WORKSPACE>"))
        path_replacements.append((str(Path(verify_ws_path).as_posix()), "<VERIFY_WORKSPACE>"))
    if worker_ws_path:
        path_replacements.append((worker_ws_path, "<WORKSPACE>"))
        path_replacements.append((str(Path(worker_ws_path).as_posix()), "<WORKSPACE>"))
    if source_repo_path:
        path_replacements.append((source_repo_path, "<SOURCE_REPO>"))
        path_replacements.append((str(Path(source_repo_path).as_posix()), "<SOURCE_REPO>"))

    # Sort replacements by length descending
    path_replacements.sort(key=lambda x: len(x[0]), reverse=True)
    for p_orig, p_rep in path_replacements:
        if p_orig and p_orig in sanitized:
            sanitized = sanitized.replace(p_orig, p_rep)

    return sanitized


class ControlledExecutionEngine:
    """Project-agnostic deterministic controlled execution engine."""

    def __init__(
        self,
        project_descriptor: Dict[str, Any],
        canonical_task: Dict[str, Any],
        source_adapter_factory: Optional[Callable[[str, str], ProjectSourceAdapter]] = None,
        git_workspace_factory: Optional[Callable[..., GitWorkspace]] = None,
        worker_adapter_factory: Optional[Callable[[], WorkerAdapter]] = None,
        verification_runner: Optional[Callable[[List[str], str, int, Dict[str, str]], subprocess.CompletedProcess]] = None,
        repo_identity_inspector: Optional[Callable[[str], str]] = None,
    ):
        self.descriptor = project_descriptor
        self.task = canonical_task
        self.source_adapter_factory = source_adapter_factory or (lambda repo, ref: ProjectSourceAdapter(repo, ref))
        self.git_workspace_factory = git_workspace_factory or (lambda repo, base, tid, branch: GitWorkspace(repo, base, tid, branch))
        self.worker_adapter_factory = worker_adapter_factory or (lambda: AntigravityWorkerAdapter())
        self.verification_runner = verification_runner or self._default_verification_runner
        self.repo_identity_inspector = repo_identity_inspector or inspect_github_repository_identity

    def _default_verification_runner(
        self, argv: List[str], cwd: str, timeout_seconds: int, env: Dict[str, str]
    ) -> subprocess.CompletedProcess:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout_seconds, env=env)

    def execute(self, local_target_repo_path: Optional[str] = None) -> Dict[str, Any]:
        """Execute controlled single-worker workflow under LIVE_GUARD."""
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        errors: List[str] = []

        project_id = self.descriptor.get("project_id", "unknown")
        task_id = self.task.get("task_id", "unknown")
        gate = self.task.get("gate", "unknown")

        req_checks = self.task.get("evidence_requirements", {}).get("required_checks", []) if isinstance(self.task.get("evidence_requirements"), dict) else []

        # Define ordered pipeline checks
        pipeline_check_ids: List[str] = [
            "descriptor_schema",
            "target_repository_identity",
            "task_schema",
            "worker_requirements",
            "worker_capability",
            "retry_policy",
            "required_checks_contract",
            "canonical_snapshot",
            "execution_authority",
            "execution_base_resolvability",
            "live_guard_pre_execution",
            "workspace_initial_integrity",
            "worker_execution",
            "worker_failure_forensic_integrity",
            "worker_failure_scope_guard",
            "worker_failure_quarantine",
            "git_integrity_post_worker",
            "scope_guard_post_worker",
            "live_guard_post_worker",
        ]


        for rc in req_checks:
            pipeline_check_ids.append(f"project:{rc}")
        pipeline_check_ids.extend([
            "git_integrity_final",
            "scope_guard_final",
            "live_guard_final",
            "failure_candidate_quarantine",
            "candidate_persistence",
        ])

        pipeline_status: Dict[str, Dict[str, Any]] = {
            cid: {"check_id": cid, "status": "NOT_RUN", "message": ""} for cid in pipeline_check_ids
        }

        def _record_check(check_id: str, status: str, message: str = ""):
            if check_id in pipeline_status:
                pipeline_status[check_id]["status"] = status
                pipeline_status[check_id]["message"] = message
            else:
                pipeline_status[check_id] = {"check_id": check_id, "status": status, "message": message}

        def _get_checks_list() -> List[Dict[str, Any]]:
            res = []
            for cid in pipeline_check_ids:
                entry = pipeline_status.get(cid, {"check_id": cid, "status": "NOT_RUN"})
                d: Dict[str, Any] = {"check_id": entry["check_id"], "status": entry["status"]}
                if entry.get("message"):
                    d["message"] = entry["message"]
                res.append(d)
            return res

        worker_adapter_instance = self.worker_adapter_factory()
        worker_adapter_name = "antigravity" if self.task.get("worker_requirements", {}).get("adapter") == "antigravity" else str(self.task.get("worker_requirements", {}).get("adapter") or "unknown")
        worker_capability_status = getattr(worker_adapter_instance, "capability_status", "UNPROVEN")
        worker_mutation_attempted = False
        worker_identity: Optional[str] = None

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
            exit_code: Optional[int] = None,
            timed_out: bool = False,
            mutation_performed: bool = False,
            mutation_attempted: bool = False,
            err_list: Optional[List[str]] = None,
            extensions: Optional[Dict[str, Any]] = None,
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
                "worker_identity": worker_identity,
                "worker_capability_status": worker_capability_status,
                "worker_mutation_attempted": mutation_attempted,
                "worker_exit_code": exit_code,
                "worker_timed_out": timed_out,
                "verification_checks": _get_checks_list(),
                "mutation_performed": mutation_performed,
                "retry_count": 0,
                "started_at": started_at,
                "finished_at": finished_at,
                "errors": all_errors,
            }

            if extensions:
                res["extensions"] = extensions

            # Self-validate result against canonical schema
            val = validate_document("controlled_execution_result", res)
            if not val.is_valid:
                err_msgs = [str(e) for e in val.errors]
                raise RuntimeError(f"Internal execution contract error: generated result is invalid against schema: {'; '.join(err_msgs)}")

            return res

        # 0. Validate target repository path & identity
        if not local_target_repo_path or not isinstance(local_target_repo_path, str) or not local_target_repo_path.strip():
            _record_check("target_repository_identity", "FAIL", "Missing local_target_repo_path parameter")
            return _build_and_validate_result("HOLD", err_list=["Missing required local_target_repo_path parameter"])

        # 1. Validate descriptor schema
        desc_val = validate_document("project_descriptor", self.descriptor)
        if not desc_val.is_valid:
            _record_check("descriptor_schema", "FAIL", "Descriptor schema invalid")
            return _build_and_validate_result("HOLD", err_list=[f"Descriptor schema invalid: {str(e)}" for e in desc_val.errors])
        _record_check("descriptor_schema", "PASS")

        # 2. Verify target repository identity against descriptor
        try:
            expected_repo = normalize_github_repository_name(self.descriptor.get("repository", ""))
            actual_repo = self.repo_identity_inspector(local_target_repo_path)
            if actual_repo != expected_repo:
                _record_check("target_repository_identity", "FAIL", f"Repository identity mismatch: local '{actual_repo}' != descriptor '{expected_repo}'")
                return _build_and_validate_result(
                    "HOLD",
                    err_list=[f"Repository identity mismatch: local repo origin '{actual_repo}' does not match descriptor repository '{expected_repo}'"]
                )
            _record_check("target_repository_identity", "PASS")
        except Exception as e:
            _record_check("target_repository_identity", "FAIL", str(e))
            return _build_and_validate_result("HOLD", err_list=[f"Target repository identity verification failed: {e}"])

        # 3. Validate task schema
        task_val = validate_document("task", self.task)
        if not task_val.is_valid:
            _record_check("task_schema", "FAIL", "Task schema invalid")
            return _build_and_validate_result("HOLD", err_list=[f"Task schema invalid: {str(e)}" for e in task_val.errors])
        _record_check("task_schema", "PASS")

        # 4. Check worker requirements & constraints
        worker_reqs = self.task.get("worker_requirements", {})
        if not worker_reqs.get("isolated_worktree") or worker_reqs.get("adapter") != "antigravity":
            _record_check("worker_requirements", "FAIL", "Invalid worker requirements")
            return _build_and_validate_result("HOLD", err_list=["Controlled execution requires isolated_worktree = true and adapter = 'antigravity'"])
        _record_check("worker_requirements", "PASS")

        # 5. Check worker capability status (Fail closed on UNPROVEN)
        if worker_capability_status == "UNPROVEN":
            _record_check("worker_capability", "FAIL", "Worker adapter capability is UNPROVEN")
            return _build_and_validate_result(
                "HOLD",
                err_list=["Worker adapter capability status is UNPROVEN (live execution disabled until proven)"]
            )
        _record_check("worker_capability", "PASS")

        # 6. Check retry policy
        retry_policy = self.task.get("retry_policy", {})
        max_retries = retry_policy.get("max_retries", 0)
        if max_retries > GLOBAL_MAX_RETRY_CEILING:
            _record_check("retry_policy", "FAIL", "Exceeds global retry ceiling")
            return _build_and_validate_result("HOLD", err_list=[f"Task retry_policy.max_retries ({max_retries}) exceeds global ceiling ({GLOBAL_MAX_RETRY_CEILING})"])
        _record_check("retry_policy", "PASS")

        # 7. Check required verification checks in evidence requirements
        if not req_checks:
            _record_check("required_checks_contract", "FAIL", "No declared required verification checks")
            return _build_and_validate_result("HOLD", err_list=["Controlled execution requires at least one declared verification check"])

        desc_verification_checks = self.descriptor.get("verification", {}).get("checks", {})
        for check_id in req_checks:
            if check_id not in desc_verification_checks:
                _record_check("required_checks_contract", "FAIL", f"Unknown check '{check_id}'")
                return _build_and_validate_result("HOLD", err_list=[f"Unknown required verification check ID '{check_id}' not declared in descriptor verification registry"])
        _record_check("required_checks_contract", "PASS")

        # 8. Checkpoint A: Resolve live control ref and build snapshot
        repo = self.descriptor.get("repository")
        control_ref = self.descriptor.get("control_ref")

        source_adapter = self.source_adapter_factory(repo, control_ref)
        try:
            live_control_sha = source_adapter.resolve_ref_to_sha()
        except Exception as e:
            _record_check("canonical_snapshot", "FAIL", str(e))
            return _build_and_validate_result("HOLD", err_list=[f"LIVE_GUARD: Failed to resolve control ref '{control_ref}': {e}"])

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
            _record_check("canonical_snapshot", "FAIL", str(e))
            return _build_and_validate_result("HOLD", control_sha=live_control_sha, err_list=[f"LIVE_GUARD: Failed to build snapshot at SHA {live_control_sha}: {e}"])

        snap_val = validate_document("canonical_project_snapshot", snapshot)
        if not snap_val.is_valid:
            _record_check("canonical_snapshot", "FAIL", "Snapshot schema invalid")
            return _build_and_validate_result("HOLD", control_sha=live_control_sha, err_list=[f"Snapshot schema invalid: {str(e)}" for e in snap_val.errors])
        _record_check("canonical_snapshot", "PASS")

        # Validate execution authority
        auth_result = validate_execution_authority(snapshot, self.task)
        if not auth_result.is_valid:
            _record_check("execution_authority", "FAIL", "; ".join(auth_result.errors))
            return _build_and_validate_result(
                "HOLD",
                control_sha=live_control_sha,
                exec_base_sha=snapshot.get("next_action_execution_base_sha"),
                err_list=auth_result.errors,
            )
        _record_check("execution_authority", "PASS")

        exec_base_sha = snapshot.get("next_action_execution_base_sha")

        # 8b. Execution-Base Revision Resolvability (Verify commit exists in canonical repository)
        try:
            resolved_base_sha = source_adapter.resolve_exact_revision(str(exec_base_sha))
            if resolved_base_sha.lower() != str(exec_base_sha).lower():
                raise ValueError(f"Resolved base SHA '{resolved_base_sha}' does not match expected '{exec_base_sha}'")
            _record_check("execution_base_resolvability", "PASS")
        except Exception as e:
            _record_check("execution_base_resolvability", "FAIL", str(e))
            return _build_and_validate_result(
                "HOLD",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                err_list=[f"Execution base commit '{exec_base_sha}' does not exist or cannot be resolved in repository '{repo}': {e}"],
            )


        # 9. Checkpoint B: Immediately before worker (live_guard_pre_execution)

        try:
            pre_exec_control_sha = source_adapter.resolve_ref_to_sha()
            if pre_exec_control_sha != live_control_sha:
                _record_check("live_guard_pre_execution", "FAIL", f"Control moved to '{pre_exec_control_sha}'")
                return _build_and_validate_result(
                    "HOLD",
                    control_sha=live_control_sha,
                    exec_base_sha=exec_base_sha,
                    err_list=[f"STALE_CONTROL_REVISION_PRE_EXECUTION: Control ref moved from '{live_control_sha}' to '{pre_exec_control_sha}' before execution"],
                )
            _record_check("live_guard_pre_execution", "PASS")
        except Exception as e:
            _record_check("live_guard_pre_execution", "FAIL", str(e))
            return _build_and_validate_result("HOLD", control_sha=live_control_sha, exec_base_sha=exec_base_sha, err_list=[f"LIVE_GUARD pre-execution check failed: {e}"])

        # 10. Setup Isolated Workspace (Disposable Clone)
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
            _record_check("workspace_initial_integrity", "PASS")
        except Exception as e:
            _record_check("workspace_initial_integrity", "FAIL", str(e))
            workspace.cleanup()
            return _build_and_validate_result(
                "HOLD",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                err_list=[f"Failed to setup isolated workspace from base SHA '{exec_base_sha}': {e}"],
            )

        # 11. Invoke Worker Adapter
        timeout_sec = worker_reqs.get("timeout_seconds", 3600)
        allowed_scope = self.task.get("allowed_scope", {})

        worker_failed = False
        worker_err_msg = ""
        w_res: Optional[WorkerExecutionResult] = None

        try:
            w_res = worker_adapter_instance.execute(
                task=self.task,
                workspace_path=workspace_dir,
                allowed_scope=allowed_scope,
                base_sha=exec_base_sha,
                timeout_seconds=timeout_sec,
            )
            worker_identity = w_res.worker_identity
            worker_mutation_attempted = w_res.mutation_attempted
            if w_res.timed_out or (w_res.exit_code is not None and w_res.exit_code != 0):
                worker_failed = True
                worker_err_msg = f"Worker timed out after {timeout_sec}s" if w_res.timed_out else f"Worker exited with code {w_res.exit_code}"
                _record_check("worker_execution", "FAIL", worker_err_msg)
            else:
                _record_check("worker_execution", "PASS")
        except Exception as e:
            worker_failed = True
            worker_mutation_attempted = True
            worker_err_msg = f"Worker execution exception: {e}"
            _record_check("worker_execution", "FAIL", worker_err_msg)

        # Inspect post-worker workspace state
        inspection_error: Optional[str] = None
        try:
            post_worker_head = workspace.get_current_head()
            post_worker_branch = workspace.get_current_branch()
            post_worker_changed_paths = workspace.get_changed_files()
        except Exception as e:
            inspection_error = f"Failed to inspect post-worker workspace: {e}"
            post_worker_head = initial_head
            post_worker_branch = worker_branch
            post_worker_changed_paths = []

        if worker_failed:
            errs = [worker_err_msg]
            if inspection_error:
                errs.append(inspection_error)

            # Build sanitized worker_failure_diagnostics from adapter stdout_summary
            worker_failure_diag = {}
            if w_res and w_res.stdout_summary:
                try:
                    raw_summary = json.loads(w_res.stdout_summary)
                    if isinstance(raw_summary, dict):
                        for k in [
                            "stream_valid",
                            "terminal_status",
                            "failed_step_observed",
                            "failed_tool_observed",
                            "failed_tool_name",
                            "failed_tool_error_type",
                            "permission_soft_denial_observed",
                            "reported_cwd_matches_workspace",
                        ]:
                            if k in raw_summary:
                                worker_failure_diag[k] = raw_summary[k]
                except Exception:
                    pass
            if w_res:
                worker_failure_diag["timed_out"] = w_res.timed_out
                worker_failure_diag["exit_code"] = w_res.exit_code

            # Forensic inspection for partial worker failure quarantine
            scope_res_forensic = None
            forensic_scope_valid = False
            forensic_violations = []
            partial_quarantine_res = None
            extensions_dict = {}

            if worker_failure_diag:
                extensions_dict["worker_failure_diagnostics"] = worker_failure_diag

            # Worker failure quarantine eligibility:
            # 1. mutation attempted and changed_paths non-empty
            # 2. no inspection error
            # 3. head unchanged and branch unchanged
            # 4. scope guard valid
            # 5. zero symlinks / zero remotes in workspace
            if (
                worker_mutation_attempted
                and len(post_worker_changed_paths) > 0
                and not inspection_error
            ):
                # Check Git forensic integrity (HEAD and branch unchanged)
                if post_worker_head == initial_head and post_worker_branch == worker_branch:
                    _record_check("worker_failure_forensic_integrity", "PASS")

                    # Check forensic scope guard
                    scope_res_forensic = validate_scope(
                        changed_paths=post_worker_changed_paths,
                        allowed_scope=allowed_scope,
                    )
                    forensic_scope_valid = scope_res_forensic.is_valid
                    forensic_violations = scope_res_forensic.violations

                    if forensic_scope_valid:
                        _record_check("worker_failure_scope_guard", "PASS")

                        # Attempt to persist partial worker failure quarantine
                        try:
                            q_res = persist_worker_failure_quarantine_candidate(
                                workspace_path=workspace_dir,
                                project_id=project_id,
                                task_id=task_id,
                                gate=gate,
                                control_source_sha=live_control_sha,
                                execution_base_sha=exec_base_sha,
                                worker_branch=worker_branch,
                                initial_head_sha=initial_head,
                                final_head_sha=post_worker_head,
                                worker_changed_paths=post_worker_changed_paths,
                                worker_timed_out=w_res.timed_out if w_res else False,
                                worker_exit_code=w_res.exit_code if w_res else None,
                                source_repo_path=local_target_repo_path,
                            )
                            _record_check("worker_failure_quarantine", "PASS")
                            partial_quarantine_res = q_res
                            extensions_dict["partial_worker_quarantine"] = q_res
                        except Exception as q_err:
                            _record_check("worker_failure_quarantine", "FAIL", str(q_err))
                            errs.append(f"Worker failure partial quarantine failed: {q_err}")
                    else:
                        _record_check("worker_failure_scope_guard", "FAIL", f"Scope violations: {'; '.join(forensic_violations)}")
                        errs.append(f"Worker failure partial scope violation: {'; '.join(forensic_violations)}")
                else:
                    _record_check("worker_failure_forensic_integrity", "FAIL", "Worker modified HEAD or branch")
                    errs.append("Worker failure forensic integrity failed: HEAD or branch modified")

            workspace.cleanup()
            return _build_and_validate_result(
                "WORKER_FAILED",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                worker_branch=worker_branch,
                initial_head=initial_head,
                final_head=post_worker_head,
                changed_paths=post_worker_changed_paths,
                scope_valid=forensic_scope_valid,
                violations=forensic_violations,
                exit_code=w_res.exit_code if w_res else None,
                timed_out=w_res.timed_out if w_res else False,
                mutation_performed=len(post_worker_changed_paths) > 0,
                mutation_attempted=worker_mutation_attempted,
                err_list=errs,
                extensions=extensions_dict if extensions_dict else None,
            )


        if inspection_error:
            _record_check("git_integrity_post_worker", "FAIL", inspection_error)
            workspace.cleanup()
            return _build_and_validate_result(
                "VERIFICATION_FAILED",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                worker_branch=worker_branch,
                initial_head=initial_head,
                final_head=post_worker_head,
                changed_paths=post_worker_changed_paths,
                exit_code=w_res.exit_code if w_res else 0,
                timed_out=w_res.timed_out if w_res else False,
                mutation_performed=len(post_worker_changed_paths) > 0,
                mutation_attempted=worker_mutation_attempted,
                err_list=[inspection_error],
            )

        # 12. Post-Worker Git Integrity
        git_integrity_post_errors = []
        if post_worker_branch != worker_branch:
            git_integrity_post_errors.append(f"UNAUTHORIZED_WORKER_COMMIT_OR_HEAD_CHANGE: Current branch '{post_worker_branch}' != expected '{worker_branch}'")
        if post_worker_head != initial_head:
            git_integrity_post_errors.append(f"UNAUTHORIZED_WORKER_COMMIT_OR_HEAD_CHANGE: Worker created commit or moved HEAD ('{initial_head}' -> '{post_worker_head}')")

        if git_integrity_post_errors:
            _record_check("git_integrity_post_worker", "FAIL", "; ".join(git_integrity_post_errors))
            workspace.cleanup()
            return _build_and_validate_result(
                "VERIFICATION_FAILED",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                worker_branch=worker_branch,
                initial_head=initial_head,
                final_head=post_worker_head,
                changed_paths=post_worker_changed_paths,
                exit_code=w_res.exit_code if w_res else 0,
                timed_out=w_res.timed_out if w_res else False,
                mutation_performed=len(post_worker_changed_paths) > 0,
                mutation_attempted=worker_mutation_attempted,
                err_list=git_integrity_post_errors,
            )
        _record_check("git_integrity_post_worker", "PASS")

        # 13. Post-Worker Scope Guard
        post_worker_scope = validate_scope(post_worker_changed_paths, allowed_scope)
        if not post_worker_scope.is_valid:
            _record_check("scope_guard_post_worker", "FAIL", "; ".join(post_worker_scope.violations))
            workspace.cleanup()
            return _build_and_validate_result(
                "VERIFICATION_FAILED",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                worker_branch=worker_branch,
                initial_head=initial_head,
                final_head=post_worker_head,
                changed_paths=post_worker_changed_paths,
                scope_valid=False,
                violations=post_worker_scope.violations,
                exit_code=w_res.exit_code if w_res else 0,
                timed_out=w_res.timed_out if w_res else False,
                mutation_performed=len(post_worker_changed_paths) > 0,
                mutation_attempted=worker_mutation_attempted,
                err_list=post_worker_scope.violations,
            )
        _record_check("scope_guard_post_worker", "PASS")

        # Capture post-worker clean workspace baseline state for immutability verification & quarantine eligibility
        ws_path_obj = Path(workspace_dir).resolve()
        post_worker_boundary_state = inspect_workspace_boundary_state(ws_path_obj)
        worker_clean_for_quarantine = True

        # 14. Checkpoint C: Post-Worker LIVE_GUARD (Before Project Verification)
        try:
            post_worker_control_sha = source_adapter.resolve_ref_to_sha()
            if post_worker_control_sha != live_control_sha:
                _record_check("live_guard_post_worker", "FAIL", f"Control moved to '{post_worker_control_sha}'")
                workspace.cleanup()
                return _build_and_validate_result(
                    "HOLD",
                    control_sha=live_control_sha,
                    exec_base_sha=exec_base_sha,
                    worker_branch=worker_branch,
                    initial_head=initial_head,
                    final_head=post_worker_head,
                    changed_paths=post_worker_changed_paths,
                    scope_valid=True,
                    violations=[],
                    exit_code=w_res.exit_code if w_res else 0,
                    timed_out=w_res.timed_out if w_res else False,
                    mutation_performed=len(post_worker_changed_paths) > 0,
                    mutation_attempted=worker_mutation_attempted,
                    err_list=[f"STALE_CONTROL_REVISION_POST_WORKER: Control ref moved from '{live_control_sha}' to '{post_worker_control_sha}' during worker execution"],
                )
            _record_check("live_guard_post_worker", "PASS")
        except Exception as e:
            _record_check("live_guard_post_worker", "FAIL", str(e))
            workspace.cleanup()
            return _build_and_validate_result("HOLD", control_sha=live_control_sha, exec_base_sha=exec_base_sha, err_list=[f"Post-worker LIVE_GUARD check failed: {e}"])

        # 15. Execute Required Project Verification Checks with Isolated Verification Workspaces & Sanitized Diagnostics
        sanitized_env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_VARS}
        verification_failed = False
        verification_err_msgs: List[str] = []
        verification_diagnostics: List[Dict[str, Any]] = []

        for check_id in req_checks:
            check_def = desc_verification_checks[check_id]
            argv = check_def["argv"]
            timeout = check_def.get("timeout_seconds", 300)
            check_key = f"project:{check_id}"

            # Create fresh, verified disposable copy of worker workspace for this check
            copy_manager = VerificationWorkspaceCopy(ws_path_obj, check_id=check_id)
            try:
                with copy_manager as verify_copy_dir:
                    c_res = self.verification_runner(argv, str(verify_copy_dir), timeout, sanitized_env)

                    stdout_str = c_res.stdout if isinstance(c_res.stdout, str) else (c_res.stdout or b"").decode("utf-8", errors="replace")
                    stderr_str = c_res.stderr if isinstance(c_res.stderr, str) else (c_res.stderr or b"").decode("utf-8", errors="replace")

                    stdout_bytes = stdout_str.encode("utf-8")
                    stderr_bytes = stderr_str.encode("utf-8")

                    stdout_sha = hashlib.sha256(stdout_bytes).hexdigest()
                    stderr_sha = hashlib.sha256(stderr_bytes).hexdigest()

                    # Sanitize tails
                    sanitized_stdout = sanitize_diagnostic_text(
                        stdout_str,
                        worker_ws_path=str(ws_path_obj),
                        verify_ws_path=str(verify_copy_dir),
                        source_repo_path=local_target_repo_path,
                    )
                    sanitized_stderr = sanitize_diagnostic_text(
                        stderr_str,
                        worker_ws_path=str(ws_path_obj),
                        verify_ws_path=str(verify_copy_dir),
                        source_repo_path=local_target_repo_path,
                    )

                    diag_entry = {
                        "check_id": check_id,
                        "exit_code": c_res.returncode,
                        "timed_out": False,
                        "stdout_present": len(stdout_str) > 0,
                        "stdout_len": len(stdout_bytes),
                        "stdout_sha256": stdout_sha,
                        "stderr_present": len(stderr_str) > 0,
                        "stderr_len": len(stderr_bytes),
                        "stderr_sha256": stderr_sha,
                        "sanitized_stdout_tail": sanitized_stdout[-DIAGNOSTIC_TAIL_MAX_CHARS:] if sanitized_stdout else "",
                        "sanitized_stderr_tail": sanitized_stderr[-DIAGNOSTIC_TAIL_MAX_CHARS:] if sanitized_stderr else "",
                    }

                    verification_diagnostics.append(diag_entry)

                    if c_res.returncode == 0:
                        _record_check(check_key, "PASS", "Exit code 0")
                    else:
                        verification_failed = True
                        msg = f"Check '{check_id}' failed with exit code {c_res.returncode}"
                        verification_err_msgs.append(msg)
                        _record_check(check_key, "FAIL", msg)

            except subprocess.TimeoutExpired as te:
                verification_failed = True
                msg = f"Check '{check_id}' timed out after {timeout}s"
                verification_err_msgs.append(msg)
                _record_check(check_key, "FAIL", msg)
                verification_diagnostics.append({
                    "check_id": check_id,
                    "exit_code": None,
                    "timed_out": True,
                    "stdout_present": False,
                    "stdout_len": 0,
                    "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                    "stderr_present": False,
                    "stderr_len": 0,
                    "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                    "sanitized_stdout_tail": "",
                    "sanitized_stderr_tail": f"Timeout expired after {timeout}s",
                })
            except Exception as e:
                verification_failed = True
                msg = f"Check '{check_id}' error: {e}"
                verification_err_msgs.append(msg)
                _record_check(check_key, "FAIL", msg)
                verification_diagnostics.append({
                    "check_id": check_id,
                    "exit_code": None,
                    "timed_out": False,
                    "stdout_present": False,
                    "stdout_len": 0,
                    "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                    "stderr_present": True,
                    "stderr_len": len(str(e)),
                    "stderr_sha256": hashlib.sha256(str(e).encode("utf-8")).hexdigest(),
                    "sanitized_stdout_tail": "",
                    "sanitized_stderr_tail": sanitize_diagnostic_text(
                        str(e),
                        worker_ws_path=str(ws_path_obj),
                        source_repo_path=local_target_repo_path,
                    )[-DIAGNOSTIC_TAIL_MAX_CHARS:],
                })

        # 16. Post-Verification Original Workspace Immutability & Final State Inspection
        original_workspace_matches_post_worker_baseline = True
        try:
            current_boundary_state = inspect_workspace_boundary_state(ws_path_obj)
            final_head = workspace.get_current_head()
            final_branch = workspace.get_current_branch()
            final_changed_paths = workspace.get_changed_files()

            # Verify original worker workspace was not altered by verification checks
            if current_boundary_state["head"] != post_worker_boundary_state["head"]:
                verification_failed = True
                original_workspace_matches_post_worker_baseline = False
                verification_err_msgs.append(f"ORIGINAL_WORKSPACE_MUTATION: HEAD moved from '{post_worker_boundary_state['head']}' to '{current_boundary_state['head']}'")
            if current_boundary_state["branch"] != post_worker_boundary_state["branch"]:
                verification_failed = True
                original_workspace_matches_post_worker_baseline = False
                verification_err_msgs.append(f"ORIGINAL_WORKSPACE_MUTATION: Branch changed from '{post_worker_boundary_state['branch']}' to '{current_boundary_state['branch']}'")
            if current_boundary_state["changed_paths"] != post_worker_boundary_state["changed_paths"]:
                verification_failed = True
                original_workspace_matches_post_worker_baseline = False
                verification_err_msgs.append(f"ORIGINAL_WORKSPACE_MUTATION: Changed paths modified ({post_worker_boundary_state['changed_paths']} -> {current_boundary_state['changed_paths']})")
            if current_boundary_state["file_states"] != post_worker_boundary_state["file_states"]:
                verification_failed = True
                original_workspace_matches_post_worker_baseline = False
                verification_err_msgs.append("ORIGINAL_WORKSPACE_MUTATION: File states or hashes were modified in original worker workspace")
        except Exception as e:
            final_head = post_worker_head
            final_branch = post_worker_branch
            final_changed_paths = post_worker_changed_paths
            verification_failed = True
            original_workspace_matches_post_worker_baseline = False
            verification_err_msgs.append(f"Failed to inspect final workspace state: {e}")


        # 17. Final Git Integrity Check
        git_integrity_final_errors = []
        if final_branch != worker_branch:
            git_integrity_final_errors.append(f"UNAUTHORIZED_VERIFICATION_COMMIT_OR_HEAD_CHANGE: Current branch '{final_branch}' != expected '{worker_branch}'")
        if final_head != initial_head:
            git_integrity_final_errors.append(f"UNAUTHORIZED_VERIFICATION_COMMIT_OR_HEAD_CHANGE: Verification moved HEAD ('{initial_head}' -> '{final_head}')")

        if git_integrity_final_errors:
            _record_check("git_integrity_final", "FAIL", "; ".join(git_integrity_final_errors))
            verification_failed = True
            verification_err_msgs.extend(git_integrity_final_errors)
        else:
            _record_check("git_integrity_final", "PASS")

        # 18. Final Scope Guard
        final_scope = validate_scope(final_changed_paths, allowed_scope)
        if not final_scope.is_valid:
            _record_check("scope_guard_final", "FAIL", "; ".join(final_scope.violations))
            verification_failed = True
            verification_err_msgs.extend(final_scope.violations)
        else:
            _record_check("scope_guard_final", "PASS")

        # Failure handling with Quarantine Persistence
        if verification_failed:
            quarantine_meta: Optional[Dict[str, Any]] = None
            if worker_clean_for_quarantine and post_worker_scope.is_valid:
                if original_workspace_matches_post_worker_baseline:
                    try:
                        quarantine_meta = persist_quarantine_candidate(
                            workspace_path=workspace_dir,
                            project_id=str(project_id) if project_id else "unknown",
                            task_id=str(task_id) if task_id else "unknown",
                            gate=str(gate) if gate else "unknown",
                            control_source_sha=live_control_sha,
                            execution_base_sha=exec_base_sha,
                            worker_branch=worker_branch,
                            initial_head_sha=initial_head,
                            final_head_sha=final_head,
                            worker_changed_paths=post_worker_changed_paths,
                            source_repo_path=local_target_repo_path,
                        )
                        _record_check("failure_candidate_quarantine", "PASS", f"Quarantined unverified candidate {quarantine_meta.get('quarantine_id')}")
                    except Exception as qe:
                        _record_check("failure_candidate_quarantine", "FAIL", str(qe))
                        verification_err_msgs.append(f"Failed candidate quarantine persistence failed: {qe}")
                else:
                    _record_check("failure_candidate_quarantine", "FAIL", "Original worker workspace modified during verification; quarantine rejected")
            else:
                _record_check("failure_candidate_quarantine", "NOT_RUN")

            _record_check("candidate_persistence", "NOT_RUN")

            workspace.cleanup()

            ext_dict: Dict[str, Any] = {}
            if verification_diagnostics:
                ext_dict["verification_diagnostics"] = verification_diagnostics
            if quarantine_meta:
                ext_dict["quarantine_candidate"] = quarantine_meta

            return _build_and_validate_result(
                "VERIFICATION_FAILED",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                worker_branch=worker_branch,
                initial_head=initial_head,
                final_head=final_head,
                changed_paths=final_changed_paths,
                scope_valid=final_scope.is_valid,
                violations=final_scope.violations,
                exit_code=w_res.exit_code if w_res else 0,
                timed_out=w_res.timed_out if w_res else False,
                mutation_performed=len(final_changed_paths) > 0,
                mutation_attempted=worker_mutation_attempted,
                err_list=verification_err_msgs,
                extensions=ext_dict if ext_dict else None,
            )

        # 19. Checkpoint D: Final LIVE_GUARD (Before Candidate Persistence)
        try:
            final_control_sha = source_adapter.resolve_ref_to_sha()
            if final_control_sha != live_control_sha:
                _record_check("live_guard_final", "FAIL", f"Control moved to '{final_control_sha}'")

                # Quarantining candidate on final LIVE_GUARD failure if scope clean and boundary unchanged
                quarantine_meta = None
                if worker_clean_for_quarantine and post_worker_scope.is_valid:
                    if original_workspace_matches_post_worker_baseline:
                        try:
                            quarantine_meta = persist_quarantine_candidate(
                                workspace_path=workspace_dir,
                                project_id=str(project_id) if project_id else "unknown",
                                task_id=str(task_id) if task_id else "unknown",
                                gate=str(gate) if gate else "unknown",
                                control_source_sha=live_control_sha,
                                execution_base_sha=exec_base_sha,
                                worker_branch=worker_branch,
                                initial_head_sha=initial_head,
                                final_head_sha=final_head,
                                worker_changed_paths=post_worker_changed_paths,
                                source_repo_path=local_target_repo_path,
                            )
                            _record_check("failure_candidate_quarantine", "PASS", f"Quarantined unverified candidate {quarantine_meta.get('quarantine_id')}")
                        except Exception as qe:
                            _record_check("failure_candidate_quarantine", "FAIL", str(qe))
                    else:
                        _record_check("failure_candidate_quarantine", "FAIL", "Original worker workspace modified during verification; quarantine rejected")
                else:
                    _record_check("failure_candidate_quarantine", "NOT_RUN")


                workspace.cleanup()
                ext_dict = {}
                if verification_diagnostics:
                    ext_dict["verification_diagnostics"] = verification_diagnostics
                if quarantine_meta:
                    ext_dict["quarantine_candidate"] = quarantine_meta

                return _build_and_validate_result(
                    "HOLD",
                    control_sha=live_control_sha,
                    exec_base_sha=exec_base_sha,
                    worker_branch=worker_branch,
                    initial_head=initial_head,
                    final_head=final_head,
                    changed_paths=final_changed_paths,
                    scope_valid=True,
                    violations=[],
                    exit_code=w_res.exit_code if w_res else 0,
                    timed_out=w_res.timed_out if w_res else False,
                    mutation_performed=len(final_changed_paths) > 0,
                    mutation_attempted=worker_mutation_attempted,
                    err_list=[f"STALE_CONTROL_REVISION_FINAL: Control ref moved from '{live_control_sha}' to '{final_control_sha}' after verification"],
                    extensions=ext_dict if ext_dict else None,
                )
            _record_check("live_guard_final", "PASS")
        except Exception as e:
            _record_check("live_guard_final", "FAIL", str(e))
            workspace.cleanup()
            return _build_and_validate_result("HOLD", control_sha=live_control_sha, exec_base_sha=exec_base_sha, err_list=[f"Final LIVE_GUARD check failed: {e}"])

        # Failure candidate quarantine is NOT_RUN on successful path
        _record_check("failure_candidate_quarantine", "NOT_RUN")

        # 20. Candidate Persistence: Persist verified candidate workspace to machine-local store
        try:
            candidate_metadata = persist_verified_candidate(
                workspace_path=workspace_dir,
                project_id=str(project_id) if project_id else "unknown",
                task_id=str(task_id) if task_id else "unknown",
                gate=str(gate) if gate else "unknown",
                control_source_sha=live_control_sha,
                execution_base_sha=exec_base_sha,
                worker_branch=worker_branch,
                initial_head_sha=initial_head,
                final_head_sha=final_head,
                changed_paths=final_changed_paths,
                source_repo_path=local_target_repo_path,
            )
            _record_check("candidate_persistence", "PASS", f"Persisted candidate {candidate_metadata.get('candidate_id')}")
        except Exception as e:
            _record_check("candidate_persistence", "FAIL", str(e))
            workspace.cleanup()
            return _build_and_validate_result(
                "VERIFICATION_FAILED",
                control_sha=live_control_sha,
                exec_base_sha=exec_base_sha,
                worker_branch=worker_branch,
                initial_head=initial_head,
                final_head=final_head,
                changed_paths=final_changed_paths,
                scope_valid=True,
                violations=[],
                exit_code=w_res.exit_code if w_res else 0,
                timed_out=w_res.timed_out if w_res else False,
                mutation_performed=len(final_changed_paths) > 0,
                mutation_attempted=worker_mutation_attempted,
                err_list=[f"Candidate persistence failed: {e}"],
            )

        # 21. Cleanup disposable workspace now that candidate is persisted
        workspace.cleanup()

        # 22. All checks passed -> VERIFIED_CANDIDATE with candidate extension & diagnostics
        res_extensions: Dict[str, Any] = {"candidate": candidate_metadata}
        if verification_diagnostics:
            res_extensions["verification_diagnostics"] = verification_diagnostics

        return _build_and_validate_result(
            "VERIFIED_CANDIDATE",
            control_sha=live_control_sha,
            exec_base_sha=exec_base_sha,
            worker_branch=worker_branch,
            initial_head=initial_head,
            final_head=final_head,
            changed_paths=final_changed_paths,
            scope_valid=True,
            violations=[],
            exit_code=w_res.exit_code if w_res else 0,
            timed_out=w_res.timed_out if w_res else False,
            mutation_performed=len(final_changed_paths) > 0,
            mutation_attempted=worker_mutation_attempted,
            extensions=res_extensions,
        )
