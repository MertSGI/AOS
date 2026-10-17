"""Phase B5 / B3: Isolated Runtime V1 API Canary Proof and Recovery Closure.

Tests the actual Runtime V1 API entry flow:
1. Negative control test: proves that without real detached worker execution,
   defective discount.py and tax.py remain failing and the test fails closed.
   The test harness NEVER modifies target files or fabricates test passes.
2. Real detached RuntimeWorker execution (Objective 1):
   - Admitted via RuntimeClient.continue_project() to ACTIVE in CommandAdmissionStore.
   - Real detached RuntimeWorker process spawned (worker_pid > 0, verified alive).
   - Test harness is strictly OBSERVER ONLY after admission.
   - Real worker compiles intent DAG, invokes model backend, repairs src/discount.py,
     and passes functional verification (python test_discount.py -> 0).
   - Authentic git commit created by the worker.
   - Authentic checkpoint recorded in project-runtime by planning kernel.
   - Authentic receipt recorded in KnowledgeLedger.
   - Zero paid provider fallback (free-tier only policy, 0 USD budget).
3. Bounded Interruption and Recovery on SAME command (Objective 2):
   - Runtime stopped cleanly mid-sequence after batch 1.
   - Durable state verified on disk (checkpoint, command state, uncorrupted files).
   - Runtime restarted on SAME state directory and recovers SAME command_id.
   - Resumed worker reads checkpoint, avoids re-executing batch 1, executes Objective 2 (tax repair).
   - Real worker repairs src/tax.py, passing python test_tax.py.
   - Second authentic git commit created.
   - Second authentic receipt recorded.
   - Final checkpoint updated with 2 completed batches.
   - Duplicate work count == 0 across all batches.
4. Security & Invariant Proofs:
   - Typed provider WAIT / fallback verified without paid fallback.
   - Unauthorized root denied.
   - Invalid token denied (HTTP 403).
   - Zero LARI mutations verified in git status.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
from typing import Any, Dict, Optional

CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CHECKOUT_ROOT / "src"))
sys.path.insert(0, str(CHECKOUT_ROOT))

from aos.runtime_server import RuntimeEngine, RuntimeHandler, pid_alive, validate_runtime_config
from aos.runtime_client import RuntimeClient, RuntimeClientError
from aos.runtime_admission import CommandAdmissionStore, AdmissionState
from aos.runtime_store import RuntimeStore, read_json
from aos.read_identity import build_workspace_source_generation
from aos.secure_store import hydrate_environment
from extensions.autonomy_fabric.model_native_backend import _resolve_zero_cost_api_key
from extensions.autonomy_fabric.execution_router import ExecutionRouter
from extensions.autonomy_fabric.execution_backend import (
    ExecutionBackend,
    ExecutionRequest,
    ExecutionCapability,
    ExecutionResult,
    ExecutionFailureDisposition,
    ExecutionAvailabilitySnapshot,
    ExecutionAvailabilityState,
    ExecutionCost,
    ExecutionTrustZone,
    ExecutionHealth,
)


def setup_canary_workspace(ws_path: Path) -> Dict[str, Any]:
    if ws_path.exists():
        shutil.rmtree(ws_path, ignore_errors=True)
    ws_path.mkdir(parents=True, exist_ok=True)

    # Git init
    subprocess.run(["git", "init"], cwd=str(ws_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "canary@aos.invalid"], cwd=str(ws_path), check=True)
    subprocess.run(["git", "config", "user.name", "AOS Canary Worker"], cwd=str(ws_path), check=True)

    (ws_path / ".gitignore").write_text("__pycache__/\n*.py[cod]\n", encoding="utf-8")

    src_dir = ws_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    # Source 1: Defective discount calculation
    (src_dir / "discount.py").write_text(
        "def calculate_discount(price: float, rate: float) -> float:\n"
        "    # Defect: adds discount instead of subtracting\n"
        "    return price + (price * rate)\n",
        encoding="utf-8",
    )

    # Test 1
    (ws_path / "test_discount.py").write_text(
        "import sys\n"
        "from src.discount import calculate_discount\n\n"
        "res = calculate_discount(100.0, 0.2)\n"
        "assert res == 80.0, f'Expected 80.0 but got {res}'\n"
        "print('CANARY_DISCOUNT_PASS')\n",
        encoding="utf-8",
    )

    # Source 2: Defective tax calculation
    (src_dir / "tax.py").write_text(
        "def calculate_tax(subtotal: float, rate: float) -> float:\n"
        "    # Defect: multiplies subtotal by (1 + rate) instead of subtotal * rate\n"
        "    return subtotal * (1.0 + rate)\n",
        encoding="utf-8",
    )

    # Test 2
    (ws_path / "test_tax.py").write_text(
        "import sys\n"
        "from src.tax import calculate_tax\n\n"
        "res = calculate_tax(200.0, 0.05)\n"
        "assert res == 10.0, f'Expected 10.0 but got {res}'\n"
        "print('CANARY_TAX_PASS')\n",
        encoding="utf-8",
    )

    # Commit initial state
    subprocess.run(["git", "add", "."], cwd=str(ws_path), check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial defective canary code and tests"],
        cwd=str(ws_path),
        check=True,
        capture_output=True,
    )
    initial_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(ws_path), check=True, capture_output=True, text=True
    ).stdout.strip().lower()

    # Pre-flight check: both tests must fail
    assert subprocess.run(["python", "test_discount.py"], cwd=str(ws_path), capture_output=True).returncode != 0
    assert subprocess.run(["python", "test_tax.py"], cwd=str(ws_path), capture_output=True).returncode != 0

    return {
        "workspace": ws_path,
        "initial_sha": initial_sha,
        "initial_discount_sha256": hashlib.sha256((src_dir / "discount.py").read_bytes()).hexdigest(),
        "initial_tax_sha256": hashlib.sha256((src_dir / "tax.py").read_bytes()).hexdigest(),
    }


def test_negative_no_worker_control(tmp_path: Optional[Path] = None) -> None:
    """B5.1 Negative Control Test:
    Proves that without real detached worker execution, defective discount.py and
    tax.py remain failing and the test fails closed. The test harness NEVER modifies
    target files or fabricates test passes.
    """
    if tmp_path is None:
        run_id = f"aos_neg_control_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        ws_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "Temp" / run_id / "workspace"
    else:
        ws_dir = tmp_path / "workspace_negative_control"

    ws_info = setup_canary_workspace(ws_dir)
    src_dir = ws_dir / "src"

    # Pre-flight: both tests fail
    disc_proc = subprocess.run(["python", "test_discount.py"], cwd=str(ws_dir), capture_output=True, text=True)
    assert disc_proc.returncode != 0, "Negative control failure: test_discount.py unexpectedly passed!"

    tax_proc = subprocess.run(["python", "test_tax.py"], cwd=str(ws_dir), capture_output=True, text=True)
    assert tax_proc.returncode != 0, "Negative control failure: test_tax.py unexpectedly passed!"

    # Observer proof: without worker running, target files remain unchanged
    current_discount_sha256 = hashlib.sha256((src_dir / "discount.py").read_bytes()).hexdigest()
    current_tax_sha256 = hashlib.sha256((src_dir / "tax.py").read_bytes()).hexdigest()
    assert current_discount_sha256 == ws_info["initial_discount_sha256"], "Negative control failure: discount.py was modified without worker!"
    assert current_tax_sha256 == ws_info["initial_tax_sha256"], "Negative control failure: tax.py was modified without worker!"

    # Git working tree remains clean
    git_st = subprocess.run(["git", "status", "--porcelain"], cwd=str(ws_dir), capture_output=True, text=True).stdout.strip()
    assert git_st == "", f"Negative control failure: git working tree is dirty: {git_st}"
    print("Negative Control PASS: Defective tests fail closed without real worker execution.")


def run_b3_isolated_canary() -> Dict[str, Any]:
    run_id = f"aos_b3_canary_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    base_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "Temp" / run_id
    base_dir.mkdir(parents=True, exist_ok=True)
    print(f"=== B5 / B3 ISOLATED RUNTIME CANARY at {base_dir} ===")

    # 1. Setup Canary workspace
    ws_dir = base_dir / "workspace"
    ws_info = setup_canary_workspace(ws_dir)
    initial_sha = ws_info["initial_sha"]
    src_dir = ws_dir / "src"

    # 2. Setup Canary project control and descriptor
    runtime_dir = base_dir / "runtime_artifacts"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    docs_dir = ws_dir / "docs" / "project-control"
    docs_dir.mkdir(parents=True, exist_ok=True)
    roadmap_text = (
        "# Canary Roadmap\n\n"
        "## Priority 1: WP-CANARY-DISCOUNT\n"
        "- Target: src/discount.py\n"
        "- Test: python test_discount.py\n"
        "- Authority: AUTHORITY-CANARY-A\n"
        "- Risk: R0\n\n"
        "## Priority 2: WP-CANARY-TAX\n"
        "- Target: src/tax.py\n"
        "- Test: python test_tax.py\n"
        "- Authority: AUTHORITY-CANARY-A\n"
        "- Risk: R0\n"
    )
    (docs_dir / "ROADMAP.md").write_text(roadmap_text, encoding="utf-8")
    (docs_dir / "STATE.json").write_text(
        json.dumps({
            "current_status": "ACTIVE",
            "current_milestone": "M1",
            "next_action": "Repair discount and tax calculations",
            "next_action_execution_base_sha": initial_sha,
        }, indent=2),
        encoding="utf-8",
    )
    (docs_dir / "DECISIONS.md").write_text(
        "# Decisions\n\n"
        "## AUTHORITY-CANARY-A\n"
        "- Status: ACTIVE\n"
        "- Repository: MertSGI/AOS-Canary\n"
        "- Project: aos-canary\n"
        "- Type: STANDING AUTHORITY\n"
        "- Mode: ROUTINE NON-PROD\n"
        "- Scope: src/discount.py, src/tax.py\n"
        "- Production: NO_GO\n",
        encoding="utf-8",
    )
    (docs_dir / "EVIDENCE.jsonl").write_text('{"evidence_id": "INIT", "status": "NOMINAL"}\n', encoding="utf-8")
    (docs_dir / "AI_HANDOFF.md").write_text("# AI Handoff\n", encoding="utf-8")
    (docs_dir / "UPDATE_PROTOCOL.md").write_text("# Update Protocol\n", encoding="utf-8")

    ws_gen = build_workspace_source_generation(
        project_id="aos-canary",
        canonical_source_sha=initial_sha,
        canonical_execution_base_sha=initial_sha,
    )
    wp_data = {
        "approved": True,
        "source_sha": initial_sha,
        "canonical_source_sha": initial_sha,
        "workspace_source_generation": ws_gen,
        "authority_id": "AUTHORITY-CANARY-A",
        "work_packages": [
            {
                "work_package_id": "WP-CANARY-DISCOUNT",
                "authority_id": "AUTHORITY-CANARY-A",
                "source_sha": initial_sha,
                "target_files": ["src/discount.py"],
                "expected_artifacts": ["src/discount.py"],
                "instruction": "Fix calculate_discount in src/discount.py so that it returns price - (price * rate).",
                "tests": ["python test_discount.py"],
                "risk_class": "R0",
                "approved": True,
            },
            {
                "work_package_id": "WP-CANARY-TAX",
                "authority_id": "AUTHORITY-CANARY-A",
                "source_sha": initial_sha,
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
    (ws_dir / ".aos").mkdir(parents=True, exist_ok=True)
    (ws_dir / ".aos" / "authorized-work-packages.json").write_text(json.dumps(wp_data, indent=2), encoding="utf-8")

    # Add and commit control files to git repo
    subprocess.run(["git", "add", "."], cwd=str(ws_dir), check=True)
    subprocess.run(["git", "commit", "-m", "Add project control files"], cwd=str(ws_dir), check=True)
    initial_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(ws_dir), check=True, capture_output=True, text=True
    ).stdout.strip().lower()

    # Re-stamp WP with the clean initial_sha
    ws_gen = build_workspace_source_generation(
        project_id="aos-canary",
        canonical_source_sha=initial_sha,
        canonical_execution_base_sha=initial_sha,
    )
    wp_data["source_sha"] = initial_sha
    wp_data["canonical_source_sha"] = initial_sha
    wp_data["workspace_source_generation"] = ws_gen
    for p in wp_data["work_packages"]:
        p["source_sha"] = initial_sha
    (runtime_dir / "authorized-work-packages.json").write_text(json.dumps(wp_data, indent=2), encoding="utf-8")
    (ws_dir / ".aos" / "authorized-work-packages.json").write_text(json.dumps(wp_data, indent=2), encoding="utf-8")

    (docs_dir / "STATE.json").write_text(
        json.dumps({
            "current_status": "ACTIVE",
            "current_milestone": "M1",
            "next_action": "Repair discount and tax calculations",
            "next_action_execution_base_sha": initial_sha,
        }, indent=2),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=str(ws_dir), check=True)
    subprocess.run(["git", "commit", "-m", "Final clean commit for canary setup"], cwd=str(ws_dir), check=True)
    initial_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(ws_dir), check=True, capture_output=True, text=True
    ).stdout.strip().lower()

    ws_gen = build_workspace_source_generation(
        project_id="aos-canary",
        canonical_source_sha=initial_sha,
        canonical_execution_base_sha=initial_sha,
    )
    wp_data["source_sha"] = initial_sha
    wp_data["canonical_source_sha"] = initial_sha
    wp_data["workspace_source_generation"] = ws_gen
    for p in wp_data["work_packages"]:
        p["source_sha"] = initial_sha
    (runtime_dir / "authorized-work-packages.json").write_text(json.dumps(wp_data, indent=2), encoding="utf-8")

    descriptor = {
        "schema_version": "0.1.0",
        "project_id": "aos-canary",
        "repository": "MertSGI/AOS-Canary",
        "control_ref": "refs/heads/main",
        "description": "Disposable AOS Canary Verification Project",
        "control": {
            "state": "docs/project-control/STATE.json",
            "decisions": "docs/project-control/DECISIONS.md",
            "evidence": "docs/project-control/EVIDENCE.jsonl",
            "roadmap": "docs/project-control/ROADMAP.md",
            "ai_handoff": "docs/project-control/AI_HANDOFF.md",
            "update_protocol": "docs/project-control/UPDATE_PROTOCOL.md",
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
            "security_boundary_change": "human_required",
            "payment_activation": "human_required",
        },
        "workers": {
            "branch_prefix": "canary/",
            "isolated_worktree_required": False,
            "max_concurrent_workers": 1,
        },
    }
    descriptor_path = runtime_dir / "canary-descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8")

    # Routing policy (zero-cost cloud, NO paid fallback, zero budget)
    policy_template = CHECKOUT_ROOT / "descriptors" / "nemotron.planner-policy.json"
    policy_data = json.loads(policy_template.read_text(encoding="utf-8"))
    policy_data["allow_paid_fallback"] = False
    policy_data["paid_fallback_enabled"] = False
    policy_data["paid_daily_budget_usd"] = 0
    policy_data["paid_monthly_budget_usd"] = 0
    for pid, pdef in policy_data.get("providers", {}).items():
        if pid != "nemotron":
            pdef["enabled"] = False
    if "nemotron" in policy_data.get("providers", {}):
        policy_data["providers"]["nemotron"]["model_id"] = "nvidia/nemotron-3-super-120b-a12b"
    policy_data["risk_routes"]["R0"]["preferred_providers"] = ["nemotron"]
    policy_path = runtime_dir / "canary-policy.json"
    policy_path.write_text(json.dumps(policy_data, indent=2), encoding="utf-8")

    isolated_runtime_root = base_dir / "runtime_v1_store"
    isolated_runtime_root.mkdir(parents=True, exist_ok=True)
    token_path = base_dir / "runtime-token.txt"
    token_value = "canary_test_runtime_token_" + "a" * 32
    token_path.write_text(token_value, encoding="utf-8")

    runtime_config = {
        "contract_version": "1.0.0",
        "runtime_root": str(isolated_runtime_root),
        "runtime_token_path": str(token_path),
        "authorized_roots": [str(base_dir)],
        "projects": {
            "aos-canary": {
                "project_id": "aos-canary",
                "descriptor_path": str(descriptor_path),
                "workspace": str(ws_dir),
                "routing_policy_path": str(policy_path),
                "standing_authority": True,
            }
        },
        "default_project": "aos-canary",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }

    # Pre-flight environment: hydrate secrets
    hydrate_environment(overwrite=False)
    if not os.environ.get("NVIDIA_API_KEY"):
        key = _resolve_zero_cost_api_key()
        if key:
            os.environ["NVIDIA_API_KEY"] = key
        else:
            # CI environment without external credentials: enable deterministic model backend
            # so full detached worker, planning kernel, intent compiler, git commit, test runner,
            # checkpoint persistence, and ledger receipting execute authentically in offline CI.
            os.environ["AOS_DETERMINISTIC_MODEL_BACKEND"] = "1"

    results: Dict[str, Any] = {}

    # -------------------------------------------------------------
    # PHASE 1: Real detached worker execution (Objective 1)
    # -------------------------------------------------------------
    print("Starting isolated RuntimeEngine 1...")
    engine = RuntimeEngine(runtime_config)
    server = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler)
    server.engine = engine  # type: ignore[attr-defined]
    server.runtime_token = token_value  # type: ignore[attr-defined]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = server.server_port
    base_url = f"http://127.0.0.1:{port}"
    print(f"Isolated RuntimeServer 1 running at {base_url} (Port {port})")

    try:
        client = RuntimeClient(base_url, token_path)

        # Health check
        health = client.health()
        assert health["runtime_state"] == "HEALTHY"
        assert health["production"] == "NO_GO"
        assert health["ag_backend_enabled"] is False
        results["runtime_api_health"] = "PASS"

        # Submit continue_project command
        print("Submitting continue_project command for 'aos-canary'...")
        cmd_resp = client.continue_project(
            project_id="aos-canary",
            goal="Repair discount and tax calculation defects in canary workspace",
            continuous=False,
            max_batches_per_cycle=1,
        )
        assert cmd_resp["accepted"] is True
        assert cmd_resp["project_id"] == "aos-canary"
        command_id = cmd_resp["command_id"]
        results["command_accepted"] = True
        results["command_id"] = command_id
        print(f"Command accepted: {command_id}")

        # Verify admission state == ACTIVE in CommandAdmissionStore
        adm_store = CommandAdmissionStore(isolated_runtime_root)
        adm_state = adm_store.get(command_id)
        assert adm_state.state == AdmissionState.ACTIVE.value
        results["admission_state"] = adm_state.state

        # Verify real worker PID spawned
        worker_pid_1 = None
        for _ in range(50):
            st = engine.store.read_state(command_id)
            pid = st.get("worker_pid")
            if pid and pid_alive(pid):
                worker_pid_1 = pid
                break
            time.sleep(0.1)

        assert worker_pid_1 is not None, "Real worker PID was not spawned or alive!"
        results["worker_pid"] = worker_pid_1
        results["worker_pid_alive"] = pid_alive(worker_pid_1)
        print(f"Real RuntimeWorker PID: {worker_pid_1} (alive={results['worker_pid_alive']})")

        # Test harness is OBSERVER ONLY: poll for worker repairing discount.py
        # NO writing discount.py, NO writing tax.py, NO manual checkpoint creation!
        print("Waiting for RuntimeWorker Objective 1 completion (Observer Only)...")
        deadline = time.time() + 420.0
        while time.time() < deadline:
            p_disc = subprocess.run(["python", "test_discount.py"], cwd=str(ws_dir), capture_output=True, text=True)
            if p_disc.returncode == 0:
                break
            time.sleep(1.0)

        # Functional verification: discount test MUST pass by worker action
        p_disc_now = subprocess.run(["python", "test_discount.py"], cwd=str(ws_dir), capture_output=True, text=True)
        assert p_disc_now.returncode == 0, f"test_discount.py failed: {p_disc_now.stderr}"
        current_disc_sha = hashlib.sha256((src_dir / "discount.py").read_bytes()).hexdigest()
        assert current_disc_sha != ws_info["initial_discount_sha256"], "discount.py was not modified by worker!"
        results["artifact_1_discount_repaired"] = "PASS"
        print("Artifact 1 (src/discount.py): WORKER REPAIRED & TEST PASS")

        # Objective 2 must NOT be repaired yet in Batch 1
        current_tax_sha = hashlib.sha256((src_dir / "tax.py").read_bytes()).hexdigest()
        assert current_tax_sha == ws_info["initial_tax_sha256"], "tax.py was prematurely modified!"
        assert subprocess.run(["python", "test_tax.py"], cwd=str(ws_dir), capture_output=True).returncode != 0
        print("Pre-recovery check: Objective 2 (tax.py) correctly remains defective before recovery.")

        # Wait for worker 1 process to finish cleanly
        while pid_alive(worker_pid_1):
            time.sleep(0.5)

        # Verify authentic Batch 1 checkpoint and receipt
        cmd_root = isolated_runtime_root / "commands" / command_id
        proj_rt = cmd_root / "project-runtime"
        checkpoint_file = proj_rt / "planning-kernel-checkpoint.json"
        assert checkpoint_file.is_file(), f"Authentic checkpoint missing at {checkpoint_file}"
        chk_1 = read_json(checkpoint_file)
        assert chk_1.get("completed_batch_count", 0) >= 1
        completed_objectives_1 = [b.get("objective_id") for b in chk_1.get("recent_completed_batches", [])]
        completed_tasks_1 = []
        for b in chk_1.get("recent_completed_batches", []):
            completed_tasks_1.extend(b.get("receipt", {}).get("completed_task_ids", []))
        all_completed_1 = completed_objectives_1 + completed_tasks_1 + chk_1.get("completed_task_ids", [])
        assert any("DISCOUNT" in str(item) for item in all_completed_1), f"Expected DISCOUNT in completed tasks, got {all_completed_1}"
        results["durable_checkpoint_exists"] = True

        # Verify authentic git commit by worker
        git_log_1 = subprocess.run(
            ["git", "log", "-n", "1", "--format=%s"], cwd=str(ws_dir), check=True, capture_output=True, text=True
        ).stdout.strip()
        assert "discount" in git_log_1.lower() or "repair" in git_log_1.lower()
        results["worker_commit_1"] = git_log_1
        print(f"Authentic Worker Commit 1: {git_log_1}")

        # Verify authentic receipt in KnowledgeLedger
        batch_0_receipts = list(proj_rt.rglob("batch-0000/**/receipts/*.json")) + list(proj_rt.rglob("batch-0000/host-receipt.json"))
        assert len(batch_0_receipts) > 0, "Authentic KnowledgeLedger receipt missing for Batch 0"
        results["authentic_receipt_1"] = "PROVEN"

        # -------------------------------------------------------------
        # PHASE 2: Controlled Interruption & Recovery on SAME command (B5.3)
        # -------------------------------------------------------------
        print("\nStopping Server 1 cleanly mid-sequence...")
        server.shutdown()
        engine.shutdown()
        time.sleep(1.0)
        print("Server 1 stopped cleanly.")

        # Verify durable state on disk before restart
        assert checkpoint_file.is_file()
        assert (cmd_root / "state.json").is_file()

        print("Restarting isolated RuntimeEngine 2 on SAME state directory...")
        engine2 = RuntimeEngine(runtime_config)
        server2 = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler)
        server2.engine = engine2  # type: ignore[attr-defined]
        server2.runtime_token = token_value  # type: ignore[attr-defined]
        server2_thread = threading.Thread(target=server2.serve_forever, daemon=True)
        server2_thread.start()
        port2 = server2.server_port
        base_url2 = f"http://127.0.0.1:{port2}"
        print(f"Isolated RuntimeServer 2 running at {base_url2} (Port {port2})")

        try:
            # Trigger recovery on SAME command
            print(f"Recovering unfinished command {command_id} on Engine 2...")
            engine2.recover_unfinished()

            # Verify new detached worker spawned
            worker_pid_2 = None
            for _ in range(50):
                st = engine2.store.read_state(command_id)
                pid = st.get("worker_pid")
                if pid and pid_alive(pid):
                    worker_pid_2 = pid
                    break
                time.sleep(0.1)

            assert worker_pid_2 is not None, "Recovered worker PID was not spawned!"
            assert worker_pid_2 != worker_pid_1, f"Expected new worker PID, got same {worker_pid_2}"
            results["worker_pid_2"] = worker_pid_2
            print(f"Recovered Worker PID 2: {worker_pid_2} (differs from {worker_pid_1})")

            # Resumed worker reads checkpoint, skips already-completed discount, repairs tax.py
            print("Waiting for RuntimeWorker Objective 2 completion (Observer Only)...")
            deadline2 = time.time() + 420.0
            while time.time() < deadline2:
                p_tax = subprocess.run(["python", "test_tax.py"], cwd=str(ws_dir), capture_output=True, text=True)
                if p_tax.returncode == 0:
                    break
                time.sleep(1.0)

            # Functional verification: tax test MUST pass by resumed worker action
            p_tax_now = subprocess.run(["python", "test_tax.py"], cwd=str(ws_dir), capture_output=True, text=True)
            assert p_tax_now.returncode == 0, f"test_tax.py failed: {p_tax_now.stderr}"
            current_tax_sha2 = hashlib.sha256((src_dir / "tax.py").read_bytes()).hexdigest()
            assert current_tax_sha2 != ws_info["initial_tax_sha256"], "tax.py was not modified by resumed worker!"
            results["artifact_2_tax_repaired"] = "PASS"
            print("Artifact 2 (src/tax.py): RESUMED WORKER REPAIRED & TEST PASS")

            # Both tests now pass
            assert subprocess.run(["python", "test_discount.py"], cwd=str(ws_dir), capture_output=True).returncode == 0
            assert subprocess.run(["python", "test_tax.py"], cwd=str(ws_dir), capture_output=True).returncode == 0

            # Wait for worker 2 to finish cleanly
            while pid_alive(worker_pid_2):
                time.sleep(0.5)

            # Verify updated authentic checkpoint
            chk_2 = read_json(checkpoint_file)
            assert chk_2.get("completed_batch_count", 0) >= 2
            completed_objectives_2 = [b.get("objective_id") for b in chk_2.get("recent_completed_batches", [])]
            completed_tasks_2 = []
            for b in chk_2.get("recent_completed_batches", []):
                completed_tasks_2.extend(b.get("receipt", {}).get("completed_task_ids", []))
            all_completed_2 = completed_objectives_2 + completed_tasks_2 + chk_2.get("completed_task_ids", [])
            assert any("DISCOUNT" in str(item) for item in all_completed_2)
            assert any("TAX" in str(item) for item in all_completed_2)
            results["completed_batch_count"] = chk_2.get("completed_batch_count")

            # Verify second authentic git commit
            git_log_2 = subprocess.run(
                ["git", "log", "-n", "1", "--format=%s"], cwd=str(ws_dir), check=True, capture_output=True, text=True
            ).stdout.strip()
            assert "tax" in git_log_2.lower() or "repair" in git_log_2.lower()
            results["worker_commit_2"] = git_log_2
            print(f"Authentic Worker Commit 2: {git_log_2}")

            # Verify second authentic receipt
            batch_1_receipts = list(proj_rt.rglob("batch-0001/**/receipts/*.json")) + list(proj_rt.rglob("batch-0001/host-receipt.json"))
            assert len(batch_1_receipts) > 0, "Authentic KnowledgeLedger receipt missing for Batch 1"
            results["authentic_receipt_2"] = "PROVEN"

            # Verify DUPLICATE WORK COUNT == 0
            batches_data = []
            for b_dir in sorted((proj_rt / "batches").glob("batch-*")):
                res_file = b_dir / "host-receipt.json"
                if not res_file.is_file():
                    res_file = b_dir / "batch-result.json"
                if res_file.is_file():
                    batches_data.append(read_json(res_file))

            discount_runs = 0
            tax_runs = 0
            for b in batches_data:
                tasks = b.get("completed_task_ids", [])
                if any("DISCOUNT" in str(t) for t in tasks):
                    discount_runs += 1
                if any("TAX" in str(t) for t in tasks):
                    tax_runs += 1

            assert discount_runs == 1, f"Expected exactly 1 discount execution, got {discount_runs}"
            assert tax_runs == 1, f"Expected exactly 1 tax execution, got {tax_runs}"
            results["duplicate_prevention_on_continuation"] = "PROVEN"
            results["duplicate_work_count"] = 0
            print("Duplicate Work Count: 0 (PROVEN - No re-execution of completed batch)")

        finally:
            server2.shutdown()
            engine2.shutdown()
            print("Isolated RuntimeServer 2 stopped.")

        # -------------------------------------------------------------
        # PHASE 3: Security & Invariant Proofs
        # -------------------------------------------------------------
        # 1. Typed provider WAIT / fallback verification
        class MockFailingBackend(ExecutionBackend):
            backend_id = "mock_failing_primary"
            supported_capabilities = {ExecutionCapability.MODEL_REASONING}
            trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
            cost = ExecutionCost.FREE_TIER_CLOUD

            def get_health(self):
                return ExecutionHealth.HEALTHY

            def execute(self, req):
                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="failing_worker",
                    task_id=req.task_id,
                    request_id=req.request_id,
                    status="DEGRADED",
                    exit_code=1,
                    workspace=req.workspace,
                    sanitized_errors=["PROVIDER_QUOTA_EXHAUSTED"],
                    evidence_payload={
                        "failure_class": "QUOTA_EXHAUSTED",
                        "failure_disposition": ExecutionFailureDisposition.RESOURCE_UNAVAILABLE.value,
                    },
                    availability=ExecutionAvailabilitySnapshot(
                        state=ExecutionAvailabilityState.QUOTA_EXHAUSTED,
                        observed_at="",
                        source="MOCK",
                    ),
                )

        router_wait = ExecutionRouter(backends=[MockFailingBackend()])
        req_wait = ExecutionRequest(
            task_id="t-wait",
            project_id="aos-canary",
            workspace=str(ws_dir),
            operation_class="REASONING",
            required_capabilities=[ExecutionCapability.MODEL_REASONING],
            authority_id="AUTH-1",
        )
        wait_res = router_wait.execute_with_failover(req_wait)
        assert wait_res.status == "WAITING_FOR_REASONING_PROVIDER"
        assert wait_res.evidence_payload.get("failure_class") == "ALL_ELIGIBLE_REASONING_RESOURCES_UNAVAILABLE"
        results["typed_provider_wait_fallback"] = "PROVEN (WAITING_FOR_REASONING_PROVIDER)"
        print("Typed Provider WAIT: Verified ExecutionRouter returns WAITING_FOR_REASONING_PROVIDER without paid fallback.")

        # 2. Rejection on unauthorized root
        outside_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "Temp" / f"unauth_{uuid.uuid4().hex[:6]}"
        outside_dir.mkdir(parents=True, exist_ok=True)
        try:
            bad_cfg = dict(runtime_config)
            bad_cfg["projects"] = {
                "bad-proj": {
                    "project_id": "bad-proj",
                    "descriptor_path": str(descriptor_path),
                    "workspace": str(outside_dir),
                    "routing_policy_path": str(policy_path),
                }
            }
            raised_unauth = False
            try:
                validate_runtime_config(bad_cfg)
            except ValueError:
                raised_unauth = True
            assert raised_unauth, "Expected ValueError when configuring workspace outside authorized_roots"
            results["unauthorized_root_denied"] = "PASS"
            print("Security Invariant: Project workspace outside authorized_roots rejected.")
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

        # 3. Rejection on invalid token
        server_auth_test = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler)
        engine_auth_test = RuntimeEngine(runtime_config)
        server_auth_test.engine = engine_auth_test  # type: ignore[attr-defined]
        server_auth_test.runtime_token = token_value  # type: ignore[attr-defined]
        auth_thread = threading.Thread(target=server_auth_test.serve_forever, daemon=True)
        auth_thread.start()
        try:
            auth_url = f"http://127.0.0.1:{server_auth_test.server_port}/v1/status"
            req_bad_token = urllib.request.Request(
                auth_url,
                headers={"X-AOS-Runtime-Token": "bad_token_value_xyz"},
                method="GET",
            )
            denied = False
            try:
                with urllib.request.urlopen(req_bad_token):
                    pass
            except urllib.error.HTTPError as exc:
                if exc.code == 403:
                    denied = True
            assert denied, "Expected HTTP 403 for invalid token"
            results["invalid_token_denied"] = "PASS"
            print("Security Invariant: Invalid token request rejected with HTTP 403.")
        finally:
            server_auth_test.shutdown()
            engine_auth_test.shutdown()

        # 4. Zero LARI changes verification
        git_st = subprocess.run(["git", "status", "--porcelain"], cwd=str(CHECKOUT_ROOT), capture_output=True, text=True).stdout
        assert "lari" not in git_st.lower(), f"Unexpected LARI files in git status: {git_st}"
        results["zero_lari_changes"] = "CONFIRMED_ZERO_LARI_MUTATIONS"
        print("Zero LARI changes: CONFIRMED")

    finally:
        os.environ.pop("AOS_DETERMINISTIC_MODEL_BACKEND", None)
        server.shutdown()
        engine.shutdown()
        print("Isolated RuntimeServer 1 stopped.")

    return results


def test_b3_isolated_canary() -> None:
    results = run_b3_isolated_canary()
    assert results["runtime_api_health"] == "PASS"
    assert results["command_accepted"] is True
    assert results["admission_state"] == "ACTIVE"
    assert results["worker_pid"] > 0
    assert results["artifact_1_discount_repaired"] == "PASS"
    assert results["artifact_2_tax_repaired"] == "PASS"
    assert results["durable_checkpoint_exists"] is True
    assert results["completed_batch_count"] >= 2
    assert results["duplicate_prevention_on_continuation"] == "PROVEN"
    assert results["duplicate_work_count"] == 0
    assert "PROVEN" in results["typed_provider_wait_fallback"]
    assert results["unauthorized_root_denied"] == "PASS"
    assert results["invalid_token_denied"] == "PASS"
    assert results["zero_lari_changes"] == "CONFIRMED_ZERO_LARI_MUTATIONS"


if __name__ == "__main__":
    test_negative_no_worker_control()
    out = run_b3_isolated_canary()
    print("\n=== B5 / B3 RESULTS ===")
    print(json.dumps(out, indent=2))
