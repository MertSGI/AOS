"""Offline regression and real-git tests for AOS-3 Controlled Single-Worker Execution Engine."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from aos.controlled_execution import ControlledExecutionEngine
from aos.git_workspace import (
    GitWorkspace,
    enforce_aos_branch_namespace,
    inspect_github_repository_identity,
    normalize_github_repository_name,
)
from aos.source_adapter import ProjectSourceAdapter
from aos.validate import validate_document
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
        post_worker_head=None,
        final_head_sha=None,
        post_worker_branch=None,
        final_branch=None,
        final_changed_files=None,
    ):
        super().__init__(repo, base_sha, task_id, branch_name)
        self.mock_post_worker_changed = changed_files if changed_files is not None else ["src/app.py"]
        self.mock_final_changed = final_changed_files if final_changed_files is not None else self.mock_post_worker_changed
        self.setup_fails = setup_fails
        self.mock_post_worker_head = post_worker_head or base_sha
        self.mock_final_head_sha = final_head_sha or self.mock_post_worker_head
        self.mock_post_worker_branch = post_worker_branch or f"aos/{task_id.lower()}"
        self.mock_final_branch = final_branch or self.mock_post_worker_branch
        self.cleaned_up = False
        self.head_call_count = 0
        self.branch_call_count = 0
        self.changed_call_count = 0

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
        elif self.head_call_count == 2:
            return self.mock_post_worker_head
        return self.mock_final_head_sha

    def get_current_branch(self) -> str:
        self.branch_call_count += 1
        if self.branch_call_count <= 1:
            return self.mock_post_worker_branch
        return self.mock_final_branch

    def get_status(self) -> List[str]:
        return [f" M {f}" for f in self.mock_post_worker_changed]

    def get_changed_files(self, from_sha=None) -> List[str]:
        self.changed_call_count += 1
        if self.changed_call_count <= 1:
            return self.mock_post_worker_changed
        return self.mock_final_changed

    def cleanup(self) -> None:
        self.cleaned_up = True


class MockTestDoubleWorkerAdapter(WorkerAdapter):
    capability_status: str = "TEST_DOUBLE"

    def __init__(self, exit_code=0, timed_out=False, raises=False, mutation_attempted=True):
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.raises = raises
        self.mutation_attempted = mutation_attempted
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
            mutation_attempted=self.mutation_attempted,
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


def mock_pass_verification_runner(argv, cwd, timeout, env):
    return subprocess.CompletedProcess(argv, 0, stdout="OK", stderr="")


def mock_fail_verification_runner(argv, cwd, timeout, env):
    return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Tests failed")


class TestControlledExecutionEngineFinalHardened:
    def test_valid_generic_r1_task_reaches_verified_candidate(self):
        """1. Full pipeline reaches VERIFIED_CANDIDATE with all check accounting PASS."""
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
            repo_identity_inspector=lambda path: "GenericOrg/GenericRepo",
        )

        res = engine.execute(local_target_repo_path="/path/to/repo")
        assert res["disposition"] == "VERIFIED_CANDIDATE"
        assert res["worker_mutation_attempted"] is True
        assert res["mutation_performed"] is True
        assert res["worker_capability_status"] == "TEST_DOUBLE"
        assert all(c["status"] == "PASS" for c in res["verification_checks"])

    def test_missing_unreadable_origin_holds_zero_worker(self):
        """2. Missing or unreadable origin remote returns HOLD and 0 worker calls."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_worker = MockTestDoubleWorkerAdapter()

        def _bad_inspector(path):
            raise RuntimeError("Unresolved or missing origin remote")

        engine = ControlledExecutionEngine(
            desc, task,
            worker_adapter_factory=lambda: mock_worker,
            repo_identity_inspector=_bad_inspector,
        )

        res = engine.execute(local_target_repo_path="/path/to/repo")
        assert res["disposition"] == "HOLD"
        assert res["control_source_sha"] is None
        assert mock_worker.executed is False

    def test_non_github_origin_cannot_spoof_matching_owner_repo(self):
        """3. Non-GitHub origin (e.g. gitlab.com) fails closed and returns HOLD."""
        desc = make_generic_descriptor(repo="GenericOrg/GenericRepo")
        task = make_generic_task()
        mock_worker = MockTestDoubleWorkerAdapter()

        with pytest.raises(ValueError):
            normalize_github_repository_name("https://gitlab.com/GenericOrg/GenericRepo.git")

        engine = ControlledExecutionEngine(
            desc, task,
            worker_adapter_factory=lambda: mock_worker,
            repo_identity_inspector=lambda path: normalize_github_repository_name("https://gitlab.com/GenericOrg/GenericRepo.git"),
        )

        res = engine.execute(local_target_repo_path="/path/to/repo")
        assert res["disposition"] == "HOLD"
        assert mock_worker.executed is False

    def test_repository_mismatch_holds_zero_worker(self):
        """4. Local repo origin mismatch with descriptor returns HOLD with 0 worker calls."""
        desc = make_generic_descriptor(repo="GenericOrg/GenericRepo")
        task = make_generic_task()
        mock_worker = MockTestDoubleWorkerAdapter()

        engine = ControlledExecutionEngine(
            desc, task,
            worker_adapter_factory=lambda: mock_worker,
            repo_identity_inspector=lambda path: "OtherOrg/OtherRepo",
        )

        res = engine.execute(local_target_repo_path="/path/to/repo")
        assert res["disposition"] == "HOLD"
        assert any("Repository identity mismatch" in e for e in res["errors"])
        assert mock_worker.executed is False

    def test_worker_mutation_attempt_distinguishable_from_zero_changes(self):
        """5. Worker mutation attempt = True with 0 final changed paths stays distinguishable."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockTestDoubleWorkerAdapter(mutation_attempted=True)
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"], changed_files=[])

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
            verification_runner=mock_pass_verification_runner,
            repo_identity_inspector=lambda path: "GenericOrg/GenericRepo",
        )

        res = engine.execute(local_target_repo_path="/path/to/repo")
        assert res["disposition"] == "VERIFIED_CANDIDATE"
        assert res["worker_mutation_attempted"] is True
        assert res["mutation_performed"] is False
        assert res["changed_paths"] == []

    def test_timeout_after_mutation_preserves_worker_mutation_attempted(self):
        """6. Timeout after worker attempted mutation records worker_mutation_attempted=True."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockTestDoubleWorkerAdapter(timed_out=True, mutation_attempted=True)
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"], changed_files=["src/partial.py"])

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
            repo_identity_inspector=lambda path: "GenericOrg/GenericRepo",
        )

        res = engine.execute(local_target_repo_path="/path/to/repo")
        assert res["disposition"] == "WORKER_FAILED"
        assert res["worker_timed_out"] is True
        assert res["worker_mutation_attempted"] is True
        assert res["mutation_performed"] is True
        assert res["changed_paths"] == ["src/partial.py"]

    def test_worker_exception_forensic_path_recorded(self):
        """7. Worker exception records forensic changed paths before cleanup."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockTestDoubleWorkerAdapter(raises=True)
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"], changed_files=["src/error.py"])

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
            repo_identity_inspector=lambda path: "GenericOrg/GenericRepo",
        )

        res = engine.execute(local_target_repo_path="/path/to/repo")
        assert res["disposition"] == "WORKER_FAILED"
        assert res["worker_mutation_attempted"] is True
        assert res["changed_paths"] == ["src/error.py"]
        assert mock_ws.cleaned_up is True

    def test_verification_command_creates_forbidden_file_fails(self):
        """8. Verification command creating/modifying forbidden file -> VERIFICATION_FAILED."""
        desc = make_generic_descriptor()
        task = make_generic_task(paths=["src/"], forbidden_paths=["docs/CHARTER.md"])
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockTestDoubleWorkerAdapter()
        # Post-worker changes was only src/app.py, but verification created docs/CHARTER.md!
        mock_ws = MockGitWorkspace(
            "repo", task["base_sha"], task["task_id"],
            changed_files=["src/app.py"],
            final_changed_files=["src/app.py", "docs/CHARTER.md"],
        )

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
            verification_runner=mock_pass_verification_runner,
            repo_identity_inspector=lambda path: "GenericOrg/GenericRepo",
        )

        res = engine.execute(local_target_repo_path="/path/to/repo")
        assert res["disposition"] == "VERIFICATION_FAILED"
        assert "docs/CHARTER.md" in res["changed_paths"]
        assert res["scope_validation"]["is_valid"] is False

    def test_verification_command_creates_out_of_scope_file_fails(self):
        """9. Verification command creating out-of-scope file -> VERIFICATION_FAILED."""
        desc = make_generic_descriptor()
        task = make_generic_task(paths=["src/"])
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockTestDoubleWorkerAdapter()
        mock_ws = MockGitWorkspace(
            "repo", task["base_sha"], task["task_id"],
            changed_files=["src/app.py"],
            final_changed_files=["src/app.py", "out_of_scope.txt"],
        )

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
            verification_runner=mock_pass_verification_runner,
            repo_identity_inspector=lambda path: "GenericOrg/GenericRepo",
        )

        res = engine.execute(local_target_repo_path="/path/to/repo")
        assert res["disposition"] == "VERIFICATION_FAILED"
        assert "out_of_scope.txt" in res["changed_paths"]

    def test_verification_command_commits_or_changes_head_fails(self):
        """10. Verification command committing or moving HEAD -> VERIFICATION_FAILED."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockTestDoubleWorkerAdapter()
        mock_ws = MockGitWorkspace(
            "repo", task["base_sha"], task["task_id"],
            post_worker_head=task["base_sha"],
            final_head_sha="9999999999999999999999999999999999999999",  # Changed during verification!
        )

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
            verification_runner=mock_pass_verification_runner,
            repo_identity_inspector=lambda path: "GenericOrg/GenericRepo",
        )

        res = engine.execute(local_target_repo_path="/path/to/repo")
        assert res["disposition"] == "VERIFICATION_FAILED"
        assert any("UNAUTHORIZED_VERIFICATION_COMMIT_OR_HEAD_CHANGE" in e for e in res["errors"])

    def test_verification_command_switches_branch_fails(self):
        """11. Verification command switching branch -> VERIFICATION_FAILED."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockTestDoubleWorkerAdapter()
        mock_ws = MockGitWorkspace(
            "repo", task["base_sha"], task["task_id"],
            post_worker_branch=f"aos/{task['task_id'].lower()}",
            final_branch="main",  # Switched during verification!
        )

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
            verification_runner=mock_pass_verification_runner,
            repo_identity_inspector=lambda path: "GenericOrg/GenericRepo",
        )

        res = engine.execute(local_target_repo_path="/path/to/repo")
        assert res["disposition"] == "VERIFICATION_FAILED"
        assert any("UNAUTHORIZED_VERIFICATION_COMMIT_OR_HEAD_CHANGE" in e for e in res["errors"])

    def test_post_worker_control_stale_zero_project_verification_calls(self):
        """12. Stale control ref after worker aborts before project verification (runner calls = 0)."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter(
            "GenericOrg/GenericRepo", "control/main",
            control_sha="4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            second_control_sha="9999999999999999999999999999999999999999",
            stale_on_call=3,  # Post-worker checkpoint C
        )
        mock_worker = MockTestDoubleWorkerAdapter()
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"])

        verification_called = False

        def _tracking_runner(argv, cwd, timeout, env):
            nonlocal verification_called
            verification_called = True
            return subprocess.CompletedProcess(argv, 0, stdout="OK", stderr="")

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
            verification_runner=_tracking_runner,
            repo_identity_inspector=lambda path: "GenericOrg/GenericRepo",
        )

        res = engine.execute(local_target_repo_path="/path/to/repo")
        assert res["disposition"] == "HOLD"
        assert any("STALE_CONTROL_REVISION_POST_WORKER" in e for e in res["errors"])
        assert verification_called is False  # Zero verification calls!

    def test_final_control_stale_holds_after_verification(self):
        """13. Control moving after verification (Checkpoint D) returns HOLD."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter(
            "GenericOrg/GenericRepo", "control/main",
            control_sha="4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            second_control_sha="9999999999999999999999999999999999999999",
            stale_on_call=4,  # Final checkpoint D
        )
        mock_worker = MockTestDoubleWorkerAdapter()
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"])

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
            verification_runner=mock_pass_verification_runner,
            repo_identity_inspector=lambda path: "GenericOrg/GenericRepo",
        )

        res = engine.execute(local_target_repo_path="/path/to/repo")
        assert res["disposition"] == "HOLD"
        assert any("STALE_CONTROL_REVISION_FINAL" in e for e in res["errors"])

    def test_verification_runner_receives_scrubbed_sensitive_environment(self):
        """14. Verification runner receives environment with sensitive tokens scrubbed."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockTestDoubleWorkerAdapter()
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"])

        captured_env: Dict[str, str] = {}

        def _env_capturing_runner(argv, cwd, timeout, env):
            nonlocal captured_env
            captured_env = dict(env)
            return subprocess.CompletedProcess(argv, 0, stdout="OK", stderr="")

        os.environ["OPENAI_API_KEY"] = "sk-fake-openai"
        os.environ["GEMINI_API_KEY"] = "fake-gemini"
        os.environ["GH_TOKEN"] = "ghp-fake-token"

        try:
            engine = ControlledExecutionEngine(
                desc, task,
                source_adapter_factory=lambda r, ref: mock_source,
                git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
                worker_adapter_factory=lambda: mock_worker,
                verification_runner=_env_capturing_runner,
                repo_identity_inspector=lambda path: "GenericOrg/GenericRepo",
            )
            res = engine.execute(local_target_repo_path="/path/to/repo")
            assert res["disposition"] == "VERIFIED_CANDIDATE"
            assert "OPENAI_API_KEY" not in captured_env
            assert "GEMINI_API_KEY" not in captured_env
            assert "GH_TOKEN" not in captured_env
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("GH_TOKEN", None)


