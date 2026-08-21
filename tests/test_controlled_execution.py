"""Offline regression tests for AOS-3 Controlled Single-Worker Execution Engine."""

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from aos.controlled_execution import ControlledExecutionEngine
from aos.git_workspace import GitWorkspace, enforce_aos_branch_namespace, normalize_github_repository_name
from aos.source_adapter import ProjectSourceAdapter
from aos.workers.base import WorkerAdapter, WorkerExecutionResult


class MockSourceAdapter(ProjectSourceAdapter):
    def __init__(
        self,
        repo: str,
        ref: str,
        control_sha: str = "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
        second_control_sha: Optional[str] = None,
        stale_on_call: Optional[int] = None,
        exec_base_sha: str = "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
        has_exec_base: bool = True,
        has_ambiguity: bool = False,
    ):
        super().__init__(repo, ref)
        self.control_sha = control_sha
        self.second_control_sha = second_control_sha or control_sha
        self.stale_on_call = stale_on_call
        self.resolve_call_count = 0
        self.exec_base_sha = exec_base_sha
        self.has_exec_base = has_exec_base
        self.has_ambiguity = has_ambiguity

    def resolve_ref_to_sha(self) -> str:
        self.resolve_call_count += 1
        if self.stale_on_call is not None and self.resolve_call_count >= self.stale_on_call:
            return self.second_control_sha
        return self.control_sha

    def fetch_canonical_context(self, exact_sha: str, paths: Dict[str, str]):
        contents = {
            "state": json.dumps({
                "schema_version": "0.1.0",
                "current_status": "READY",
                "current_milestone": "M1",
                "next_action": "Do task",
                "next_action_execution_base_sha": self.exec_base_sha if self.has_exec_base else None,
            }),
            "decisions": "# Decisions",
            "evidence": "",
            "roadmap": "# Roadmap",
        }
        hashes = {v: "0000000000000000000000000000000000000000000000000000000000000000" for v in paths.values()}
        return contents, hashes

    def build_normalized_snapshot(self, project_id, exact_sha, raw_contents, file_hashes, projection_config=None):
        return {
            "schema_version": "0.1.0",
            "project_id": project_id,
            "repository": self.repository,
            "source_ref": self.control_ref,
            "source_sha": exact_sha,
            "current_status": "READY",
            "current_milestone": "M1",
            "canonical_next_action": "Do task",
            "target_base_sha": "65a53427f52c21e60aa8f92e02a17d693a201601",
            "next_action_execution_base_sha": self.exec_base_sha if self.has_exec_base else None,
            "has_ambiguity": self.has_ambiguity,
            "ambiguity_reasons": ["Ambiguity mock"] if self.has_ambiguity else [],
            "input_file_hashes": file_hashes,
        }


class MockGitWorkspace(GitWorkspace):
    def __init__(
        self,
        repo,
        base_sha,
        task_id,
        branch_name=None,
        changed_files=None,
        setup_fails=False,
        final_head_sha=None,
        current_branch=None,
        origin_repo="GenericOrg/GenericRepo",
    ):
        super().__init__(repo, base_sha, task_id, branch_name)
        self.mock_changed_files = changed_files if changed_files is not None else ["src/app.py"]
        self.setup_fails = setup_fails
        self.mock_final_head_sha = final_head_sha or base_sha
        self.mock_current_branch = current_branch or f"aos/{task_id.lower()}"
        self.origin_repo = origin_repo
        self.cleaned_up = False
        self.head_call_count = 0

    def get_origin_repository_name(self) -> Optional[str]:
        return self.origin_repo

    def setup(self) -> str:
        if self.setup_fails:
            raise RuntimeError("Mock git workspace setup failed")
        self.workspace_dir = "/tmp/mock_workspace"
        self.initial_head_sha = self.base_sha
        return self.workspace_dir

    def get_current_head(self) -> str:
        self.head_call_count += 1
        if self.head_call_count == 1:
            return self.base_sha
        return self.mock_final_head_sha

    def get_current_branch(self) -> str:
        return self.mock_current_branch

    def get_status(self) -> List[str]:
        return [f" M {f}" for f in self.mock_changed_files]

    def get_changed_files(self, from_sha=None) -> List[str]:
        return self.mock_changed_files

    def cleanup(self) -> None:
        self.cleaned_up = True


class MockTestDoubleWorkerAdapter(WorkerAdapter):
    capability_status: str = "TEST_DOUBLE"

    def __init__(self, exit_code=0, timed_out=False, raises=False):
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.raises = raises
        self.executed = False

    def execute(self, task, workspace_path, allowed_scope, base_sha, timeout_seconds=3600):
        self.executed = True
        if self.raises:
            raise RuntimeError("Mock worker explosion")
        return WorkerExecutionResult(
            worker_identity="test-double-worker",
            workspace_path=workspace_path,
            exit_code=self.exit_code,
            timed_out=self.timed_out,
            stdout_summary="Mock stdout",
            stderr_summary="Mock stderr" if self.exit_code != 0 else "",
            mutation_attempted=True,
        )


