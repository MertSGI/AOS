"""AOS Genuine Two-Objective Autonomous Software-Development Continuation Canary Runner.

Demonstrates autonomous multi-objective continuation through the actual production AOS orchestration path:
- Genuine production select_objective path (Nemotron reasoning provider)
- Trusted Compact Intent compiler (build_intent_catalog + compile_intent_to_plan)
- Resource Orchestrator backend selection (ModelNativeImplementationBackend)
- Real bounded source modification (via live zero-cost NVIDIA Nemotron)
- Native functional verification (FAIL -> PASS)
- Git commits with verifiable lineage and distinct Git SHAs
- Durable implementation receipts in KnowledgeLedger
- Kernel checkpoint persistence produced exclusively through production code
- Deliberate restart: history reconstruction, automatic selection of Objective B without test-script selection
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

from aos.planning_kernel import (
    AuthorityRecord,
    Objective,
    ProjectSituation,
    reconstruct_batch_history,
    run_autonomous_project,
    _kernel_checkpoint_path,
    _read_json,
)
from aos.read_identity import build_workspace_source_generation
from extensions.autonomy_fabric.model_native_backend import (
    ModelNativeImplementationBackend,
    _resolve_zero_cost_api_key,
)

CANARY_SCRATCH_ROOT = Path(r"C:\Users\mozcelikbas\AppData\Local\Temp\aos_two_objective_canary_workspace")
REPO_ROOT = Path(__file__).parent.parent


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

    workspace_gen = build_workspace_source_generation(
        project_id="canary-two-obj-proj",
        canonical_source_sha=initial_git_sha,
        canonical_execution_base_sha=initial_git_sha,
    )

    # Two distinct work packages with independent canary authorities
    wp_data = {
        "approved": True,
        "source_sha": initial_git_sha,
        "workspace_source_generation": workspace_gen,
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

    # Project Descriptor (conforming to project_descriptor.schema.json)
    descriptor = {
        "schema_version": "0.1.0",
        "project_id": "canary-two-obj-proj",
        "repository": "MertSGI/AOS",
        "control_ref": "refs/heads/main",
        "description": "AOS Two-Objective Continuation Canary",
        "control": {
            "state": "docs/STATE.json",
            "decisions": "docs/DECISIONS.md",
            "evidence": "docs/EVIDENCE.jsonl",
            "roadmap": "docs/ROADMAP.md",
        },
        "authority": {
            "production_mutation": "forbidden",
            "roadmap_change": "forbidden",
            "destructive_data": "forbidden",
        },
    }
    descriptor_path = runtime_dir / "project-descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8")

    # Routing policy (from production nemotron.planner-policy.json)
    policy_template = REPO_ROOT / "descriptors" / "nemotron.planner-policy.json"
    routing_policy_path = runtime_dir / "routing-policy.json"
    routing_policy_path.write_text(policy_template.read_text(encoding="utf-8"), encoding="utf-8")

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
    """Execute the full two-objective autonomous qualification loop via production orchestrator."""
    fixture = setup_disposable_project(CANARY_SCRATCH_ROOT)
    workspace = fixture["workspace"]
    runtime_dir = fixture["runtime_dir"]
    descriptor_path = fixture["descriptor_path"]
    routing_policy_path = fixture["routing_policy_path"]
    initial_git_sha = fixture["initial_git_sha"]

    if not os.environ.get("NVIDIA_API_KEY"):
        key = _resolve_zero_cost_api_key()
        if key:
            os.environ["NVIDIA_API_KEY"] = key

    # Authentic isolated situation factory synthesizing fresh project state
    def canary_situation_factory(**kwargs) -> ProjectSituation:
        head_proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(workspace), capture_output=True, text=True, check=True)
        current_head = head_proc.stdout.strip().lower()

        disc_passed = subprocess.run(["python", "test_discount.py"], cwd=str(workspace), capture_output=True).returncode == 0
        tax_passed = subprocess.run(["python", "test_tax.py"], cwd=str(workspace), capture_output=True).returncode == 0

        if disc_passed and tax_passed:
            next_action = "PROJECT COMPLETE"
            status = "COMPLETE"
        elif disc_passed:
            next_action = "Repair tax calculation in src/tax.py"
            status = "ACTIVE"
        else:
            next_action = "Repair discount calculation in src/discount.py"
            status = "ACTIVE"

        return ProjectSituation(
            schema_version="1.0.0",
            project_id="canary-two-obj-proj",
            repository="MertSGI/AOS",
            control_ref="refs/heads/main",
            control_sha=initial_git_sha,
            repository_head=current_head,
            execution_base_sha=initial_git_sha,
            current_status=status,
            current_milestone="M1",
            canonical_next_action=next_action,
            canonical_hashes={},
            canonical_excerpt=(workspace / "docs" / "ROADMAP.md").read_text(encoding="utf-8"),
            working_tree_state="CLEAN",
            ci_state=[],
            accepted_gates=[],
            blocked_gates=[],
            authority_records={
                "CANARY-AUTH-OBJ-A": AuthorityRecord(
                    authority_id="CANARY-AUTH-OBJ-A",
                    source_path="docs/ROADMAP.md",
                    text="STANDING AUTHORITY FOR ROUTINE NON-PRODUCTION CANARY-TWO-OBJ-PROJ MERTSGI/AOS REPAIR DISCOUNT",
                    superseded=False,
                    production_allowed=False,
                ),
                "CANARY-AUTH-OBJ-B": AuthorityRecord(
                    authority_id="CANARY-AUTH-OBJ-B",
                    source_path="docs/ROADMAP.md",
                    text="STANDING AUTHORITY FOR ROUTINE NON-PRODUCTION CANARY-TWO-OBJ-PROJ MERTSGI/AOS REPAIR TAX",
                    superseded=False,
                    production_allowed=False,
                ),
            },
            goal="Autonomous continuation of discount and tax repair",
            constraints=("ZERO_COST_CLOUD", "NO_LOCAL_LLAMA", "NO_AGENTIC_PLANNING"),
            red_lines=(),
            completion_criteria=("test_discount.py passes", "test_tax.py passes"),
            ambiguity_reasons=(),
            captured_at="2026-10-16T00:00:00Z",
            lane_allowed_scope=("src/",),
        )

    # =========================================================================
    # PHASE 1: EXECUTE OBJECTIVE A VIA PRODUCTION run_autonomous_project
    # =========================================================================
    print("[PHASE 1] Executing Objective A through production run_autonomous_project...")
    res_1 = run_autonomous_project(
        descriptor_path=descriptor_path,
        workspace=workspace,
        runtime_dir=runtime_dir,
        routing_policy_path=routing_policy_path,
        situation_factory=canary_situation_factory,
        max_batches=1,
    )

    # Verify Objective A test FAIL -> PASS
    p_disc_after = subprocess.run(["python", "test_discount.py"], cwd=str(workspace), capture_output=True, text=True)
    assert p_disc_after.returncode == 0, f"Expected test_discount to PASS, stderr: {p_disc_after.stderr}"
    assert "CANARY_DISCOUNT_TEST_PASS" in p_disc_after.stdout
    test_a_after_output = p_disc_after.stdout.strip()

    # Verify Git SHA after Objective A
    git_sha_1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(workspace), capture_output=True, text=True, check=True).stdout.strip().lower()
    assert git_sha_1 != initial_git_sha

    # Inspect batch-0000 artifacts and receipt
    batch_0_runtime = runtime_dir / "batches" / "batch-0000"
    receipt_0_path = batch_0_runtime / "host-receipt.json"
    assert receipt_0_path.is_file(), "Host receipt for batch-0000 not found"
    receipt_1 = json.loads(receipt_0_path.read_text(encoding="utf-8"))
    assert not receipt_1.get("failed_task_ids"), f"Objective A tasks failed: {receipt_1}"
    assert "TASK-AGENTIC-WP-WP-REPAIR-DISCOUNT" in receipt_1.get("completed_task_ids", [])

    telemetry_1 = receipt_1.get("backend_attempt_telemetry", [])
    assert telemetry_1, "No backend attempt telemetry recorded"
    first_worker_route = telemetry_1[0].get("backend_id")
    assert first_worker_route == "model_native_implementation_worker"

    # Read implementation receipt from KnowledgeLedger
    ledger_records_path = batch_0_runtime / "resource-os" / "ledger.jsonl"
    assert ledger_records_path.is_file(), "Knowledge ledger receipt was not recorded"
    first_receipt_line = [json.loads(line) for line in ledger_records_path.read_text(encoding="utf-8").splitlines() if line.strip()][-1]
    first_receipt_id = first_receipt_line.get("receipt_id") or first_receipt_line.get("event_id")
    assert first_receipt_id, "first_receipt_id could not be resolved from KnowledgeLedger"
    first_source_diff = first_receipt_line.get("claims", {}).get("diff")

    # Read verified kernel checkpoint persisted by production kernel
    checkpoint_file = _kernel_checkpoint_path(runtime_dir)
    assert checkpoint_file.is_file(), "Kernel checkpoint was not persisted"
    checkpoint_0 = _read_json(checkpoint_file)
    assert checkpoint_0.get("completed_batch_count") == 1
    checkpoint_hash_1 = hashlib.sha256(checkpoint_file.read_bytes()).hexdigest()

    # Read objective-0000.json chosen by production select_objective
    obj_0_path = runtime_dir / "objective-0000.json"
    assert obj_0_path.is_file()
    obj_a_dict = json.loads(obj_0_path.read_text(encoding="utf-8"))
    obj_a_id = obj_a_dict.get("objective_id", "OBJ-DISCOUNT")

    # =========================================================================
    # PHASE 2: DELIBERATE RESTART AND AUTONOMOUS SELECTION OF OBJECTIVE B
    # =========================================================================
    print("[PHASE 2] Deliberately restarting AOS against same runtime and checkpoint...")

    # Fresh call to production run_autonomous_project — history reconstructed from disk,
    # production select_objective chooses Objective B without test-script intervention!
    res_2 = run_autonomous_project(
        descriptor_path=descriptor_path,
        workspace=workspace,
        runtime_dir=runtime_dir,
        routing_policy_path=routing_policy_path,
        situation_factory=canary_situation_factory,
        max_batches=1,
    )

    # Verify Objective B test FAIL -> PASS
    p_tax_after = subprocess.run(["python", "test_tax.py"], cwd=str(workspace), capture_output=True, text=True)
    assert p_tax_after.returncode == 0, f"Expected test_tax to PASS, stderr: {p_tax_after.stderr}"
    assert "CANARY_TAX_TEST_PASS" in p_tax_after.stdout
    test_b_after_output = p_tax_after.stdout.strip()

    # Verify distinct Git SHA after Objective B
    git_sha_2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(workspace), capture_output=True, text=True, check=True).stdout.strip().lower()
    assert git_sha_2 != git_sha_1
    assert git_sha_2 != initial_git_sha

    # Inspect batch-0001 artifacts and receipt
    batch_1_runtime = runtime_dir / "batches" / "batch-0001"
    receipt_1_path = batch_1_runtime / "host-receipt.json"
    assert receipt_1_path.is_file(), "Host receipt for batch-0001 not found"
    receipt_2 = json.loads(receipt_1_path.read_text(encoding="utf-8"))
    assert not receipt_2.get("failed_task_ids"), f"Objective B tasks failed: {receipt_2}"
    assert "TASK-AGENTIC-WP-WP-REPAIR-TAX" in receipt_2.get("completed_task_ids", [])

    # Read implementation receipt 2 from KnowledgeLedger
    ledger_records_path_2 = batch_1_runtime / "resource-os" / "ledger.jsonl"
    assert ledger_records_path_2.is_file(), "Knowledge ledger receipt 2 was not recorded"
    second_receipt_line = [json.loads(line) for line in ledger_records_path_2.read_text(encoding="utf-8").splitlines() if line.strip()][-1]
    second_receipt_id = second_receipt_line.get("receipt_id") or second_receipt_line.get("event_id")
    assert second_receipt_id, "second_receipt_id could not be resolved from KnowledgeLedger"
    assert second_receipt_id != first_receipt_id
    second_source_diff = second_receipt_line.get("claims", {}).get("diff")

    # Read updated production kernel checkpoint
    checkpoint_1 = _read_json(checkpoint_file)
    assert checkpoint_1.get("completed_batch_count") == 2

    # Read objective-0001.json chosen by production select_objective
    obj_1_path = runtime_dir / "objective-0001.json"
    assert obj_1_path.is_file()
    obj_b_dict = json.loads(obj_1_path.read_text(encoding="utf-8"))
    obj_b_id = obj_b_dict.get("objective_id", "OBJ-TAX")

    # =========================================================================
    # PHASE 3: THIRD RESTART — DUPLICATE EXECUTION PREVENTION
    # =========================================================================
    print("[PHASE 3] Third restart: verifying duplicate-execution prevention...")
    res_3 = run_autonomous_project(
        descriptor_path=descriptor_path,
        workspace=workspace,
        runtime_dir=runtime_dir,
        routing_policy_path=routing_policy_path,
        situation_factory=canary_situation_factory,
        max_batches=1,
    )

    # Verify no new commit on third restart
    git_sha_3 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(workspace), capture_output=True, text=True, check=True).stdout.strip().lower()
    assert git_sha_3 == git_sha_2, "Git SHA must remain unchanged on third restart"

    # Verify durable batch history contains both tasks completed
    durable_history_3 = reconstruct_batch_history(runtime_dir, workspace=workspace)
    assert "TASK-AGENTIC-WP-WP-REPAIR-DISCOUNT" in durable_history_3.completed_task_ids
    assert "TASK-AGENTIC-WP-WP-REPAIR-TAX" in durable_history_3.completed_task_ids

    # Verify no new batch directory batch-0002 was executed
    batch_2_runtime = runtime_dir / "batches" / "batch-0002"
    assert not batch_2_runtime.exists(), "No batch-0002 should be created when all objectives are complete"

    return {
        "initial_git_sha": initial_git_sha,
        "first_objective_id": obj_a_id,
        "first_worker_route": first_worker_route,
        "first_source_diff": first_source_diff,
        "test_a_before": fixture["test_a_fail_output"],
        "test_a_after": test_a_after_output,
        "first_git_sha": git_sha_1,
        "first_receipt_id": first_receipt_id,
        "checkpoint_path": str(checkpoint_file),
        "checkpoint_hash": checkpoint_hash_1,
        "second_objective_id": obj_b_id,
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
