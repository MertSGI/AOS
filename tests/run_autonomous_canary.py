"""Autonomous implementation canary runner.

Disposably creates a scratch test repository with:
- Exactly 1 source file: src/math_util.py
- Exactly 1 test file: test_math.py with a functional test that initially FAILS.
- Executes test before (observes FAIL).
- Authorizes work package with explicit isolated canary context.
- Compiles compact intent to plan.
- Routes and executes via ModelNativeImplementationBackend (calling NVIDIA Nemotron live).
- Executes test after (observes PASS).
- Captures before/after SHA, diff, test output, and durable receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from aos.intent_compiler import build_intent_catalog, compile_intent_to_plan
from aos.planning_kernel import Objective, ProjectSituation
from extensions.autonomy_fabric.execution_backend import (
    ExecutionCapability,
    ExecutionRequest,
)
from extensions.autonomy_fabric.model_native_backend import (
    ModelNativeImplementationBackend,
)
from extensions.autonomy_fabric.resource_orchestrator import ResourceOrchestrator


def run_autonomous_canary() -> dict:
    scratch_root = Path(r"C:\Users\mozcelikbas\AppData\Local\Temp\aos_autonomous_canary_disposable")
    if scratch_root.exists():
        shutil.rmtree(scratch_root, ignore_errors=True)
    scratch_root.mkdir(parents=True, exist_ok=True)

    # Initialize a clean disposable git repo
    subprocess.run(["git", "init"], cwd=str(scratch_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "canary@aos.invalid"], cwd=str(scratch_root), check=True)
    subprocess.run(["git", "config", "user.name", "AOS Canary Worker"], cwd=str(scratch_root), check=True)

    # 1. Create source file with defect
    src_dir = scratch_root / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    source_file = src_dir / "math_util.py"
    initial_content = "def calculate_discount(price: float, rate: float) -> float:\n    # Bug: returns addition instead of discount\n    return price + (price * rate)\n"
    source_file.write_text(initial_content, encoding="utf-8")
    initial_file_sha = hashlib.sha256(source_file.read_bytes()).hexdigest()

    # Create functional test that tests discount calculation (will FAIL on initial content)
    test_file = scratch_root / "test_discount.py"
    test_content = (
        "import sys\n"
        "from src.math_util import calculate_discount\n\n"
        "result = calculate_discount(100.0, 0.2)\n"
        "assert result == 80.0, f'Expected 80.0 but got {result}'\n"
        "print('CANARY_FUNCTIONAL_TEST_PASS')\n"
    )
    test_file.write_text(test_content, encoding="utf-8")

    # Commit initial state so workspace has valid git lineage
    subprocess.run(["git", "add", "."], cwd=str(scratch_root), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit with defective calculate_discount"], cwd=str(scratch_root), check=True, capture_output=True)
    initial_git_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(scratch_root), check=True, capture_output=True, text=True).stdout.strip()

    # 2. Execute functional test BEFORE repair and verify FAIL
    proc_before = subprocess.run(
        ["python", "test_discount.py"],
        cwd=str(scratch_root),
        capture_output=True,
        text=True,
    )
    test_before_exit = proc_before.returncode
    test_before_output = (proc_before.stderr or proc_before.stdout).strip()
    assert test_before_exit != 0, f"Expected initial test to FAIL, but it passed: {proc_before.stdout}"

    # 3. Setup AOS situation and runtime directories
    runtime_dir = scratch_root / ".aos-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    # Write authorized work package file in runtime_dir
    wp_data = {
        "approved": True,
        "authority_id": "CANARY-AUTH-MODEL-NATIVE-01",
        "source_sha": initial_git_sha,
        "workspace_source_generation": "canary-gen-1",
        "work_packages": [
            {
                "work_package_id": "WP-REPAIR-DISCOUNT",
                "target_files": ["src/math_util.py"],
                "expected_artifacts": ["src/math_util.py"],
                "instruction": "Fix calculate_discount in src/math_util.py so that it returns price - (price * rate).",
                "tests": ["python test_discount.py"],
                "risk_class": "R0",
                "approved": True,
            }
        ],
    }
    wp_file = runtime_dir / "authorized-work-packages.json"
    wp_file.write_text(json.dumps(wp_data, indent=2), encoding="utf-8")

    situation = ProjectSituation(
        schema_version="1.0.0",
        project_id="canary-math-proj",
        repository="MertSGI/AOS",
        control_ref="refs/heads/main",
        control_sha=initial_git_sha,
        repository_head=initial_git_sha,
        execution_base_sha=initial_git_sha,
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
        goal="Repair calculate_discount",
        constraints=(),
        red_lines=(),
        completion_criteria=(),
        ambiguity_reasons=(),
        captured_at="2026-10-15T00:00:00Z",
        lane_allowed_scope=("src/",),
    )

    objective = Objective(
        objective_id="OBJ-CANARY-01",
        title="Repair calculate_discount function",
        description="Fix calculation bug in src/math_util.py",
        authority_id="CANARY-AUTH-MODEL-NATIVE-01",
        risk_class="R0",
        rationale="Autonomous canary execution",
        scope_tags=("src",),
        completion_criteria=("test passes",),
        parallel_candidates=(),
    )

    canary_ctx = {
        "isolated_workspace": True,
        "canary_authority_id": "CANARY-AUTH-MODEL-NATIVE-01",
        "canary_run_id": "RUN-CANARY-MODEL-NATIVE-001",
    }

    # 4. AOS Intent Compilation
    catalog = build_intent_catalog(
        situation=situation,
        objective=objective,
        workspace=scratch_root,
        runtime_dir=runtime_dir,
        workspace_source_generation="canary-gen-1",
        isolated_canary_context=canary_ctx,
    )

    candidates = [c for c in catalog.candidates if c.intent_type == "IMPLEMENT_AUTHORIZED_WORK_PACKAGE"]
    assert len(candidates) == 1, f"Expected 1 candidate, got {catalog.candidates}"
    selected_candidate = candidates[0]
    assert selected_candidate.target_ref == "WP-REPAIR-DISCOUNT"

    intent = {
        "schema_version": "1.0.0",
        "objective_id": objective.objective_id,
        "intent_type": "IMPLEMENT_AUTHORIZED_WORK_PACKAGE",
        "target_ref": "WP-REPAIR-DISCOUNT",
    }
    plan = compile_intent_to_plan(
        intent=intent,
        catalog=catalog,
        situation=situation,
        objective=objective,
        runtime_dir=runtime_dir,
        workspace=scratch_root,
        workspace_source_generation="canary-gen-1",
    )
    task = plan["tasks"][0]

    # 5. Resource Orchestrator Selection
    backend = ModelNativeImplementationBackend()
    orch = ResourceOrchestrator()
    req = ExecutionRequest(
        task_id=task["node_id"],
        project_id="canary-math-proj",
        workspace=str(scratch_root),
        operation_class=task["run_type"],
        required_capabilities=[ExecutionCapability.FILE_WRITE, ExecutionCapability.TEST_EXECUTION],
        authority_id=task["authority_id"],
        write_scope=list(task["write_scope"]),
        expected_artifacts=list(task["expected_artifacts"]),
        payload={
            **task["payload"],
            "source_sha": initial_git_sha,
            "tests": list(task["tests"]),
            "runtime_dir": str(runtime_dir),
        },
    )

    ranks = orch.rank([backend], req)
    assert len(ranks) == 1 and ranks[0].eligible is True
    selected_backend_id = orch.select([backend], req)
    assert selected_backend_id == backend.backend_id

    # 6. Execute Model + Native worker (autonomous model generation + native application + test)
    exec_result = backend.execute(req)
    assert exec_result.status == "SUCCESS", f"Execution failed: {exec_result.sanitized_errors}"
    assert exec_result.exit_code == 0

    # 7. Verify Functional Test AFTER repair
    proc_after = subprocess.run(
        ["python", "test_discount.py"],
        cwd=str(scratch_root),
        capture_output=True,
        text=True,
    )
    test_after_exit = proc_after.returncode
    test_after_output = proc_after.stdout.strip()
    assert test_after_exit == 0, f"Expected test to PASS, but it failed: {proc_after.stderr}"
    assert "CANARY_FUNCTIONAL_TEST_PASS" in test_after_output

    # 8. Check source SHA changes
    repaired_bytes = source_file.read_bytes()
    repaired_file_sha = hashlib.sha256(repaired_bytes).hexdigest()
    assert initial_file_sha != repaired_file_sha

    diff = exec_result.evidence_payload.get("diff")
    receipt = exec_result.evidence_payload.get("receipt")

    return {
        "initial_git_sha": initial_git_sha,
        "initial_file_sha": initial_file_sha,
        "repaired_file_sha": repaired_file_sha,
        "diff": diff,
        "test_before_exit": test_before_exit,
        "test_before_output": test_before_output,
        "test_after_exit": test_after_exit,
        "test_after_output": test_after_output,
        "selected_backend": selected_backend_id,
        "receipt": receipt,
        "scratch_root": str(scratch_root),
    }


if __name__ == "__main__":
    result = run_autonomous_canary()
    print("=== AUTONOMOUS CANARY RESULT ===")
    print(json.dumps(result, indent=2))
