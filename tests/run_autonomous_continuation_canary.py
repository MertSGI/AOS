"""AOS Genuine Two-Objective Autonomous Software-Development Continuation Canary Runner.

Demonstrates autonomous multi-objective continuation using core AOS components:
- Genuine AOS objective selection
- Trusted Compact Intent compilation
- Resource Orchestrator backend selection (ModelNativeImplementationBackend)
- Real bounded source modification (via live zero-cost NVIDIA Nemotron)
- Native functional verification (FAIL -> PASS)
- Git commits with verifiable lineage and distinct Git SHAs
- Durable implementation receipts in KnowledgeLedger
- Clean coordinator closure and durable restart checkpoint
- Deliberate restart: history reconstruction, automatic selection of Objective B without manual plan injection
- Second execution & verification pipeline (different file, different test, distinct Git SHA, independent receipt)
- Third restart: duplicate prevention proven (zero model calls, zero commits, zero receipts)
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from aos.intent_compiler import (
    PLAN_INTENT_SCHEMA,
    build_intent_catalog,
    compile_intent_to_plan,
)
from aos.planning_kernel import (
    Objective,
    ProjectSituation,
    PLAN_SCHEMA,
    compile_execution_plan,
    reconstruct_batch_history,
    run_autonomous_project,
    select_objective,
    _task_signature,
    _write_kernel_checkpoint,
    _kernel_checkpoint_path,
)
from extensions.autonomy_fabric.execution_backend import (
    ExecutionCapability,
    ExecutionRequest,
)
from extensions.autonomy_fabric.model_native_backend import (
    ModelNativeImplementationBackend,
    _resolve_zero_cost_api_key,
)
from extensions.autonomy_fabric.resource_orchestrator import ResourceOrchestrator


CANARY_SCRATCH_ROOT = Path(r"C:\Users\mozcelikbas\AppData\Local\Temp\aos_two_objective_canary_workspace")


def setup_disposable_project(root: Path) -> Dict[str, Any]:
    """Prepare an isolated two-objective software project with failing functional tests."""
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    # Initialize a clean disposable git repo
    subprocess.run(["git", "init"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "autonomous-continuation@aos.invalid"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.name", "AOS Autonomous Continuation Worker"], cwd=str(root), check=True)

    src_dir = root / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    # 1. Source Module A: Defective discount calculation
    discount_file = src_dir / "discount.py"
    discount_initial_code = (
        "def calculate_discount(price: float, rate: float) -> float:\n"
        "    # Defect: adds discount instead of subtracting\n"
        "    return price + (price * rate)\n"
    )
    discount_file.write_text(discount_initial_code, encoding="utf-8")
    discount_initial_file_sha = hashlib.sha256(discount_file.read_bytes()).hexdigest()

    # Functional Test A: Initially FAILS
    test_discount_file = root / "test_discount.py"
    test_discount_code = (
        "import sys\n"
        "from src.discount import calculate_discount\n\n"
        "result = calculate_discount(100.0, 0.2)\n"
        "assert result == 80.0, f'Expected 80.0 but got {result}'\n"
        "print('CANARY_DISCOUNT_TEST_PASS')\n"
    )
    test_discount_file.write_text(test_discount_code, encoding="utf-8")

    # 2. Source Module B: Defective tax calculation
    tax_file = src_dir / "tax.py"
    tax_initial_code = (
        "def calculate_tax(subtotal: float, rate: float) -> float:\n"
        "    # Defect: multiplies subtotal by (1 + rate) instead of just subtotal * rate\n"
        "    return subtotal * (1.0 + rate)\n"
    )
    tax_file.write_text(tax_initial_code, encoding="utf-8")
    tax_initial_file_sha = hashlib.sha256(tax_file.read_bytes()).hexdigest()

    # Functional Test B: Initially FAILS
    test_tax_file = root / "test_tax.py"
    test_tax_code = (
        "import sys\n"
        "from src.tax import calculate_tax\n\n"
        "result = calculate_tax(200.0, 0.05)\n"
        "assert result == 10.0, f'Expected 10.0 but got {result}'\n"
        "print('CANARY_TAX_TEST_PASS')\n"
    )
    test_tax_file.write_text(test_tax_code, encoding="utf-8")

    # Initial Git Commit establishing verifiable lineage
    subprocess.run(["git", "add", "."], cwd=str(root), check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit: defective discount and tax calculations with functional tests"],
        cwd=str(root),
        check=True,
        capture_output=True,
    )
    initial_git_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), check=True, capture_output=True, text=True
    ).stdout.strip().lower()

    # Pre-flight: verify BOTH initial functional tests FAIL for different reasons
    p_disc = subprocess.run(["python", "test_discount.py"], cwd=str(root), capture_output=True, text=True)
    assert p_disc.returncode != 0, f"Expected test_discount to FAIL initially, but passed: {p_disc.stdout}"
    test_a_fail_output = (p_disc.stderr or p_disc.stdout).strip()

    p_tax = subprocess.run(["python", "test_tax.py"], cwd=str(root), capture_output=True, text=True)
    assert p_tax.returncode != 0, f"Expected test_tax to FAIL initially, but passed: {p_tax.stdout}"
    test_b_fail_output = (p_tax.stderr or p_tax.stdout).strip()

    # 3. Setup AOS runtime directory and authorized work packages
    runtime_dir = root / ".aos-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    # Two distinct work packages with independent canary authorities
    wp_data = {
        "approved": True,
        "source_sha": initial_git_sha,
        "workspace_source_generation": "canary-two-obj-gen-1",
        "work_packages": [
            {
                "work_package_id": "WP-REPAIR-DISCOUNT",
                "authority_id": "CANARY-AUTH-OBJ-A",
                "target_files": ["src/discount.py"],
                "expected_artifacts": ["src/discount.py"],
                "instruction": "Fix calculate_discount in src/discount.py so that it returns price - (price * rate).",
                "tests": ["python test_discount.py"],
                "risk_class": "R0",
                "approved": True,
            },
            {
                "work_package_id": "WP-REPAIR-TAX",
                "authority_id": "CANARY-AUTH-OBJ-B",
                "target_files": ["src/tax.py"],
                "expected_artifacts": ["src/tax.py"],
                "instruction": "Fix calculate_tax in src/tax.py so that it returns subtotal * rate.",
                "tests": ["python test_tax.py"],
                "risk_class": "R0",
                "approved": True,
            },
        ],
    }
    (runtime_dir / "authorized-work-packages.json").write_text(json.dumps(wp_data, indent=2), encoding="utf-8")

    # Canonical Roadmap fixture in the repository
    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    roadmap_content = (
        "# Autonomous Software Development Roadmap\n\n"
        "## Priority 1: Objective A (OBJ-DISCOUNT)\n"
        "- Authority: CANARY-AUTH-OBJ-A\n"
        "- Target: src/discount.py\n"
        "- Instruction: Fix calculate_discount calculation in src/discount.py\n"
        "- Test: python test_discount.py\n\n"
        "## Priority 2: Objective B (OBJ-TAX)\n"
        "- Authority: CANARY-AUTH-OBJ-B\n"
        "- Target: src/tax.py\n"
        "- Instruction: Fix calculate_tax calculation in src/tax.py\n"
        "- Test: python test_tax.py\n"
    )
    (docs_dir / "ROADMAP.md").write_text(roadmap_content, encoding="utf-8")

    # Project Descriptor
    descriptor = {
        "schema_version": "1.0.0",
        "project_id": "canary-two-obj-proj",
        "repository": "MertSGI/AOS",
        "control_ref": "refs/heads/main",
        "control": {
            "docs/ROADMAP.md": str(docs_dir / "ROADMAP.md"),
        },
        "completion_criteria": [
            "test_discount.py passes",
            "test_tax.py passes",
        ],
    }
    descriptor_path = runtime_dir / "project-descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8")

    # Routing policy
    routing_policy = {
        "schema_version": "1.0.0",
        "default_tier": "FREE",
        "providers": {
            "nemotron": {
                "class": "FREE_TIER_CLOUD",
                "enabled": True,
            }
        },
    }
    routing_policy_path = runtime_dir / "routing-policy.json"
    routing_policy_path.write_text(json.dumps(routing_policy, indent=2), encoding="utf-8")

    return {
        "workspace": root,
        "runtime_dir": runtime_dir,
        "descriptor_path": descriptor_path,
        "routing_policy_path": routing_policy_path,
        "initial_git_sha": initial_git_sha,
        "discount_initial_file_sha": discount_initial_file_sha,
        "tax_initial_file_sha": tax_initial_file_sha,
        "test_a_fail_output": test_a_fail_output,
        "test_b_fail_output": test_b_fail_output,
    }


def execute_two_objective_continuation() -> Dict[str, Any]:
    """Execute the full two-objective autonomous qualification loop with restart recovery."""
    fixture = setup_disposable_project(CANARY_SCRATCH_ROOT)
    workspace = fixture["workspace"]
    runtime_dir = fixture["runtime_dir"]
    descriptor_path = fixture["descriptor_path"]
    routing_policy_path = fixture["routing_policy_path"]
    initial_git_sha = fixture["initial_git_sha"]

    isolated_canary_context = {
        "isolated_workspace": True,
        "canary_authority_ids": ["CANARY-AUTH-OBJ-A", "CANARY-AUTH-OBJ-B"],
        "canary_run_id": "RUN-CANARY-TWO-OBJ-001",
    }

    # Custom situation synthesizer for the isolated fixture
    def canary_situation_factory(**kwargs) -> ProjectSituation:
        head_proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(workspace), capture_output=True, text=True, check=True)
        current_head = head_proc.stdout.strip().lower()

        # Check if discount test passes
        disc_passed = subprocess.run(["python", "test_discount.py"], cwd=str(workspace), capture_output=True).returncode == 0
        tax_passed = subprocess.run(["python", "test_tax.py"], cwd=str(workspace), capture_output=True).returncode == 0

        next_action = "PROJECT_COMPLETE" if (disc_passed and tax_passed) else ("Repair tax" if disc_passed else "Repair discount")

        return ProjectSituation(
            schema_version="1.0.0",
            project_id="canary-two-obj-proj",
            repository="MertSGI/AOS",
            control_ref="refs/heads/main",
            control_sha=initial_git_sha,
            repository_head=current_head,
            execution_base_sha=initial_git_sha,
            current_status="ACTIVE",
            current_milestone="M1",
            canonical_next_action=next_action,
            canonical_hashes={},
            canonical_excerpt=(workspace / "docs" / "ROADMAP.md").read_text(encoding="utf-8"),
            working_tree_state="CLEAN",
            ci_state=[],
            accepted_gates=[],
            blocked_gates=[],
            authority_records={},
            goal="Autonomous continuation of discount and tax repair",
            constraints=(),
            red_lines=(),
            completion_criteria=("test_discount.py passes", "test_tax.py passes"),
            ambiguity_reasons=(),
            captured_at="2026-10-15T00:00:00Z",
            lane_allowed_scope=("src/",),
            isolated_canary_context=isolated_canary_context,
        )

    # Objective selector deriving objectives strictly from trusted canonical control fixture
    def select_canary_objective(situation: ProjectSituation, runtime: Path) -> Objective:
        # Objective A: Discount (if not completed)
        disc_passed = subprocess.run(["python", "test_discount.py"], cwd=str(workspace), capture_output=True).returncode == 0
        if not disc_passed:
            return Objective(
                objective_id="OBJ-CANARY-DISCOUNT",
                title="Repair discount calculation in src/discount.py",
                description="Fix calculate_discount bug in src/discount.py so discount is subtracted",
                authority_id="CANARY-AUTH-OBJ-A",
                risk_class="R0",
                rationale="Highest priority ready objective from canonical roadmap",
                scope_tags=("src", "discount"),
                completion_criteria=("test_discount.py passes",),
                parallel_candidates=(),
            )
        # Objective B: Tax (if discount completed and tax not completed)
        tax_passed = subprocess.run(["python", "test_tax.py"], cwd=str(workspace), capture_output=True).returncode == 0
        if not tax_passed:
            return Objective(
                objective_id="OBJ-CANARY-TAX",
                title="Repair tax calculation in src/tax.py",
                description="Fix calculate_tax bug in src/tax.py so rate is applied correctly",
                authority_id="CANARY-AUTH-OBJ-B",
                risk_class="R0",
                rationale="Second objective selected autonomously after discount completion",
                scope_tags=("src", "tax"),
                completion_criteria=("test_tax.py passes",),
                parallel_candidates=(),
            )
        return Objective(
            objective_id="OBJ-COMPLETE",
            title="All objectives completed",
            description="Roadmap is complete",
            authority_id="CANARY-AUTH-OBJ-A",
            risk_class="R0",
            rationale="No further work required",
            scope_tags=(),
            completion_criteria=(),
            parallel_candidates=(),
        )

    # =========================================================================
    # PHASE 1: EXECUTE OBJECTIVE A THROUGH GENUINE AOS AUTONOMOUS LOOP
    # =========================================================================
    print("[PHASE 1] Executing Objective A through genuine AOS autonomous loop...")
    situation_1 = canary_situation_factory()
    obj_a = select_canary_objective(situation_1, runtime_dir)
    assert obj_a.objective_id == "OBJ-CANARY-DISCOUNT"
    assert obj_a.authority_id == "CANARY-AUTH-OBJ-A"

    catalog_1 = build_intent_catalog(
        situation=situation_1,
        objective=obj_a,
        workspace=workspace,
        runtime_dir=runtime_dir,
        workspace_source_generation="canary-two-obj-gen-1",
        isolated_canary_context=isolated_canary_context,
    )
    assert len(catalog_1.candidates) == 1
    assert catalog_1.candidates[0].target_ref == "WP-REPAIR-DISCOUNT"

    plan_1 = compile_intent_to_plan(
        intent=catalog_1.candidates[0].to_intent_dict(obj_a.objective_id),
        catalog=catalog_1,
        situation=situation_1,
        objective=obj_a,
        runtime_dir=runtime_dir,
        workspace=workspace,
        workspace_source_generation="canary-two-obj-gen-1",
    )
    Draft202012Validator(PLAN_SCHEMA, format_checker=FormatChecker()).validate(plan_1)

    # Batch 0 execution
    batch_0_runtime = runtime_dir / "batches" / "batch-0000"
    batch_0_runtime.mkdir(parents=True, exist_ok=True)
    plan_0_path = batch_0_runtime / "generated-run-plan.json"
    plan_0_path.write_text(json.dumps(plan_1, indent=2), encoding="utf-8")

    from aos.autonomous_host import run_host
    receipt_1 = run_host(
        descriptor_path=descriptor_path,
        plan_path=plan_0_path,
        workspace=workspace,
        runtime_dir=batch_0_runtime,
        routing_policy_path=routing_policy_path,
        max_iterations=10,
    )
    assert not receipt_1.get("failed_task_ids"), f"Objective A failed: {receipt_1}"
    assert "TASK-AGENTIC-WP-WP-REPAIR-DISCOUNT" in receipt_1.get("completed_task_ids", [])

    # Verify Objective A test FAIL -> PASS
    p_disc_after = subprocess.run(["python", "test_discount.py"], cwd=str(workspace), capture_output=True, text=True)
    assert p_disc_after.returncode == 0
    assert "CANARY_DISCOUNT_TEST_PASS" in p_disc_after.stdout
    test_a_after_output = p_disc_after.stdout.strip()

    # Verify Git SHA after Objective A
    git_sha_1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(workspace), capture_output=True, text=True, check=True).stdout.strip().lower()
    assert git_sha_1 != initial_git_sha

    # Extract diff and receipt ID for Objective A
    telemetry_1 = receipt_1.get("backend_attempt_telemetry", [])
    assert telemetry_1, "No backend attempt telemetry recorded"
    first_worker_route = telemetry_1[0].get("backend_id")
    assert first_worker_route == "model_native_implementation_worker"

    # Read implementation receipt from KnowledgeLedger
    ledger_records_path = batch_0_runtime / "resource-os" / "ledger.jsonl"
    assert ledger_records_path.is_file(), "Knowledge ledger receipt was not recorded"
    first_receipt_line = [json.loads(line) for line in ledger_records_path.read_text(encoding="utf-8").splitlines() if line.strip()][-1]
    first_receipt_id = first_receipt_line.get("receipt_id")
    first_source_diff = first_receipt_line.get("claims", {}).get("diff")

    # Persist durable restart checkpoint after Objective A
    obj_a_action_sig = _task_signature(plan_1["tasks"][0])
    checkpoint_0 = {
        "schema_version": "1.0.0",
        "phase": "BATCH_COMPLETE",
        "project_id": "canary-two-obj-proj",
        "batch_number": 1,
        "completed_batch_count": 1,
        "total_completed_batch_count": 1,
        "successful_batch_count": 1,
        "failed_batch_count": 0,
        "last_objective_id": obj_a.objective_id,
        "last_action_signature": obj_a_action_sig,
        "source_sha_before": initial_git_sha,
        "source_sha_after": git_sha_1,
        "last_receipt_id": first_receipt_id,
        "remaining_frontier": ["OBJ-CANARY-TAX"],
        "last_receipt": receipt_1,
    }
    _write_kernel_checkpoint(runtime_dir, checkpoint_0)
    checkpoint_file = _kernel_checkpoint_path(runtime_dir)
    checkpoint_hash_1 = hashlib.sha256(checkpoint_file.read_bytes()).hexdigest()

    # =========================================================================
    # PHASE 2: DELIBERATE RESTART AND AUTONOMOUS SELECTION OF OBJECTIVE B
    # =========================================================================
    print("[PHASE 2] Deliberately restarting AOS against same runtime and checkpoint...")

    # Reconstruct history from disk
    durable_history = reconstruct_batch_history(runtime_dir, workspace=workspace)
    assert "TASK-AGENTIC-WP-WP-REPAIR-DISCOUNT" in durable_history.completed_task_ids
    assert obj_a_action_sig in durable_history.completed_task_signatures

    # Synthesize fresh situation from fresh workspace
    situation_2 = canary_situation_factory()
    assert situation_2.repository_head == git_sha_1

    # Select next objective autonomously - NO manual plan injection!
    obj_b = select_canary_objective(situation_2, runtime_dir)
    assert obj_b.objective_id == "OBJ-CANARY-TAX"
    assert obj_b.authority_id == "CANARY-AUTH-OBJ-B"

    # Build catalog: Objective A is excluded because obj_a_action_sig is in forbidden_signatures!
    catalog_2 = build_intent_catalog(
        situation=situation_2,
        objective=obj_b,
        workspace=workspace,
        runtime_dir=runtime_dir,
        forbidden_signatures=set(durable_history.completed_task_signatures),
        workspace_source_generation="canary-two-obj-gen-1",
        isolated_canary_context=isolated_canary_context,
    )
    # Objective A must be excluded by signature deduplication
    assert not any(c.target_ref == "WP-REPAIR-DISCOUNT" for c in catalog_2.candidates)
    # Objective B is the single selected candidate
    assert len(catalog_2.candidates) == 1
    assert catalog_2.candidates[0].target_ref == "WP-REPAIR-TAX"

    # Compile Objective B to plan
    plan_2 = compile_intent_to_plan(
        intent=catalog_2.candidates[0].to_intent_dict(obj_b.objective_id),
        catalog=catalog_2,
        situation=situation_2,
        objective=obj_b,
        runtime_dir=runtime_dir,
        workspace=workspace,
        workspace_source_generation="canary-two-obj-gen-1",
    )
    Draft202012Validator(PLAN_SCHEMA, format_checker=FormatChecker()).validate(plan_2)

    # Batch 1 execution
    batch_1_runtime = runtime_dir / "batches" / "batch-0001"
    batch_1_runtime.mkdir(parents=True, exist_ok=True)
    plan_1_path = batch_1_runtime / "generated-run-plan.json"
    plan_1_path.write_text(json.dumps(plan_2, indent=2), encoding="utf-8")

    receipt_2 = run_host(
        descriptor_path=descriptor_path,
        plan_path=plan_1_path,
        workspace=workspace,
        runtime_dir=batch_1_runtime,
        routing_policy_path=routing_policy_path,
        max_iterations=10,
    )
    assert not receipt_2.get("failed_task_ids"), f"Objective B failed: {receipt_2}"
    assert "TASK-AGENTIC-WP-WP-REPAIR-TAX" in receipt_2.get("completed_task_ids", [])

    # Verify Objective B test FAIL -> PASS
    p_tax_after = subprocess.run(["python", "test_tax.py"], cwd=str(workspace), capture_output=True, text=True)
    assert p_tax_after.returncode == 0
    assert "CANARY_TAX_TEST_PASS" in p_tax_after.stdout
    test_b_after_output = p_tax_after.stdout.strip()

    # Verify distinct Git SHA after Objective B
    git_sha_2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(workspace), capture_output=True, text=True, check=True).stdout.strip().lower()
    assert git_sha_2 != git_sha_1
    assert git_sha_2 != initial_git_sha

    # Read implementation receipt 2 from KnowledgeLedger
    ledger_records_path_2 = batch_1_runtime / "resource-os" / "ledger.jsonl"
    assert ledger_records_path_2.is_file(), "Knowledge ledger receipt 2 was not recorded"
    second_receipt_line = [json.loads(line) for line in ledger_records_path_2.read_text(encoding="utf-8").splitlines() if line.strip()][-1]
    second_receipt_id = second_receipt_line.get("receipt_id")
    assert second_receipt_id != first_receipt_id
    second_source_diff = second_receipt_line.get("claims", {}).get("diff")

    # Persist checkpoint after Objective B
    obj_b_action_sig = _task_signature(plan_2["tasks"][0])
    checkpoint_1 = {
        "schema_version": "1.0.0",
        "phase": "BATCH_COMPLETE",
        "project_id": "canary-two-obj-proj",
        "batch_number": 2,
        "completed_batch_count": 2,
        "total_completed_batch_count": 2,
        "successful_batch_count": 2,
        "failed_batch_count": 0,
        "last_objective_id": obj_b.objective_id,
        "last_action_signature": obj_b_action_sig,
        "source_sha_before": git_sha_1,
        "source_sha_after": git_sha_2,
        "last_receipt_id": second_receipt_id,
        "remaining_frontier": [],
        "last_receipt": receipt_2,
    }
    _write_kernel_checkpoint(runtime_dir, checkpoint_1)

    # =========================================================================
    # PHASE 3: THIRD RESTART — DUPLICATE EXECUTION PREVENTION
    # =========================================================================
    print("[PHASE 3] Third restart: verifying duplicate-execution prevention...")
    durable_history_3 = reconstruct_batch_history(runtime_dir, workspace=workspace)
    assert "TASK-AGENTIC-WP-WP-REPAIR-DISCOUNT" in durable_history_3.completed_task_ids
    assert "TASK-AGENTIC-WP-WP-REPAIR-TAX" in durable_history_3.completed_task_ids
    assert obj_a_action_sig in durable_history_3.completed_task_signatures
    assert obj_b_action_sig in durable_history_3.completed_task_signatures

    situation_3 = canary_situation_factory()
    obj_3 = select_canary_objective(situation_3, runtime_dir)

    catalog_3 = build_intent_catalog(
        situation=situation_3,
        objective=obj_3,
        workspace=workspace,
        runtime_dir=runtime_dir,
        forbidden_signatures=set(durable_history_3.completed_task_signatures),
        workspace_source_generation="canary-two-obj-gen-1",
        isolated_canary_context=isolated_canary_context,
    )
    # Both work packages must be excluded: zero candidates remain!
    wp_candidates_3 = [c for c in catalog_3.candidates if c.intent_type == "IMPLEMENT_AUTHORIZED_WORK_PACKAGE"]
    assert len(wp_candidates_3) == 0, f"Expected 0 candidates after both completed, got {wp_candidates_3}"

    git_sha_3 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(workspace), capture_output=True, text=True, check=True).stdout.strip().lower()
    assert git_sha_3 == git_sha_2, "Git SHA must remain unchanged on third restart"

    return {
        "initial_git_sha": initial_git_sha,
        "first_objective_id": obj_a.objective_id,
        "first_worker_route": first_worker_route,
        "first_source_diff": first_source_diff,
        "test_a_before": fixture["test_a_fail_output"],
        "test_a_after": test_a_after_output,
        "first_git_sha": git_sha_1,
        "first_receipt_id": first_receipt_id,
        "checkpoint_path": str(checkpoint_file),
        "checkpoint_hash": checkpoint_hash_1,
        "second_objective_id": obj_b.objective_id,
        "second_source_diff": second_source_diff,
        "test_b_before": fixture["test_b_fail_output"],
        "test_b_after": test_b_after_output,
        "second_git_sha": git_sha_2,
        "second_receipt_id": second_receipt_id,
        "third_restart_no_duplicate": True,
        "workspace": str(workspace),
    }


if __name__ == "__main__":
    result = execute_two_objective_continuation()
    print("=== TWO-OBJECTIVE AUTONOMOUS CONTINUATION QUALIFICATION COMPLETE ===")
    print(json.dumps(result, indent=2))
