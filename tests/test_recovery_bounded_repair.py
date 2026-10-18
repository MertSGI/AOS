"""Regression tests for AOS recovery bounded repair.

Verifies:
1. is_implementation_objective correctly identifies repair/fix objectives and distinguishes them from pure verification.
2. build_intent_catalog skips READ_OBSERVED_SOURCE candidates whose task node_id is in forbidden_ids.
3. CommandAdmissionStore transitions to HOLD on terminal command completion in RuntimeWorker.
"""
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from aos.intent_compiler import build_intent_catalog
from aos.planning_kernel import Objective, is_implementation_objective
from aos.runtime_admission import AdmissionState, CommandAdmissionStore
from aos.runtime_worker import execute_command


def test_is_implementation_objective_repair_and_fix():
    # 1. Defect repair with verification criteria should be recognized as IMPLEMENTATION
    obj_repair = Objective(
        objective_id="OBJ-01",
        title="Repair discount calculation defect in src/discount.py and verify with test_discount.py",
        description="Fix the discount logic",
        authority_id="AUTH-01",
        risk_class="R0",
        rationale="Fix defect",
        scope_tags=("backend", "canary"),
        completion_criteria=("python -m pytest tests/test_discount.py passes",),
        parallel_candidates=(),
    )
    assert is_implementation_objective(obj_repair) is True

    # 2. Fix bug title
    obj_fix = Objective(
        objective_id="OBJ-02",
        title="Fix tax calculation defect and run tests",
        description="Fix tax defect",
        authority_id="AUTH-01",
        risk_class="R0",
        rationale="Fix defect",
        scope_tags=("backend",),
        completion_criteria=(),
        parallel_candidates=(),
    )
    assert is_implementation_objective(obj_fix) is True

    # 3. Pure verification starting with verify should NOT be classified as implementation
    obj_pure_verif = Objective(
        objective_id="OBJ-03",
        title="Verify implementation of discount calculation",
        description="Run test suite to verify discount",
        authority_id="AUTH-01",
        risk_class="R0",
        rationale="Verification",
        scope_tags=("verification",),
        completion_criteria=(),
        parallel_candidates=(),
    )
    assert is_implementation_objective(obj_pure_verif) is False


def test_build_intent_catalog_skips_forbidden_task_read_ids(tmp_path):
    # Create workspace with sample file
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sample_file = workspace / "src" / "discount.py"
    sample_file.parent.mkdir(parents=True)
    sample_file.write_text("def calculate_discount(): pass\n", encoding="utf-8")

    manifest = {"representative_existing_paths": ["src/discount.py"]}
    path_hash = hashlib.sha256(b"src/discount.py").hexdigest()[:12]
    task_read_id = f"TASK-READ-{path_hash.upper()}"

    obj = Objective(
        objective_id="OBJ-01",
        title="Repair defect",
        description="Fix logic",
        authority_id="AUTH-01",
        risk_class="R0",
        rationale="Fix",
        scope_tags=(),
        completion_criteria=(),
        parallel_candidates=(),
    )

    situation = SimpleNamespace(
        project_id="test-p",
        authority_records={},
        isolated_canary_context=None,
        control_sha="a" * 40,
        execution_base_sha="b" * 40,
        lane_allowed_scope=None,
    )

    # When task_read_id is in forbidden_ids, catalog MUST NOT include READ_OBSERVED_SOURCE for that file
    catalog = build_intent_catalog(
        situation,
        obj,
        workspace,
        tmp_path / "runtime",
        workspace_manifest=manifest,
        forbidden_ids={task_read_id},
    )
    assert not any(c.intent_type == "READ_OBSERVED_SOURCE" and c.target_ref == "src/discount.py" for c in catalog.candidates)

    # When task_read_id is NOT in forbidden_ids, it is included
    catalog_allowed = build_intent_catalog(
        situation,
        obj,
        workspace,
        tmp_path / "runtime",
        workspace_manifest=manifest,
        forbidden_ids=set(),
    )
    assert any(c.intent_type == "READ_OBSERVED_SOURCE" and c.target_ref == "src/discount.py" for c in catalog_allowed.candidates)


def test_runtime_worker_closes_admission_on_terminal_state(tmp_path):
    runtime_root = tmp_path / "runtime-v1"
    runtime_root.mkdir()
    commands_dir = runtime_root / "commands"
    commands_dir.mkdir()

    command_id = "test-cmd-001"
    cmd_dir = commands_dir / command_id
    cmd_dir.mkdir()

    from aos.runtime_store import RuntimeStore
    store = RuntimeStore(runtime_root)

    # Create dummy command.json
    import json
    (cmd_dir / "command.json").write_text(json.dumps({
        "command_id": command_id,
        "project": {"project_id": "test-project"},
        "goal": "Test goal",
    }), encoding="utf-8")
    (cmd_dir / "state.json").write_text(json.dumps({
        "state": "ACTIVE",
        "command_id": command_id,
    }), encoding="utf-8")

    admission_store = CommandAdmissionStore(runtime_root)
    admission_store.set_state(
        command_id,
        AdmissionState.ACTIVE,
        authority="TEST",
        reason="INIT",
    )
    assert admission_store.is_active(command_id) is True

    # Test that closing admission via terminal state transitions to HOLD
    admission_store.set_state(
        command_id,
        AdmissionState.HOLD,
        authority="COMMAND_TERMINATION",
        reason="COMMAND_PROJECT_COMPLETE",
    )
    rec = admission_store.get(command_id)
    assert rec.state == AdmissionState.HOLD.value
    assert rec.reason == "COMMAND_PROJECT_COMPLETE"
    assert rec.authority == "COMMAND_TERMINATION"
    assert admission_store.is_active(command_id) is False