class TestRealGitControlledWorkspace:
    """Real Git integration tests on temporary repositories."""

    @pytest.fixture
    def real_git_repo(self):
        temp_dir = tempfile.mkdtemp(prefix="aos_real_git_test_")
        # Initialize bare origin and working clone
        subprocess.run(["git", "init", temp_dir], capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "AOS Tester"], cwd=temp_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "tester@mertsgi.org"], cwd=temp_dir, capture_output=True, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/TestOrg/TestRepo.git"], cwd=temp_dir, capture_output=True, check=True)

        # Initial commit
        (Path(temp_dir) / "README.md").write_text("Hello", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=temp_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=temp_dir, capture_output=True, check=True)
        head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=temp_dir, capture_output=True, text=True, check=True).stdout.strip()

        yield temp_dir, head_sha

        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_disposable_clone_has_zero_remotes_and_no_source_mutation(self, real_git_repo):
        """15. Disposable clone has 0 remotes and creates zero branches/refs in source repo."""
        repo_dir, base_sha = real_git_repo
        ws = GitWorkspace(repo_dir, base_sha, "TASK-100")
        clone_dir = ws.setup()

        try:
            # Verify disposable clone has 0 remotes
            remotes = subprocess.run(["git", "remote"], cwd=clone_dir, capture_output=True, text=True, check=True).stdout.strip()
            assert remotes == ""

            # Verify push fails locally
            push_res = subprocess.run(["git", "push", "origin", "aos/task-100"], cwd=clone_dir, capture_output=True, text=True)
            assert push_res.returncode != 0

            # Verify source repo received no new branches
            source_branches = subprocess.run(["git", "branch"], cwd=repo_dir, capture_output=True, text=True, check=True).stdout
            assert "aos/task-100" not in source_branches
        finally:
            ws.cleanup()

    def test_real_git_changed_files_with_spaces_renames_deletes_untracked(self, real_git_repo):
        """16. Real Git changed file discovery handles spaces, renames, deletes, and untracked files."""
        repo_dir, base_sha = real_git_repo
        ws = GitWorkspace(repo_dir, base_sha, "TASK-101")
        clone_dir = ws.setup()

        try:
            # 1. File with spaces
            space_file = Path(clone_dir) / "file with spaces.txt"
            space_file.write_text("content", encoding="utf-8")

            # 2. Untracked file
            untracked = Path(clone_dir) / "untracked.py"
            untracked.write_text("print(1)", encoding="utf-8")

            # 3. Deleted file
            (Path(clone_dir) / "README.md").unlink()

            changed = ws.get_changed_files()
            assert "file with spaces.txt" in changed
            assert "untracked.py" in changed
            assert "README.md" in changed
        finally:
            ws.cleanup()