def make_generic_descriptor(project_id="generic_project", repo="GenericOrg/GenericRepo"):
    return {
        "schema_version": "0.1.0",
        "project_id": project_id,
        "repository": repo,
        "control_ref": "control/main",
        "control": {
            "state": "docs/STATE.json",
            "decisions": "docs/DECISIONS.md",
            "evidence": "docs/EVIDENCE.jsonl",
            "roadmap": "docs/ROADMAP.md",
        },
        "projection": {
            "current_status_pointer": "/current_status",
            "current_milestone_pointer": "/current_milestone",
            "canonical_next_action_pointer": "/next_action",
            "next_action_execution_base_sha_pointer": "/next_action_execution_base_sha",
            "next_action_execution_base_sha_required": True,
        },
        "authority": {
            "production_mutation": "human_required",
            "roadmap_change": "human_required",
            "destructive_data": "human_required",
        },
        "verification": {
            "checks": {
                "unit_tests": {
                    "argv": ["python", "-m", "pytest", "-q"],
                    "timeout_seconds": 300,
                }
            }
        },
    }


def make_generic_task(
    project_id="generic_project",
    gate="AOS-3",
    risk_class="R1",
    base_sha="5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
    isolated_worktree=True,
    adapter="antigravity",
    max_retries=1,
    paths=None,
    forbidden_paths=None,
    branch_name="feature/my-task",
    required_checks=None,
):
    return {
        "schema_version": "0.1.0",
        "project_id": project_id,
        "task_id": "TASK-301",
        "gate": gate,
        "title": "Generic Task Title",
        "description": "Generic Task Description",
        "risk_class": risk_class,
        "base_sha": base_sha,
        "branch_name": branch_name,
        "allowed_scope": {
            "paths": paths or ["src/"],
            "forbidden_paths": forbidden_paths or ["docs/CHARTER.md"],
        },
        "worker_requirements": {
            "adapter": adapter,
            "isolated_worktree": isolated_worktree,
        },
        "evidence_requirements": {
            "minimum_level": "E3_ISOLATED_RUNTIME_PROVEN",
            "required_checks": required_checks if required_checks is not None else ["unit_tests"],
        },
        "retry_policy": {
            "max_retries": max_retries,
        },
    }


def mock_pass_verification_runner(argv, cwd, timeout):
    return subprocess.CompletedProcess(argv, 0, stdout="OK", stderr="")


def mock_fail_verification_runner(argv, cwd, timeout):
    return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Tests failed")


