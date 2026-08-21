"""Offline regression tests for AOS-3 Controlled Single-Worker Execution Engine."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from aos.controlled_execution import ControlledExecutionEngine
from aos.git_workspace import GitWorkspace, enforce_aos_branch_namespace
from aos.source_adapter import ProjectSourceAdapter
from aos.workers.base import WorkerAdapter, WorkerExecutionResult


class MockSourceAdapter(ProjectSourceAdapter):
    def __init__(
        self,
        repo: str,
        ref: str,
        control_sha: str = "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
        second_control_sha: Optional[str] = None,
        exec_base_sha: str = "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
        has_exec_base: bool = True,
        has_ambiguity: bool = False,
    ):
        super().__init__(repo, ref)
        self.control_sha = control_sha
        self.second_control_sha = second_control_sha or control_sha
        self.resolve_call_count = 0
        self.exec_base_sha = exec_base_sha
        self.has_exec_base = has_exec_base
        self.has_ambiguity = has_ambiguity

    def resolve_ref_to_sha(self) -> str:
        self.resolve_call_count += 1
        if self.resolve_call_count == 1:
            return self.control_sha
        return self.second_control_sha

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
    def __init__(self, repo, base_sha, task_id, branch_name=None, changed_files=None, setup_fails=False):
        super().__init__(repo, base_sha, task_id, branch_name)
        self.mock_changed_files = changed_files if changed_files is not None else ["src/app.py"]
        self.setup_fails = setup_fails
        self.cleaned_up = False

    def setup(self) -> str:
        if self.setup_fails:
            raise RuntimeError("Mock git workspace setup failed")
        self.workspace_dir = "/tmp/mock_workspace"
        self.initial_head_sha = self.base_sha
        return self.workspace_dir

    def get_current_head(self) -> str:
        return "1111222233334444555566667777888899990000"

    def get_status(self) -> List[str]:
        return [f" M {f}" for f in self.mock_changed_files]

    def get_changed_files(self, from_sha=None) -> List[str]:
        return self.mock_changed_files

    def cleanup(self) -> None:
        self.cleaned_up = True


class MockWorkerAdapter(WorkerAdapter):
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
            worker_identity="mock-worker",
            workspace_path=workspace_path,
            exit_code=self.exit_code,
            timed_out=self.timed_out,
            stdout_summary="Mock stdout",
            stderr_summary="Mock stderr" if self.exit_code != 0 else "",
            mutation_attempted=True,
        )


def make_generic_descriptor(project_id="generic_project"):
    return {
        "schema_version": "0.1.0",
        "project_id": project_id,
        "repository": "GenericOrg/GenericRepo",
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
        },
        "retry_policy": {
            "max_retries": max_retries,
        },
    }


class TestControlledExecutionEngine:
    def test_valid_generic_r1_aos3_task_reaches_verified_candidate(self):
        """1. Valid generic R1/AOS-3 task reaches VERIFIED_CANDIDATE with mocked worker."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockWorkerAdapter()
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"])

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute()
        assert res["disposition"] == "VERIFIED_CANDIDATE"
        assert res["scope_validation"]["is_valid"] is True
        assert mock_worker.executed is True

    def test_stale_control_revision_holds_zero_worker_calls(self):
        """2. Stale control revision (control ref moved during preparation) returns HOLD and 0 worker calls."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter(
            "GenericOrg/GenericRepo", "control/main",
            control_sha="4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            second_control_sha="9999999999999999999999999999999999999999",  # Moved!
        )
        mock_worker = MockWorkerAdapter()

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute()
        assert res["disposition"] == "HOLD"
        assert any("STALE_CONTROL_REVISION" in e for e in res["errors"])
        assert mock_worker.executed is False

    def test_missing_execution_base_commit_holds_zero_worker_calls(self):
        """3. Missing execution base in snapshot returns HOLD and 0 worker calls."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main", has_exec_base=False)
        mock_worker = MockWorkerAdapter()

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute()
        assert res["disposition"] == "HOLD"
        assert mock_worker.executed is False

    def test_execution_authority_hold_propagates_without_worker_call(self):
        """4. Snapshot ambiguity / authority validation failure returns HOLD with 0 worker calls."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main", has_ambiguity=True)
        mock_worker = MockWorkerAdapter()

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute()
        assert res["disposition"] == "HOLD"
        assert mock_worker.executed is False

    def test_wrong_project_id_holds(self):
        """5. Task project_id != descriptor project_id returns HOLD."""
        desc = make_generic_descriptor(project_id="proj_a")
        task = make_generic_task(project_id="proj_b")
        mock_worker = MockWorkerAdapter()

        engine = ControlledExecutionEngine(
            desc, task,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute()
        assert res["disposition"] == "HOLD"
        assert mock_worker.executed is False

    def test_wrong_gate_holds(self):
        """6. Gate != AOS-3 returns HOLD."""
        desc = make_generic_descriptor()
        task = make_generic_task(gate="AOS-2")
        mock_worker = MockWorkerAdapter()

        engine = ControlledExecutionEngine(
            desc, task,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute()
        assert res["disposition"] == "HOLD"
        assert mock_worker.executed is False

    def test_r0_r2_r3_r4_risk_classes_hold(self):
        """7. Risk classes other than R1 return HOLD."""
        desc = make_generic_descriptor()
        mock_worker = MockWorkerAdapter()

        for risk in ["R0", "R2", "R3", "R4"]:
            task = make_generic_task(risk_class=risk)
            engine = ControlledExecutionEngine(desc, task, worker_adapter_factory=lambda: mock_worker)
            res = engine.execute()
            assert res["disposition"] == "HOLD"
            assert mock_worker.executed is False

    def test_isolated_worktree_false_holds(self):
        """8. isolated_worktree=false returns HOLD."""
        desc = make_generic_descriptor()
        task = make_generic_task(isolated_worktree=False)
        mock_worker = MockWorkerAdapter()

        engine = ControlledExecutionEngine(
            desc, task,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute()
        assert res["disposition"] == "HOLD"
        assert mock_worker.executed is False

    def test_wrong_worker_adapter_holds(self):
        """9. Worker adapter != 'antigravity' returns HOLD."""
        desc = make_generic_descriptor()
        task = make_generic_task(adapter="other_adapter")
        mock_worker = MockWorkerAdapter()

        engine = ControlledExecutionEngine(
            desc, task,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute()
        assert res["disposition"] == "HOLD"
        assert mock_worker.executed is False

    def test_retry_max_retries_exceeding_ceiling_holds(self):
        """10. task.retry_policy.max_retries > 2 returns HOLD."""
        desc = make_generic_descriptor()
        task = make_generic_task(max_retries=3)  # Exceeds ceiling 2
        mock_worker = MockWorkerAdapter()

        engine = ControlledExecutionEngine(
            desc, task,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute()
        assert res["disposition"] == "HOLD"
        assert any("exceeds global ceiling" in e for e in res["errors"])
        assert mock_worker.executed is False

    def test_branch_policy_namespace_enforcement(self):
        """11. Worker branch is strictly forced under 'aos/' namespace."""
        assert enforce_aos_branch_namespace("TASK-301", "feature/custom") == "aos/custom"
        assert enforce_aos_branch_namespace("TASK-301", "aos/custom") == "aos/custom"
        assert enforce_aos_branch_namespace("TASK-301", "../../main") == "aos/main"
        assert enforce_aos_branch_namespace("TASK-301", None) == "aos/task-301"

    def test_allowed_path_change_verification_pass(self):
        """12. Path change in allowed scope passes verification -> VERIFIED_CANDIDATE."""
        desc = make_generic_descriptor()
        task = make_generic_task(paths=["src/"])
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockWorkerAdapter()
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"], changed_files=["src/utils.py"])

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute()
        assert res["disposition"] == "VERIFIED_CANDIDATE"
        assert res["mutation_performed"] is True
        assert res["scope_validation"]["is_valid"] is True

    def test_forbidden_path_change_verification_failed(self):
        """13. Path change in forbidden scope fails verification -> VERIFICATION_FAILED."""
        desc = make_generic_descriptor()
        task = make_generic_task(paths=["src/"], forbidden_paths=["src/secret.py"])
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockWorkerAdapter()
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"], changed_files=["src/secret.py"])

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute()
        assert res["disposition"] == "VERIFICATION_FAILED"
        assert res["scope_validation"]["is_valid"] is False

    def test_out_of_scope_path_change_verification_failed(self):
        """14. Path change outside allowed scope paths fails verification -> VERIFICATION_FAILED."""
        desc = make_generic_descriptor()
        task = make_generic_task(paths=["src/"])
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockWorkerAdapter()
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"], changed_files=["docs/README.md"])

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute()
        assert res["disposition"] == "VERIFICATION_FAILED"
        assert res["scope_validation"]["is_valid"] is False

    def test_no_changed_files_deterministic_result(self):
        """15. Worker run producing 0 changed files returns VERIFIED_CANDIDATE with mutation_performed=False."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockWorkerAdapter()
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"], changed_files=[])

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute()
        assert res["disposition"] == "VERIFIED_CANDIDATE"
        assert res["mutation_performed"] is False

    def test_worker_nonzero_exit_worker_failed(self):
        """16. Worker exiting with non-zero code returns WORKER_FAILED."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockWorkerAdapter(exit_code=1)
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"])

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute()
        assert res["disposition"] == "WORKER_FAILED"
        assert res["worker_exit_code"] == 1

    def test_worker_timeout_worker_failed(self):
        """17. Worker timing out returns WORKER_FAILED."""
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_worker = MockWorkerAdapter(timed_out=True)
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"])

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: mock_worker,
        )

        res = engine.execute()
        assert res["disposition"] == "WORKER_FAILED"
        assert res["worker_timed_out"] is True

    def test_no_lari_specific_identifiers_in_core(self):
        """18. Core engine files contain no hardcoded LARI identifiers."""
        for path_name in ["controlled_execution.py", "git_workspace.py", "scope_guard.py"]:
            source_code = (Path(__file__).parent.parent / "src" / "aos" / path_name).read_text(encoding="utf-8")
            assert "lari" not in source_code.lower()
            assert "clinic" not in source_code.lower()
            assert "supabase" not in source_code.lower()
