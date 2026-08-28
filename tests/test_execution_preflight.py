"""Deterministic offline unit tests for PreEngineExecutionGate and PreflightControlledExecutionController."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, create_autospec

import pytest
import aos.execution_preflight as ep_mod
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
    def __init__(self, disposition: str = "VERIFIED_CANDIDATE", raises: bool = False, custom_res: Any = None):
        self.call_count = 0
        self.last_kwargs: Optional[Dict[str, Any]] = None
        self.disposition = disposition
        self.raises = raises
        self.custom_res = custom_res

    def execute(self, local_target_repo_path: Optional[str] = None) -> Any:
        self.call_count += 1
        self.last_kwargs = {"local_target_repo_path": local_target_repo_path}
        if self.raises:
            raise RuntimeError("Synthetic engine failure")
        if self.custom_res is not None:
            return self.custom_res
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
    )


def make_gate(env: Dict[str, Any], **kwargs) -> PreEngineExecutionGate:
    defaults = {
        "cli_version_resolver": lambda p: "1.1.20",
        "capability_resolver": lambda p, a, i: "PROVEN",
        "git_runner": make_fake_git_runner(),
        "capability_store_path_resolver": lambda: env["cap_store"],
    }
    defaults.update(kwargs)
    return PreEngineExecutionGate(**defaults)


# ==========================================
# BASE R1 REQUIRED TESTS (Exact names preserved)
# ==========================================

def test_real_controlled_execution_engine_autospec_signature(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)
    gate = make_gate(env)

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


def test_exact_engine_result_preserved(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)
    gate = make_gate(env)

    engine = FakeEngine(disposition="VERIFICATION_FAILED")
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "PASS"
    assert res["engine_invoked"] is True
    assert res["disposition"] == "VERIFICATION_FAILED"
    assert res["status"] == "VERIFICATION_FAILED"
    assert res["engine_result"] == {"status": "VERIFICATION_FAILED", "disposition": "VERIFICATION_FAILED"}


def test_one_shot_enforced_after_preflight_hold(tmp_path):
    env = make_fixture_env(tmp_path)
    env["cli_file"].write_bytes(b"tampered content")
    req = make_request(env)

    version_calls = []
    cap_calls = []

    gate = make_gate(
        env,
        cli_version_resolver=lambda p: version_calls.append(1) or "1.1.20",
        capability_resolver=lambda p, a, i: cap_calls.append(1) or "PROVEN",
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


def test_default_capability_resolver_reuses_identity(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    version_calls = []
    cap_identity_passed = []

    gate = make_gate(
        env,
        cli_version_resolver=lambda p: version_calls.append(1) or "1.1.20",
        capability_resolver=lambda p, a, i: cap_identity_passed.append(i) or "PROVEN",
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "PASS"
    assert len(version_calls) == 1
    assert len(cap_identity_passed) == 1
    assert cap_identity_passed[0]["version"] == "1.1.20"
    assert cap_identity_passed[0]["sha256"] == env["cli_hash"]


def test_relative_cli_path_fails(tmp_path):
    env = make_fixture_env(tmp_path)
    req = PreEngineExecutionRequest(
        local_target_repo_path=env["repo_dir"],
        expected_control_branch="feature/aos-4-independent-verification-hold",
        authorization_artifact_path=env["auth_file"],
        canonical_state_path=env["state_file"],
        attempt_number=6,
        cli_command="agy",  # Relative path!
        raw_result_path=env["raw_result"],
    )

    version_calls = []
    gate = make_gate(env, cli_version_resolver=lambda p: version_calls.append(1) or "1.1.20")
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False
    assert engine.call_count == 0
    assert len(version_calls) == 0


def test_external_auth_artifact_holds(tmp_path):
    env = make_fixture_env(tmp_path)

    external_auth = tmp_path / "external_auth.json"
    external_auth.write_text(env["auth_file"].read_text())

    req = PreEngineExecutionRequest(
        local_target_repo_path=env["repo_dir"],
        expected_control_branch="feature/aos-4-independent-verification-hold",
        authorization_artifact_path=external_auth,  # Outside target repo!
        canonical_state_path=env["state_file"],
        attempt_number=6,
        cli_command=env["cli_file"],
        raw_result_path=env["raw_result"],
    )

    gate = make_gate(env)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_external_canonical_state_holds(tmp_path):
    env = make_fixture_env(tmp_path)

    external_state = tmp_path / "external_state.json"
    external_state.write_text(env["state_file"].read_text())

    req = PreEngineExecutionRequest(
        local_target_repo_path=env["repo_dir"],
        expected_control_branch="feature/aos-4-independent-verification-hold",
        authorization_artifact_path=env["auth_file"],
        canonical_state_path=external_state,  # Outside target repo!
        attempt_number=6,
        cli_command=env["cli_file"],
        raw_result_path=env["raw_result"],
    )

    gate = make_gate(env)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_carrier_ls_tree_failure_holds(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env, git_runner=make_fake_git_runner(carrier_ls_tree_fails=True))
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_parent_ls_tree_failure_holds(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env, git_runner=make_fake_git_runner(parent_ls_tree_fails=True))
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_canonical_state_absent_from_head_holds(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env, git_runner=make_fake_git_runner(state_carrier_present=False))
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_cli_hash_mismatch_blocks_version_and_engine(tmp_path):
    env = make_fixture_env(tmp_path)
    env["cli_file"].write_bytes(b"tampered content")
    req = make_request(env)

    version_calls = []
    gate = make_gate(env, cli_version_resolver=lambda p: version_calls.append(1) or "1.1.20")
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False
    assert engine.call_count == 0
    assert len(version_calls) == 0


def test_cli_version_mismatch_after_hash_match(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    version_calls = []
    cap_calls = []
    gate = make_gate(
        env,
        cli_version_resolver=lambda p: version_calls.append(1) or "1.1.21",
        capability_resolver=lambda p, a, i: cap_calls.append(1) or "PROVEN",
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False
    assert engine.call_count == 0
    assert len(version_calls) == 1
    assert len(cap_calls) == 0


def test_capability_unproven_blocks_engine(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env, capability_resolver=lambda p, a, i: "UNPROVEN")
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False
    assert engine.call_count == 0


def test_engine_exception_fails_closed(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env)
    engine = FakeEngine(raises=True)
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "PASS"
    assert res["engine_invoked"] is True
    assert res["disposition"] == "HOLD"
    assert res["status"] == "HOLD"
    assert "Synthetic engine failure" in res["errors"][0]


# ==========================================
# RESTORED STAGE 10T SCENARIO TESTS
# ==========================================

def test_valid_preflight_pass(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "PASS"
    assert res["engine_invoked"] is True
    assert engine.call_count == 1


def test_malformed_capability_attestation(tmp_path):
    env = make_fixture_env(tmp_path)
    env["cap_store"].write_text(json.dumps({"invalid": "schema"}), encoding="utf-8")
    req = make_request(env)

    gate = make_gate(env)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_malformed_authorization_artifact(tmp_path):
    env = make_fixture_env(tmp_path)
    env["auth_file"].write_text(json.dumps({"schema_version": "0.1.0"}), encoding="utf-8")
    req = make_request(env)

    gate = make_gate(env)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_authorization_decision_human_required(tmp_path):
    env = make_fixture_env(tmp_path)
    auth = env["auth_data"]
    auth["decision"] = "HUMAN_REQUIRED"
    env["auth_file"].write_text(json.dumps(auth), encoding="utf-8")
    req = make_request(env)

    gate = make_gate(env)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_authorization_already_consumed_in_state(tmp_path):
    env = make_fixture_env(tmp_path)
    st = env["state_data"]
    st["extensions"]["aos4_independent_verification"]["attempt_6_authorization_consumed"] = True
    env["state_file"].write_text(json.dumps(st), encoding="utf-8")
    req = make_request(env)

    gate = make_gate(env)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_authorization_status_not_issued(tmp_path):
    env = make_fixture_env(tmp_path)
    st = env["state_data"]
    st["extensions"]["aos4_independent_verification"]["attempt_6_authorization_status"] = "CONSUMED"
    env["state_file"].write_text(json.dumps(st), encoding="utf-8")
    req = make_request(env)

    gate = make_gate(env)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_authorization_id_mismatch(tmp_path):
    env = make_fixture_env(tmp_path)
    st = env["state_data"]
    st["extensions"]["aos4_independent_verification"]["attempt_6_authorization_id"] = "WRONG-ID"
    env["state_file"].write_text(json.dumps(st), encoding="utf-8")
    req = make_request(env)

    gate = make_gate(env)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_state_execution_actual_greater_than_zero(tmp_path):
    env = make_fixture_env(tmp_path)
    st = env["state_data"]
    st["extensions"]["aos4_independent_verification"]["attempt_6_execution_actual"] = 1
    env["state_file"].write_text(json.dumps(st), encoding="utf-8")
    req = make_request(env)

    gate = make_gate(env)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_state_terminal_true(tmp_path):
    env = make_fixture_env(tmp_path)
    st = env["state_data"]
    st["extensions"]["aos4_independent_verification"]["attempt_6_terminal"] = True
    env["state_file"].write_text(json.dumps(st), encoding="utf-8")
    req = make_request(env)

    gate = make_gate(env)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_wrong_branch(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env, git_runner=make_fake_git_runner(branch="main"))
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_head_parent_mismatch(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env, git_runner=make_fake_git_runner(parent="9999999999999999999999999999999999999999"))
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_dirty_working_tree(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env, git_runner=make_fake_git_runner(status_porcelain=" M file.py\n"))
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_remote_sha_mismatch(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env, git_runner=make_fake_git_runner(remote_head="8888888888888888888888888888888888888888"))
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_auth_artifact_absent_from_carrier(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env, git_runner=make_fake_git_runner(auth_carrier_present=False))
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_auth_artifact_already_exists_at_parent(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env, git_runner=make_fake_git_runner(auth_parent_present=True))
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_raw_result_collision(tmp_path):
    env = make_fixture_env(tmp_path)
    env["raw_result"].write_text("existing result", encoding="utf-8")
    req = make_request(env)

    gate = make_gate(env)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_git_failure(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env, git_runner=make_fake_git_runner(git_fails=True))
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_one_shot_controller_prevents_second_execution(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res1 = controller.execute(req)
    assert res1["preflight_status"] == "PASS"
    assert res1["engine_invoked"] is True
    assert engine.call_count == 1

    res2 = controller.execute(req)
    assert res2["preflight_status"] == "HOLD"
    assert res2["engine_invoked"] is False
    assert engine.call_count == 1


def test_project_agnostic_synthetic_paths(tmp_path):
    custom_repo = tmp_path / "custom_project_repo"
    custom_repo.mkdir(parents=True)

    auth_file = custom_repo / "custom_auth.json"
    state_file = custom_repo / "custom_state.json"
    cli_file = tmp_path / "custom_cli.exe"
    cli_bytes = b"custom cli binary"
    cli_file.write_bytes(cli_bytes)
    cli_hash = hashlib.sha256(cli_bytes).hexdigest()
    cap_store = tmp_path / "custom_cap.json"
    raw_result = tmp_path / "custom_raw_result.json"

    auth_data = {
        "schema_version": "0.1.0",
        "authorization_id": "CUSTOM-AUTH-999",
        "project_id": "custom_project",
        "task_id": "CUSTOM-TASK-001",
        "gate": "AOS-4",
        "risk_class": "R1",
        "decision": "AUTO_EXECUTE",
        "authority_source": "POLICY_AUTONOMOUS",
        "reason_codes": ["CUSTOM_REASON"],
        "control_source_sha": "4444444444444444444444444444444444444444",
        "execution_base_sha": "5555555555555555555555555555555555555555",
        "timestamp": "2026-08-27T20:00:00Z",
    }
    auth_file.write_text(json.dumps(auth_data), encoding="utf-8")

    state_data = {
        "schema_version": "0.1.0",
        "updated_at": "2026-08-27T20:00:00Z",
        "project": {
            "name": "CustomProject",
            "expanded_name": "Custom Operating System",
            "working_name_status": "PROVISIONAL",
            "purpose": "Custom test purpose",
        },
        "status": "CUSTOM_STATUS",
        "current_gate": "AOS-4",
        "delivery_train": ["Train 1"],
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
        "next_action": "Custom action",
        "extensions": {
            "aos4_independent_verification": {
                "next_execution_attempt_number": 3,
                "next_execution_authorization_status": "POLICY_AUTHORIZED",
                "attempt_3_authorization_id": "CUSTOM-AUTH-999",
                "attempt_3_authorization_status": "ISSUED_NOT_CONSUMED",
                "attempt_3_authorization_consumed": False,
                "attempt_3_authorization_control_source_sha": "4444444444444444444444444444444444444444",
                "attempt_3_execution_base_sha": "5555555555555555555555555555555555555555",
                "attempt_3_execution_actual": 0,
                "attempt_3_worker_execution_actual": 0,
                "attempt_3_retry_actual": 0,
                "attempt_3_terminal": False,
            }
        },
    }
    state_file.write_text(json.dumps(state_data), encoding="utf-8")

    cap_data = {
        "schema_version": "0.1.0",
        "worker_adapter": "antigravity",
        "adapter_contract_version": "0.2.9",
        "executable_filename": "custom_cli.exe",
        "executable_sha256": cli_hash,
        "reported_cli_version": "2.0.0",
        "runtime_environment_profile_version": "0.1.0",
        "runtime_environment_fingerprint_sha256": "6666666666666666666666666666666666666666666666666666666666666666",
        "capability_status": "PROVEN",
        "probe_id": "CUSTOM-PROBE",
        "probe_timestamp": "2026-08-27T20:00:00Z",
        "aos_revision_used_for_probe": "4444444444444444444444444444444444444444",
        "capabilities_proven": ["custom_cap"],
        "limitations": ["None"],
    }
    cap_store.write_text(json.dumps(cap_data), encoding="utf-8")

    req = PreEngineExecutionRequest(
        local_target_repo_path=custom_repo,
        expected_control_branch="feature/custom-branch",
        authorization_artifact_path=auth_file,
        canonical_state_path=state_file,
        attempt_number=3,
        cli_command=cli_file,
        raw_result_path=raw_result,
    )

    gate = PreEngineExecutionGate(
        cli_version_resolver=lambda p: "2.0.0",
        capability_resolver=lambda p, a, i: "PROVEN",
        git_runner=make_fake_git_runner(
            branch="feature/custom-branch",
            head="7777777777777777777777777777777777777777",
            parent="4444444444444444444444444444444444444444",
            remote_head="7777777777777777777777777777777777777777",
        ),
        capability_store_path_resolver=lambda: cap_store,
    )

    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "PASS"
    assert res["engine_invoked"] is True


def test_unknown_hash_binary_never_executed_for_version(tmp_path):
    env = make_fixture_env(tmp_path)
    env["cli_file"].write_bytes(b"completely unknown or untrusted binary bytes")
    req = make_request(env)

    version_resolver_invoked = []

    gate = make_gate(
        env,
        cli_version_resolver=lambda p: version_resolver_invoked.append(True) or "1.1.20",
    )

    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert len(version_resolver_invoked) == 0


# ==========================================
# NEW R2 SPECIFIC TEST COVERAGE
# ==========================================

def test_capability_store_path_not_in_request_dataclass():
    fields = PreEngineExecutionRequest.__dataclass_fields__
    assert "capability_store_path" not in fields


def test_production_default_capability_resolver_path_tested(tmp_path, monkeypatch):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    observed_args = {}

    def mock_resolve_capability_status(cli_command, store_path, identity=None):
        observed_args["cli_command"] = cli_command
        observed_args["store_path"] = store_path
        observed_args["identity"] = identity
        return "PROVEN"

    monkeypatch.setattr(ep_mod, "resolve_capability_status", mock_resolve_capability_status)

    version_calls = []
    gate = PreEngineExecutionGate(
        cli_version_resolver=lambda p: version_calls.append(1) or "1.1.20",
        capability_store_path_resolver=lambda: env["cap_store"],
        git_runner=make_fake_git_runner(),
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "PASS"
    assert len(version_calls) == 1
    assert observed_args["cli_command"] == str(env["cli_file"].resolve())
    assert observed_args["store_path"] == env["cap_store"]
    assert observed_args["identity"] is not None
    assert observed_args["identity"]["version"] == "1.1.20"
    assert observed_args["identity"]["sha256"] == env["cli_hash"]


def test_capability_store_symlink_holds(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    symlink_store = tmp_path / "symlink_cap.json"
    try:
        os.symlink(env["cap_store"], symlink_store)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in this environment")

    gate = make_gate(env, capability_store_path_resolver=lambda: symlink_store)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_authorization_artifact_direct_symlink_holds(tmp_path):
    env = make_fixture_env(tmp_path)
    symlink_auth = env["repo_dir"] / "docs" / "proofs" / "symlink_auth.json"
    try:
        os.symlink(env["auth_file"], symlink_auth)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in this environment")

    req = PreEngineExecutionRequest(
        local_target_repo_path=env["repo_dir"],
        expected_control_branch="feature/aos-4-independent-verification-hold",
        authorization_artifact_path=symlink_auth,
        canonical_state_path=env["state_file"],
        attempt_number=6,
        cli_command=env["cli_file"],
        raw_result_path=env["raw_result"],
    )

    gate = make_gate(env)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_canonical_state_direct_symlink_holds(tmp_path):
    env = make_fixture_env(tmp_path)
    symlink_state = env["repo_dir"] / "docs" / "project-control" / "symlink_state.json"
    try:
        os.symlink(env["state_file"], symlink_state)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in this environment")

    req = PreEngineExecutionRequest(
        local_target_repo_path=env["repo_dir"],
        expected_control_branch="feature/aos-4-independent-verification-hold",
        authorization_artifact_path=env["auth_file"],
        canonical_state_path=symlink_state,
        attempt_number=6,
        cli_command=env["cli_file"],
        raw_result_path=env["raw_result"],
    )

    gate = make_gate(env)
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


def test_malformed_engine_result_missing_disposition_holds(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env)
    engine = FakeEngine(custom_res={"status": "PASS"})  # Missing disposition!
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "PASS"
    assert res["engine_invoked"] is True
    assert res["disposition"] == "HOLD"
    assert res["status"] == "HOLD"
    assert "INVALID_ENGINE_TERMINAL_RESULT" in res["errors"][0]


def test_malformed_engine_result_unknown_disposition_holds(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env)
    engine = FakeEngine(custom_res={"disposition": "UNKNOWN_CUSTOM_DISPOSITION"})
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "PASS"
    assert res["engine_invoked"] is True
    assert res["disposition"] == "HOLD"
    assert res["status"] == "HOLD"
    assert "INVALID_ENGINE_TERMINAL_RESULT" in res["errors"][0]


def test_non_dict_engine_result_holds(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = make_gate(env)
    engine = FakeEngine(custom_res="Just a string output")
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "PASS"
    assert res["engine_invoked"] is True
    assert res["disposition"] == "HOLD"
    assert res["status"] == "HOLD"
    assert "INVALID_ENGINE_TERMINAL_RESULT" in res["errors"][0]
