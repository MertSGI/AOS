"""Focused regression coverage for AOS autonomous continuation qualification.

Covers the 12 mandatory fail-closed conditions from Section 6:
1. Superseded or absent canonical authority
2. Invalid source generation
3. Completed task presented under a different task ID
4. Second task referencing the first task's unauthorized file
5. Missing pre-authorized test
6. Model returning an unexpected path
7. Source hash mismatch
8. Failed functional test restoring the previous file
9. Missing or corrupted checkpoint
10. Restart with unchanged completion history
11. Free-provider unavailability
12. Unverified Git commit or receipt
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict
import pytest

from jsonschema import Draft202012Validator, FormatChecker

from aos.intent_compiler import (
    PLAN_INTENT_SCHEMA,
    SourceGenerationMismatchError,
    UnauthorizedWriteScopeError,
    UnknownTargetRefError,
    UnapprovedWorkPackageError,
    build_intent_catalog,
    compile_intent_to_plan,
)
from aos.planning_kernel import (
    Objective,
    ProjectSituation,
    PLAN_SCHEMA,
    reconstruct_batch_history,
    _task_signature,
)
from extensions.autonomy_fabric.execution_backend import (
    ExecutionAvailabilityState,
    ExecutionCapability,
    ExecutionHealth,
    ExecutionRequest,
)
from extensions.autonomy_fabric.model_native_backend import (
    ModelNativeImplementationBackend,
    _resolve_zero_cost_api_key,
)
from extensions.autonomy_fabric.persistent_coordinator import (
    CheckpointCorruptionError,
    PersistentCoordinator,
)
from extensions.autonomy_fabric.resource_orchestrator import ResourceOrchestrator
from extensions.autonomy_fabric.run_registry import AgentRunRegistry
from extensions.autonomy_fabric.task_dag import TaskDAG


def _init_git_workspace(workspace: Path) -> str:
    subprocess.run(["git", "init"], cwd=str(workspace), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "AOS Continuation Worker"], cwd=str(workspace), check=True)
    subprocess.run(["git", "config", "user.email", "continuation-worker@aos.invalid"], cwd=str(workspace), check=True)
    subprocess.run(["git", "add", "."], cwd=str(workspace), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit", "--allow-empty"], cwd=str(workspace), check=True)
    head_proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(workspace), capture_output=True, text=True, check=True)
    return head_proc.stdout.strip().lower()


@pytest.fixture
def base_canary_situation() -> ProjectSituation:
    return ProjectSituation(
        schema_version="1.0.0",
        project_id="test-continuation-proj",
        repository="MertSGI/AOS",
        control_ref="refs/heads/main",
        control_sha="a" * 40,
        repository_head="a" * 40,
        execution_base_sha="a" * 40,
        current_status="ACTIVE",
        current_milestone="M1",
        canonical_next_action="IMPLEMENT",
        canonical_hashes={},
        canonical_excerpt="excerpt",
        working_tree_state="CLEAN",
        ci_state=[],
        accepted_gates=[],
        blocked_gates=[],
        authority_records={},
        goal="Test Continuation",
        constraints=(),
        red_lines=(),
        completion_criteria=(),
        ambiguity_reasons=(),
        captured_at="2026-10-15T00:00:00Z",
        lane_allowed_scope=("src/",),
    )


@pytest.fixture
def base_canary_objective() -> Objective:
    return Objective(
        objective_id="OBJ-CANARY-01",
        title="Repair module",
        description="Repair module defect",
        authority_id="CANARY-AUTH-01",
        risk_class="R0",
        rationale="Continuation qualification",
        scope_tags=("src",),
        completion_criteria=("test passes",),
        parallel_candidates=(),
    )


class TestAutonomousContinuationRegressions:
    """12 mandatory fail-closed tests for autonomous continuation qualification."""

    # 1. Superseded or absent canonical authority
    def test_superseded_or_absent_canonical_authority(
        self, tmp_path: Path, base_canary_situation: ProjectSituation, base_canary_objective: Objective
    ):
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        wp_file = runtime_dir / "authorized-work-packages.json"
        wp_file.write_text(
            json.dumps({
                "approved": True,
                "authority_id": "CANARY-AUTH-REVOKED",
                "source_sha": base_canary_situation.control_sha,
                "workspace_source_generation": "gen-1",
                "work_packages": [
                    {
                        "work_package_id": "WP-REVOKED",
                        "target_files": ["src/discount.py"],
                        "instruction": "Fix discount",
                        "approved": True,
                    }
                ],
            }),
            encoding="utf-8",
        )
        canary_ctx = {
            "isolated_workspace": True,
            "canary_authority_id": "CANARY-AUTH-VALID",
            "canary_run_id": "RUN-001",
        }
        catalog = build_intent_catalog(
            situation=base_canary_situation,
            objective=base_canary_objective,
            workspace=tmp_path,
            runtime_dir=runtime_dir,
            workspace_source_generation="gen-1",
            isolated_canary_context=canary_ctx,
        )
        assert not any(c.target_ref == "WP-REVOKED" for c in catalog.candidates)

    # 2. Invalid source generation
    def test_invalid_source_generation_rejected(
        self, tmp_path: Path, base_canary_situation: ProjectSituation, base_canary_objective: Objective
    ):
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        wp_file = runtime_dir / "authorized-work-packages.json"
        wp_file.write_text(
            json.dumps({
                "approved": True,
                "authority_id": "CANARY-AUTH-01",
                "source_sha": base_canary_situation.control_sha,
                "workspace_source_generation": "gen-STALE-OLD",
                "work_packages": [
                    {
                        "work_package_id": "WP-STALE",
                        "target_files": ["src/discount.py"],
                        "instruction": "Fix discount",
                        "approved": True,
                    }
                ],
            }),
            encoding="utf-8",
        )
        canary_ctx = {
            "isolated_workspace": True,
            "canary_authority_id": "CANARY-AUTH-01",
            "canary_run_id": "RUN-001",
        }
        catalog = build_intent_catalog(
            situation=base_canary_situation,
            objective=base_canary_objective,
            workspace=tmp_path,
            runtime_dir=runtime_dir,
            workspace_source_generation="gen-CURRENT-FRESH",
            isolated_canary_context=canary_ctx,
        )
        assert not any(c.target_ref == "WP-STALE" for c in catalog.candidates)

    # 3. Completed task presented under a different task ID
    def test_completed_task_different_id_deduplicated_by_signature(
        self, tmp_path: Path, base_canary_situation: ProjectSituation, base_canary_objective: Objective
    ):
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        wp_file = runtime_dir / "authorized-work-packages.json"
        wp_file.write_text(
            json.dumps({
                "approved": True,
                "authority_id": "CANARY-AUTH-01",
                "source_sha": base_canary_situation.control_sha,
                "workspace_source_generation": "gen-1",
                "work_packages": [
                    {
                        "work_package_id": "WP-NEW-SPOOFED-ID",
                        "target_files": ["src/discount.py"],
                        "expected_artifacts": ["src/discount.py"],
                        "instruction": "Fix discount calculation exactly",
                        "approved": True,
                    }
                ],
            }),
            encoding="utf-8",
        )
        completed_sig = "AGENTIC:" + json.dumps({
            "expected_artifacts": ["src/discount.py"],
            "prompt": "Fix discount calculation exactly",
            "write_scope": ["src/discount.py"],
        }, sort_keys=True, separators=(",", ":"))

        canary_ctx = {
            "isolated_workspace": True,
            "canary_authority_id": "CANARY-AUTH-01",
            "canary_run_id": "RUN-001",
        }
        catalog = build_intent_catalog(
            situation=base_canary_situation,
            objective=base_canary_objective,
            workspace=tmp_path,
            runtime_dir=runtime_dir,
            forbidden_signatures={completed_sig},
            workspace_source_generation="gen-1",
            isolated_canary_context=canary_ctx,
        )
        assert not any(c.target_ref == "WP-NEW-SPOOFED-ID" for c in catalog.candidates)

    # 4. Second task referencing the first task's unauthorized file
    def test_second_task_referencing_first_task_unauthorized_file(
        self, tmp_path: Path, base_canary_situation: ProjectSituation, base_canary_objective: Objective
    ):
        situation_with_restricted_scope = ProjectSituation(
            **{**base_canary_situation.__dict__, "lane_allowed_scope": ("src/tax/",)}
        )
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        wp_file = runtime_dir / "authorized-work-packages.json"
        wp_file.write_text(
            json.dumps({
                "approved": True,
                "authority_id": "CANARY-AUTH-01",
                "source_sha": base_canary_situation.control_sha,
                "workspace_source_generation": "gen-1",
                "work_packages": [
                    {
                        "work_package_id": "WP-TAX-OUT-OF-SCOPE",
                        "target_files": ["src/discount.py"],
                        "instruction": "Illegally edit discount file while on tax lane",
                        "approved": True,
                    }
                ],
            }),
            encoding="utf-8",
        )
        canary_ctx = {
            "isolated_workspace": True,
            "canary_authority_id": "CANARY-AUTH-01",
            "canary_run_id": "RUN-001",
        }
        catalog = build_intent_catalog(
            situation=situation_with_restricted_scope,
            objective=base_canary_objective,
            workspace=tmp_path,
            runtime_dir=runtime_dir,
            workspace_source_generation="gen-1",
            isolated_canary_context=canary_ctx,
        )
        assert not any(c.target_ref == "WP-TAX-OUT-OF-SCOPE" for c in catalog.candidates)

    # 5. Missing pre-authorized test
    def test_missing_pre_authorized_test_rejected(self, tmp_path: Path):
        _init_git_workspace(tmp_path)
        src_file = tmp_path / "src" / "test_file.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("x = 1\n", encoding="utf-8")

        backend = ModelNativeImplementationBackend()
        req = ExecutionRequest(
            task_id="task-no-tests",
            project_id="test-proj",
            workspace=str(tmp_path),
            operation_class="AGENTIC",
            required_capabilities=[ExecutionCapability.FILE_WRITE, ExecutionCapability.TEST_EXECUTION],
            authority_id="AUTH-01",
            write_scope=["src/test_file.py"],
            payload={"tests": []},
        )
        res = backend.execute(req)
        assert res.status == "FAILED"
        assert any("NO_FUNCTIONAL_TESTS" in err for err in res.sanitized_errors)

    # 6. Model returning an unexpected path
    def test_model_returning_unexpected_path_rejected(self, tmp_path: Path):
        base_sha = _init_git_workspace(tmp_path)
        src_file = tmp_path / "src" / "discount.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("def discount(): pass\n", encoding="utf-8")
        test_file = tmp_path / "test_d.py"
        test_file.write_text("assert True\n", encoding="utf-8")

        backend = ModelNativeImplementationBackend(
            model_caller=lambda p, s, c, i: {
                "relative_path": "src/unexpected_different_file.py",
                "expected_original_sha256": s,
                "new_content": "hacked\n",
            }
        )
        req = ExecutionRequest(
            task_id="task-bad-path",
            project_id="test-proj",
            workspace=str(tmp_path),
            operation_class="AGENTIC",
            required_capabilities=[ExecutionCapability.FILE_WRITE, ExecutionCapability.TEST_EXECUTION],
            authority_id="AUTH-01",
            write_scope=["src/discount.py"],
            payload={"tests": ["python test_d.py"]},
        )
        res = backend.execute(req)
        assert res.status == "FAILED"
        assert any("UNAUTHORIZED_PATH_MUTATION" in err for err in res.sanitized_errors)

    # 7. Source hash mismatch
    def test_source_hash_mismatch_rejected(self, tmp_path: Path):
        base_sha = _init_git_workspace(tmp_path)
        src_file = tmp_path / "src" / "discount.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("def discount(): pass\n", encoding="utf-8")
        test_file = tmp_path / "test_d.py"
        test_file.write_text("assert True\n", encoding="utf-8")

        backend = ModelNativeImplementationBackend(
            model_caller=lambda p, s, c, i: {
                "relative_path": p,
                "expected_original_sha256": "0" * 64,
                "new_content": "fixed\n",
            }
        )
        req = ExecutionRequest(
            task_id="task-bad-sha",
            project_id="test-proj",
            workspace=str(tmp_path),
            operation_class="AGENTIC",
            required_capabilities=[ExecutionCapability.FILE_WRITE, ExecutionCapability.TEST_EXECUTION],
            authority_id="AUTH-01",
            write_scope=["src/discount.py"],
            payload={"tests": ["python test_d.py"]},
        )
        res = backend.execute(req)
        assert res.status == "FAILED"
        assert any("PRECONDITION_HASH_MISMATCH" in err for err in res.sanitized_errors)

    # 8. Failed functional test restoring the previous file
    def test_failed_functional_test_restores_file(self, tmp_path: Path):
        base_sha = _init_git_workspace(tmp_path)
        src_file = tmp_path / "src" / "discount.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        original_code = "def discount(): return 'original_code'\n"
        src_file.write_text(original_code, encoding="utf-8")
        test_file = tmp_path / "test_d.py"
        test_file.write_text("from src.discount import discount\nassert discount() == 'required_code'\n", encoding="utf-8")

        # Initial commit with defect
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-m", "defect"], cwd=str(tmp_path), check=True)

        backend = ModelNativeImplementationBackend(
            model_caller=lambda p, s, c, i: {
                "relative_path": p,
                "expected_original_sha256": s,
                "new_content": "def discount(): return 'still_wrong_code'\n",
            }
        )
        req = ExecutionRequest(
            task_id="task-failing-test",
            project_id="test-proj",
            workspace=str(tmp_path),
            operation_class="AGENTIC",
            required_capabilities=[ExecutionCapability.FILE_WRITE, ExecutionCapability.TEST_EXECUTION],
            authority_id="AUTH-01",
            write_scope=["src/discount.py"],
            payload={"tests": ["python test_d.py"]},
        )
        res = backend.execute(req)
        assert res.status == "FAILED"
        assert any("FUNCTIONAL_TEST_FAILED" in err for err in res.sanitized_errors)
        # Verify file content was restored
        assert src_file.read_text(encoding="utf-8") == original_code

    # 9. Missing or corrupted checkpoint
    def test_corrupted_checkpoint_fails_closed(self, tmp_path: Path):
        registry = AgentRunRegistry()
        dag = TaskDAG("test-proj", registry)
        orch = ResourceOrchestrator()
        from extensions.autonomy_fabric.execution_router import ExecutionRouter
        router = ExecutionRouter([], orchestrator=orch)

        bad_checkpoint = tmp_path / "coordinator-checkpoint.json"
        bad_checkpoint.write_text("{corrupt: truncated", encoding="utf-8")

        with pytest.raises(CheckpointCorruptionError):
            PersistentCoordinator(
                project_id="test-proj",
                workspace_path=str(tmp_path),
                dag=dag,
                router=router,
                registry=registry,
                checkpoint_file=str(bad_checkpoint),
            )

    # 10. Restart with unchanged completion history
    def test_restart_with_unchanged_completion_history_prevents_duplicate(self, tmp_path: Path):
        batches_dir = tmp_path / "batches" / "batch-0000"
        batches_dir.mkdir(parents=True, exist_ok=True)
        receipt = {
            "progress": 100.0,
            "completed_task_ids": ["TASK-AGENTIC-WP-WP-REPAIR-DISCOUNT"],
            "failed_task_ids": [],
            "verified_tasks": [],
        }
        (batches_dir / "host-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        plan = {
            "tasks": [
                {
                    "node_id": "TASK-AGENTIC-WP-WP-REPAIR-DISCOUNT",
                    "run_type": "AGENTIC",
                    "write_scope": ["src/discount.py"],
                    "expected_artifacts": ["src/discount.py"],
                    "payload": {
                        "prompt": "Fix calculate_discount",
                    },
                }
            ]
        }
        (batches_dir / "generated-run-plan.json").write_text(json.dumps(plan), encoding="utf-8")

        history = reconstruct_batch_history(tmp_path, workspace=tmp_path)
        assert "TASK-AGENTIC-WP-WP-REPAIR-DISCOUNT" in history.completed_task_ids
        expected_sig = "AGENTIC:" + json.dumps({
            "expected_artifacts": ["src/discount.py"],
            "prompt": "Fix calculate_discount",
            "write_scope": ["src/discount.py"],
        }, sort_keys=True, separators=(",", ":"))
        assert expected_sig in history.completed_task_signatures

    # 11. Free-provider unavailability
    def test_free_provider_unavailability_routing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        fake_empty_home = tmp_path / "empty_home"
        fake_empty_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(Path, "home", lambda: fake_empty_home)

        backend = ModelNativeImplementationBackend()
        assert backend.get_health() == ExecutionHealth.UNAVAILABLE
        avail = backend.get_availability()
        assert avail.state == ExecutionAvailabilityState.AUTH_UNAVAILABLE

        req = ExecutionRequest(
            task_id="task-1",
            project_id="test-proj",
            workspace=str(tmp_path),
            operation_class="AGENTIC",
            required_capabilities=[ExecutionCapability.FILE_WRITE, ExecutionCapability.TEST_EXECUTION],
            authority_id="AUTH-01",
        )
        orch = ResourceOrchestrator()
        ranks = orch.rank([backend], req)
        assert ranks[0].eligible is False
        assert any("AUTH_UNAVAILABLE" in r or "HEALTH_UNAVAILABLE" in r for r in ranks[0].reasons)

    # 12. Unverified Git commit or receipt: A failed objective must not be marked complete
    def test_unverified_git_commit_fails_closed_and_not_marked_complete(self, tmp_path: Path):
        base_sha = _init_git_workspace(tmp_path)
        src_file = tmp_path / "src" / "discount.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("def discount(): pass\n", encoding="utf-8")
        test_file = tmp_path / "test_d.py"
        test_file.write_text("assert True\n", encoding="utf-8")

        # Initial commit
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-m", "initial file"], cwd=str(tmp_path), check=True)

        # Model returns no actual code change -> Git commit produces 0 changes -> result SHA == base SHA
        backend = ModelNativeImplementationBackend(
            model_caller=lambda p, s, c, i: {
                "relative_path": p,
                "expected_original_sha256": s,
                "new_content": c,  # Identical content
            }
        )
        req = ExecutionRequest(
            task_id="task-no-diff",
            project_id="test-proj",
            workspace=str(tmp_path),
            operation_class="AGENTIC",
            required_capabilities=[ExecutionCapability.FILE_WRITE, ExecutionCapability.TEST_EXECUTION],
            authority_id="AUTH-01",
            write_scope=["src/discount.py"],
            payload={"tests": ["python test_d.py"]},
        )
        res = backend.execute(req)
        assert res.status == "FAILED"
        assert any("UNCHANGED_SOURCE" in err or "GIT_SHA_UNCHANGED" in err for err in res.sanitized_errors)