class TestControlledExecutionEngineHardened:
    def test_valid_generic_r1_aos3_task_reaches_verified_candidate(self):
        """1. Valid generic R1/AOS-3 task with TEST_DOUBLE adapter and PASS check reaches VERIFIED_CANDIDATE."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockTestDoubleWorkerAdapter()
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"])

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
            verification_runner=mock_pass_verification_runner,
        )

        res = engine.execute(local_target_repo_path="/path/to/local/repo")
        assert res["disposition"] == "VERIFIED_CANDIDATE"
        assert res["control_source_sha"] == "4c55eecdbe064c74b34af31a1daf9851689e4fe8"
        assert res["verification_checks"] == [{"check_id": "unit_tests", "status": "PASS", "message": "Exit code 0"}]
        assert mock_worker.executed is True

    def test_unresolved_control_sha_is_null_not_all_zeros(self):
        """2. Early HOLD returns control_source_sha = None, not all-zeros."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_worker = MockTestDoubleWorkerAdapter()

        engine = ControlledExecutionEngine(
            desc, task,
            worker_adapter_factory=lambda: mock_worker,
        )

        # Missing target repo -> early HOLD
        res = engine.execute(local_target_repo_path=None)
        assert res["disposition"] == "HOLD"
        assert res["control_source_sha"] is None
        assert res["verification_checks"] == []
        assert mock_worker.executed is False

    def test_unproven_antigravity_capability_holds_zero_worker_calls(self):
        """3. Live AntigravityWorkerAdapter (capability_status='UNPROVEN') returns HOLD and 0 worker calls."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")

        # Standard AntigravityWorkerAdapter default has UNPROVEN status
        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
        )

        res = engine.execute(local_target_repo_path="/path/to/local/repo")
        assert res["disposition"] == "HOLD"
        assert any("UNPROVEN" in e for e in res["errors"])

    def test_no_required_checks_for_r1_task_holds(self):
        """4. R1 task with empty required_checks returns HOLD."""
        desc = make_generic_descriptor()
        task = make_generic_task(required_checks=[])
        mock_worker = MockTestDoubleWorkerAdapter()

        engine = ControlledExecutionEngine(
            desc, task,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute(local_target_repo_path="/path/to/local/repo")
        assert res["disposition"] == "HOLD"
        assert any("requires at least one declared verification check" in e for e in res["errors"])
        assert mock_worker.executed is False

    def test_unknown_required_check_holds(self):
        """5. Task requesting check not in descriptor verification registry returns HOLD."""
        desc = make_generic_descriptor()
        task = make_generic_task(required_checks=["unknown_check_id"])
        mock_worker = MockTestDoubleWorkerAdapter()

        engine = ControlledExecutionEngine(
            desc, task,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute(local_target_repo_path="/path/to/local/repo")
        assert res["disposition"] == "HOLD"
        assert any("Unknown required verification check ID" in e for e in res["errors"])
        assert mock_worker.executed is False

    def test_verification_check_failure_returns_verification_failed(self):
        """6. Failing verification check (nonzero exit) returns VERIFICATION_FAILED."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockTestDoubleWorkerAdapter()
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"])

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
            verification_runner=mock_fail_verification_runner,
        )

        res = engine.execute(local_target_repo_path="/path/to/local/repo")
        assert res["disposition"] == "VERIFICATION_FAILED"
        assert res["verification_checks"] == [{"check_id": "unit_tests", "status": "FAIL", "message": "Check 'unit_tests' failed with exit code 1"}]

    def test_post_worker_live_guard_stale_control_holds(self):
        """7. Control ref moving during worker execution triggers post-worker LIVE_GUARD HOLD."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter(
            "GenericOrg/GenericRepo", "control/main",
            control_sha="4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            second_control_sha="9999999999999999999999999999999999999999",  # Moved!
            stale_on_call=3,  # Post-worker check is call #3
        )
        mock_worker = MockTestDoubleWorkerAdapter()
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"])

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
            verification_runner=mock_pass_verification_runner,
        )

        res = engine.execute(local_target_repo_path="/path/to/local/repo")
        assert res["disposition"] == "HOLD"
        assert any("STALE_CONTROL_REVISION_POST_EXECUTION" in e for e in res["errors"])

    def test_repository_identity_mismatch_holds_zero_worker(self):
        """8. Target local repo origin != descriptor repository returns HOLD and 0 worker calls."""
        desc = make_generic_descriptor(repo="OrgA/RepoA")
        task = make_generic_task()
        mock_worker = MockTestDoubleWorkerAdapter()
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"], origin_repo="OrgB/RepoB")

        engine = ControlledExecutionEngine(
            desc, task,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute(local_target_repo_path="/path/to/local/repo")
        assert res["disposition"] == "HOLD"
        assert any("Repository identity mismatch" in e for e in res["errors"])
        assert mock_worker.executed is False

    def test_worker_commit_or_head_change_verification_failed(self):
        """9. Worker creating commit or moving HEAD returns VERIFICATION_FAILED."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockTestDoubleWorkerAdapter()
        # HEAD moved from base_sha to different SHA
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"], final_head_sha="8888888888888888888888888888888888888888")

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
            verification_runner=mock_pass_verification_runner,
        )

        res = engine.execute(local_target_repo_path="/path/to/local/repo")
        assert res["disposition"] == "VERIFICATION_FAILED"
        assert any("UNAUTHORIZED_WORKER_COMMIT_OR_HEAD_CHANGE" in e for e in res["errors"])

    def test_worker_branch_switch_verification_failed(self):
        """10. Worker switching branch returns VERIFICATION_FAILED."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockTestDoubleWorkerAdapter()
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"], current_branch="main")

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
            verification_runner=mock_pass_verification_runner,
        )

        res = engine.execute(local_target_repo_path="/path/to/local/repo")
        assert res["disposition"] == "VERIFICATION_FAILED"
        assert any("Current branch 'main' != expected" in e for e in res["errors"])

    def test_github_repository_name_normalization(self):
        """11. normalize_github_repository_name handles HTTPS, SSH, and raw owner/repo."""
        assert normalize_github_repository_name("https://github.com/MertSGI/AOS.git") == "MertSGI/AOS"
        assert normalize_github_repository_name("git@github.com:MertSGI/AOS.git") == "MertSGI/AOS"
        assert normalize_github_repository_name("MertSGI/AOS") == "MertSGI/AOS"
        assert normalize_github_repository_name("https://github.com/Org/Repo") == "Org/Repo"

    def test_no_lari_identifiers_in_hardened_core(self):
        """12. Core engine files contain no hardcoded LARI identifiers."""
        for path_name in ["controlled_execution.py", "git_workspace.py", "scope_guard.py"]:
            source_code = (Path(__file__).parent.parent / "src" / "aos" / path_name).read_text(encoding="utf-8")
            assert "lari" not in source_code.lower()
            assert "clinic" not in source_code.lower()
            assert "supabase" not in source_code.lower()
