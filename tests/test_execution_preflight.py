"""Deterministic offline unit tests for PreEngineExecutionGate and PreflightControlledExecutionController."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, create_autospec

import pytest
from aos.controlled_execution import ControlledExecutionEngine
from aos.execution_preflight import (
    PreEngineExecutionGate,
    PreEngineExecutionRequest,
    PreflightControlledExecutionController,
)


class FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeEngine:
    def __init__(self, disposition: str = "VERIFIED_CANDIDATE", raises: bool = False):
        self.call_count = 0
        self.last_kwargs: Optional[Dict[str, Any]] = None
        self.disposition = disposition
        self.raises = raises

    def execute(self, local_target_repo_path: Optional[str] = None) -> Dict[str, Any]:
        self.call_count += 1
        self.last_kwargs = {"local_target_repo_path": local_target_repo_path}
        if self.raises:
            raise RuntimeError("Synthetic engine failure")
        return {"status": self.disposition, "disposition": self.disposition}


def make_fixture_env(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    auth_dir = repo_dir / "docs" / "proofs"
    auth_dir.mkdir(parents=True, exist_ok=True)
    auth_file = auth_dir / "execution_authorization.json"

    state_dir = repo_dir / "docs" / "project-control"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "STATE.json"

    cli_dir = tmp_path / "bin"
    cli_dir.mkdir(parents=True, exist_ok=True)
    cli_file = cli_dir / "antigravity.exe"
    cli_bytes = b"fake antigravity executable content"
    cli_file.write_bytes(cli_bytes)
    cli_hash = hashlib.sha256(cli_bytes).hexdigest()

    cap_dir = tmp_path / "caps"
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_store = cap_dir / "antigravity.json"

    raw_result = tmp_path / "raw_execution_result.json"

    auth_data = {
        "schema_version": "0.1.0",
        "authorization_id": "AOS4-REF-001-R1-ATTEMPT6-POLICY-20260827T200000Z",
        "project_id": "test_project",
        "task_id": "TASK-001",
        "gate": "AOS-4",
        "risk_class": "R1",
        "decision": "AUTO_EXECUTE",
        "authority_source": "POLICY_AUTONOMOUS",
        "reason_codes": ["TEST_REASON"],
        "control_source_sha": "1111111111111111111111111111111111111111",
        "execution_base_sha": "2222222222222222222222222222222222222222",
        "timestamp": "2026-08-27T20:00:00Z",
    }
    auth_file.write_text(json.dumps(auth_data, indent=2), encoding="utf-8")

    state_data = {
        "schema_version": "0.1.0",
        "updated_at": "2026-08-27T20:00:00Z",
        "project": {
            "name": "AOS",
            "expanded_name": "Agent Operating System",
            "working_name_status": "PROVISIONAL",
            "purpose": "Reusable AI-plus-deterministic development orchestration platform",
        },
        "status": "PRE_ENGINE_EXECUTION_GATE_IMPLEMENTED_INDEPENDENT_REVIEW_PENDING",
        "current_gate": "AOS-4",
        "delivery_train": [
            "AOS-0 Foundation & Charter",
            "AOS-1 Canonical Memory & Schemas",
            "AOS-2 Shadow Orchestrator",
            "AOS-3 Controlled Single-Worker Execution",
            "AOS-4 Independent Verification & HOLD",
        ],
        "principles": {
            "fail_closed": True,
            "evidence_over_claims": True,
            "exact_revision_required": True,
            "planner_executor_verifier_separation": True,
            "human_critical_authority": True,
            "portable_multi_project": True,
            "multi_pc_required": True,
            "canonical_truth_in_version_control": True,
        },
        "next_action": "Test next action string",
        "extensions": {
            "aos4_independent_verification": {
                "next_execution_attempt_number": 6,
                "next_execution_authorization_status": "POLICY_AUTHORIZED",
                "attempt_6_authorization_id": "AOS4-REF-001-R1-ATTEMPT6-POLICY-20260827T200000Z",
                "attempt_6_authorization_status": "ISSUED_NOT_CONSUMED",
                "attempt_6_authorization_consumed": False,
                "attempt_6_authorization_control_source_sha": "1111111111111111111111111111111111111111",
                "attempt_6_execution_base_sha": "2222222222222222222222222222222222222222",
                "attempt_6_execution_actual": 0,
                "attempt_6_worker_execution_actual": 0,
                "attempt_6_retry_actual": 0,
                "attempt_6_terminal": False,
            }
        },
    }
    state_file.write_text(json.dumps(state_data, indent=2), encoding="utf-8")

    cap_data = {
        "schema_version": "0.1.0",
        "worker_adapter": "antigravity",
        "adapter_contract_version": "0.2.9",
        "executable_filename": "antigravity.exe",
        "executable_sha256": cli_hash,
        "reported_cli_version": "1.1.20",
        "runtime_environment_profile_version": "0.1.0",
        "runtime_environment_fingerprint_sha256": "3333333333333333333333333333333333333333333333333333333333333333",
        "capability_status": "PROVEN",
        "probe_id": "PROBE-20260827-001",
        "probe_timestamp": "2026-08-27T20:00:00Z",
        "aos_revision_used_for_probe": "1111111111111111111111111111111111111111",
        "capabilities_proven": ["native_file_edit"],
        "limitations": ["Test limitation"],
    }
    cap_store.write_text(json.dumps(cap_data, indent=2), encoding="utf-8")

    return {
        "repo_dir": repo_dir,
        "auth_file": auth_file,
        "state_file": state_file,
        "cli_file": cli_file,
        "cli_hash": cli_hash,
        "cap_store": cap_store,
        "raw_result": raw_result,
        "auth_data": auth_data,
        "state_data": state_data,
        "cap_data": cap_data,
    }


def make_fake_git_runner(
    branch: str = "feature/aos-4-independent-verification-hold",
    head: str = "3333333333333333333333333333333333333333",
    parent: str = "1111111111111111111111111111111111111111",
    status_porcelain: str = "",
    remote_head: Optional[str] = None,
    auth_carrier_present: bool = True,
    auth_parent_present: bool = False,
    state_carrier_present: bool = True,
    carrier_ls_tree_fails: bool = False,
    parent_ls_tree_fails: bool = False,
    git_fails: bool = False,
):
    remote_target = remote_head if remote_head is not None else head

    def git_runner(cmd: List[str], cwd: Path) -> FakeCompletedProcess:
        if git_fails:
            return FakeCompletedProcess(returncode=1, stderr="Git execution error simulated")

        if cmd == ["branch", "--show-current"]:
            return FakeCompletedProcess(0, f"{branch}\n")
        elif cmd == ["rev-parse", "HEAD"]:
            return FakeCompletedProcess(0, f"{head}\n")
        elif cmd == ["rev-parse", "HEAD^"]:
            return FakeCompletedProcess(0, f"{parent}\n")
        elif cmd == ["status", "--porcelain"]:
            return FakeCompletedProcess(0, status_porcelain)
        elif cmd[0] == "ls-remote":
            return FakeCompletedProcess(0, f"{remote_target}\t{cmd[2]}\n")
        elif cmd[0] == "ls-tree" and cmd[1] == "--name-only":
            target_ref = cmd[2]
            target_path = cmd[4] if len(cmd) > 4 else ""

            if target_ref == "HEAD":
                if carrier_ls_tree_fails:
                    return FakeCompletedProcess(1, stderr="ls-tree HEAD failure")
                if "execution_authorization.json" in target_path:
                    return FakeCompletedProcess(0, f"{target_path}\n" if auth_carrier_present else "")
                elif "STATE.json" in target_path:
                    return FakeCompletedProcess(0, f"{target_path}\n" if state_carrier_present else "")
                return FakeCompletedProcess(0, f"{target_path}\n")

            elif target_ref == parent:
                if parent_ls_tree_fails:
                    return FakeCompletedProcess(1, stderr="ls-tree parent failure")
                if "execution_authorization.json" in target_path:
                    return FakeCompletedProcess(0, f"{target_path}\n" if auth_parent_present else "")
                return FakeCompletedProcess(0, "")

            return FakeCompletedProcess(0, "")

        return FakeCompletedProcess(0, "", "")

    return git_runner


def make_request(env: Dict[str, Any]) -> PreEngineExecutionRequest:
    return PreEngineExecutionRequest(
        local_target_repo_path=env["repo_dir"],
        expected_control_branch="feature/aos-4-independent-verification-hold",
        authorization_artifact_path=env["auth_file"],
        canonical_state_path=env["state_file"],
        attempt_number=6,
        cli_command=env["cli_file"],
        raw_result_path=env["raw_result"],
        capability_store_path=env["cap_store"],
    )


# Section 15: Real ControlledExecutionEngine autospec signature test
def test_real_controlled_execution_engine_autospec_signature(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    def fake_version_resolver(cli_path: Path):
        return "1.1.20"

    def fake_cap_resolver(cli_path: Path, attestation: Dict[str, Any], identity: Dict[str, Any]):
        return "PROVEN"

    gate = PreEngineExecutionGate(
        cli_version_resolver=fake_version_resolver,
        capability_resolver=fake_cap_resolver,
        git_runner=make_fake_git_runner(),
    )
    engine = create_autospec(ControlledExecutionEngine, instance=True)
    engine.execute.return_value = {
        "status": "PASS",
        "disposition": "VERIFIED_CANDIDATE",
        "candidate_id": "CAND-001",
    }
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "PASS"
    assert res["engine_invoked"] is True
    assert res["disposition"] == "VERIFIED_CANDIDATE"
    engine.execute.assert_called_once_with(
        local_target_repo_path=str(req.local_target_repo_path)
    )


# Section 16: Exact Engine Result Test
def test_exact_engine_result_preserved(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = PreEngineExecutionGate(
        cli_version_resolver=lambda p: "1.1.20",
        capability_resolver=lambda p, a, i: "PROVEN",
        git_runner=make_fake_git_runner(),
    )
    engine = FakeEngine(disposition="VERIFICATION_FAILED")
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "PASS"
    assert res["engine_invoked"] is True
    assert res["disposition"] == "VERIFICATION_FAILED"
    assert res["status"] == "VERIFICATION_FAILED"
    assert res["engine_result"] == {"status": "VERIFICATION_FAILED", "disposition": "VERIFICATION_FAILED"}


# Section 17: One-Shot After Hold Test
def test_one_shot_enforced_after_preflight_hold(tmp_path):
    env = make_fixture_env(tmp_path)
    env["cli_file"].write_bytes(b"tampered content")  # Force CLI hash mismatch
    req = make_request(env)

    version_calls = []
    cap_calls = []

    gate = PreEngineExecutionGate(
        cli_version_resolver=lambda p: version_calls.append(1) or "1.1.20",
        capability_resolver=lambda p, a, i: cap_calls.append(1) or "PROVEN",
        git_runner=make_fake_git_runner(),
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res1 = controller.execute(req)
    assert res1["preflight_status"] == "HOLD"
    assert res1["engine_invoked"] is False
    assert engine.call_count == 0
    assert len(version_calls) == 0

    # Fix CLI file for second call on SAME controller instance
    env["cli_file"].write_bytes(b"fake antigravity executable content")

    res2 = controller.execute(req)
    assert res2["preflight_status"] == "HOLD"
    assert res2["engine_invoked"] is False
    assert engine.call_count == 0
    assert len(version_calls) == 0
    assert len(cap_calls) == 0


# Section 18: Default Resolver No Second CLI Query Test
def test_default_capability_resolver_reuses_identity(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    version_calls = []

    def fake_version_resolver(cli_path: Path):
        version_calls.append(cli_path)
        return "1.1.20"

    cap_identity_passed = []

    def fake_cap_resolver(cli_path: Path, attestation: Dict[str, Any], identity: Dict[str, Any]):
        cap_identity_passed.append(identity)
        return "PROVEN"

    gate = PreEngineExecutionGate(
        cli_version_resolver=fake_version_resolver,
        capability_resolver=fake_cap_resolver,
        git_runner=make_fake_git_runner(),
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "PASS"
    assert len(version_calls) == 1  # Exactly 1 CLI version query!
    assert len(cap_identity_passed) == 1
    assert cap_identity_passed[0]["version"] == "1.1.20"
    assert cap_identity_passed[0]["sha256"] == env["cli_hash"]


# Section 19: Absolute CLI Path Test
def test_relative_cli_path_fails(tmp_path):
    env = make_fixture_env(tmp_path)
    env_req = make_request(env)

    # Use relative path
    req = PreEngineExecutionRequest(
        local_target_repo_path=env_req.local_target_repo_path,
        expected_control_branch=env_req.expected_control_branch,
        authorization_artifact_path=env_req.authorization_artifact_path,
        canonical_state_path=env_req.canonical_state_path,
        attempt_number=env_req.attempt_number,
        cli_command="agy",  # Relative path!
        raw_result_path=env_req.raw_result_path,
        capability_store_path=env_req.capability_store_path,
    )

    version_calls = []
    gate = PreEngineExecutionGate(
        cli_version_resolver=lambda p: version_calls.append(1) or "1.1.20",
        capability_resolver=lambda p, a, i: "PROVEN",
        git_runner=make_fake_git_runner(),
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False
    assert engine.call_count == 0
    assert len(version_calls) == 0


# Section 20: External Canonical File Tests
def test_external_auth_artifact_holds(tmp_path):
    env = make_fixture_env(tmp_path)
    env_req = make_request(env)

    external_auth = tmp_path / "external_auth.json"
    external_auth.write_text(env["auth_file"].read_text())

    req = PreEngineExecutionRequest(
        local_target_repo_path=env_req.local_target_repo_path,
        expected_control_branch=env_req.expected_control_branch,
        authorization_artifact_path=external_auth,  # Outside target repo!
        canonical_state_path=env_req.canonical_state_path,
        attempt_number=env_req.attempt_number,
        cli_command=env_req.cli_command,
        raw_result_path=env_req.raw_result_path,
        capability_store_path=env_req.capability_store_path,
    )

    gate = PreEngineExecutionGate(git_runner=make_fake_git_runner())
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_external_canonical_state_holds(tmp_path):
    env = make_fixture_env(tmp_path)
    env_req = make_request(env)

    external_state = tmp_path / "external_state.json"
    external_state.write_text(env["state_file"].read_text())

    req = PreEngineExecutionRequest(
        local_target_repo_path=env_req.local_target_repo_path,
        expected_control_branch=env_req.expected_control_branch,
        authorization_artifact_path=env_req.authorization_artifact_path,
        canonical_state_path=external_state,  # Outside target repo!
        attempt_number=env_req.attempt_number,
        cli_command=env_req.cli_command,
        raw_result_path=env_req.raw_result_path,
        capability_store_path=env_req.capability_store_path,
    )

    gate = PreEngineExecutionGate(git_runner=make_fake_git_runner())
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Section 21: Carrier Read-Failure Test
def test_carrier_ls_tree_failure_holds(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = PreEngineExecutionGate(git_runner=make_fake_git_runner(carrier_ls_tree_fails=True))
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_parent_ls_tree_failure_holds(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = PreEngineExecutionGate(git_runner=make_fake_git_runner(parent_ls_tree_fails=True))
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Section 22: Canonical State Presence Test
def test_canonical_state_absent_from_head_holds(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = PreEngineExecutionGate(git_runner=make_fake_git_runner(state_carrier_present=False))
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test CLI Hash Mismatch blocks version and engine
def test_cli_hash_mismatch_blocks_version_and_engine(tmp_path):
    env = make_fixture_env(tmp_path)
    env["cli_file"].write_bytes(b"tampered content")
    req = make_request(env)

    version_calls = []
    gate = PreEngineExecutionGate(
        cli_version_resolver=lambda p: version_calls.append(1) or "1.1.20",
        capability_resolver=lambda p, a, i: "PROVEN",
        git_runner=make_fake_git_runner(),
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False
    assert engine.call_count == 0
    assert len(version_calls) == 0


# Test CLI Version Mismatch
def test_cli_version_mismatch_after_hash_match(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    version_calls = []
    cap_calls = []
    gate = PreEngineExecutionGate(
        cli_version_resolver=lambda p: version_calls.append(1) or "1.1.21",  # Mismatch
        capability_resolver=lambda p, a, i: cap_calls.append(1) or "PROVEN",
        git_runner=make_fake_git_runner(),
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False
    assert engine.call_count == 0
    assert len(version_calls) == 1
    assert len(cap_calls) == 0


# Test Capability UNPROVEN
def test_capability_unproven_blocks_engine(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = PreEngineExecutionGate(
        cli_version_resolver=lambda p: "1.1.20",
        capability_resolver=lambda p, a, i: "UNPROVEN",
        git_runner=make_fake_git_runner(),
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False
    assert engine.call_count == 0


# Test Engine Exception Fail Closed
def test_engine_exception_fails_closed(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = PreEngineExecutionGate(
        cli_version_resolver=lambda p: "1.1.20",
        capability_resolver=lambda p, a, i: "PROVEN",
        git_runner=make_fake_git_runner(),
    )
    engine = FakeEngine(raises=True)
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "PASS"
    assert res["engine_invoked"] is True
    assert res["disposition"] == "HOLD"
    assert res["status"] == "HOLD"
    assert "Synthetic engine failure" in res["errors"][0]
