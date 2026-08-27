"""Deterministic offline unit tests for PreEngineExecutionGate and PreflightControlledExecutionController."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
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
    def __init__(self):
        self.call_count = 0

    def execute(self, task_id: str, request_path: str, state_path: str) -> Dict[str, Any]:
        self.call_count += 1
        return {"status": "PASS", "disposition": "VERIFIED_CANDIDATE", "task_id": task_id}


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
        "project": {"name": "TestProject"},
        "status": "TEST_STATUS",
        "current_gate": "AOS-4",
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
    carrier_present: bool = True,
    parent_present: bool = False,
    git_fails: bool = False,
):
    remote_target = remote_head if remote_head is not None else head

    def git_runner(cmd: List[str], cwd: Path) -> FakeCompletedProcess:
        if git_fails:
            return FakeCompletedProcess(returncode=1, stderr="Git execution error simulated")

        cmd_str = " ".join(cmd)
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
        elif cmd[0] == "cat-file" and cmd[1] == "-e":
            target_ref = cmd[2]
            if target_ref.startswith("HEAD:"):
                return FakeCompletedProcess(0 if carrier_present else 1, "", "")
            elif target_ref.startswith(f"{parent}:"):
                return FakeCompletedProcess(0 if parent_present else 1, "", "")
            return FakeCompletedProcess(1, "", "")
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


# Test A: valid complete preflight = PASS, controller invokes engine exactly once
def test_valid_preflight_pass(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    version_calls = []
    cap_calls = []

    def fake_version_resolver(cli_path: Path):
        version_calls.append(cli_path)
        return "1.1.20"

    def fake_cap_resolver(cli_path: Path, attestation: Dict[str, Any]):
        cap_calls.append(cli_path)
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
    assert res["engine_invoked"] is True
    assert engine.call_count == 1
    assert len(version_calls) == 1
    assert len(cap_calls) == 1


# Test B: CLI hash mismatch = HOLD, version/cap/engine call count = 0
def test_cli_hash_mismatch_blocks_version_and_engine(tmp_path):
    env = make_fixture_env(tmp_path)
    # Mutate CLI file content so hash differs
    env["cli_file"].write_bytes(b"tampered content")
    req = make_request(env)

    version_calls = []
    cap_calls = []

    def fake_version_resolver(cli_path: Path):
        version_calls.append(cli_path)
        return "1.1.20"

    def fake_cap_resolver(cli_path: Path, attestation: Dict[str, Any]):
        cap_calls.append(cli_path)
        return "PROVEN"

    gate = PreEngineExecutionGate(
        cli_version_resolver=fake_version_resolver,
        capability_resolver=fake_cap_resolver,
        git_runner=make_fake_git_runner(),
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False
    assert engine.call_count == 0
    assert len(version_calls) == 0  # CRITICAL: Version resolver must NOT be called on hash mismatch!
    assert len(cap_calls) == 0


# Test C: CLI version mismatch after valid hash = HOLD, cap/engine call count = 0
def test_cli_version_mismatch_after_hash_match(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    version_calls = []
    cap_calls = []

    def fake_version_resolver(cli_path: Path):
        version_calls.append(cli_path)
        return "1.1.21"  # Mismatch (expected 1.1.20)

    def fake_cap_resolver(cli_path: Path, attestation: Dict[str, Any]):
        cap_calls.append(cli_path)
        return "PROVEN"

    gate = PreEngineExecutionGate(
        cli_version_resolver=fake_version_resolver,
        capability_resolver=fake_cap_resolver,
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


# Test D: capability UNPROVEN = HOLD, engine call count = 0
def test_capability_unproven_blocks_engine(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    def fake_version_resolver(cli_path: Path):
        return "1.1.20"

    def fake_cap_resolver(cli_path: Path, attestation: Dict[str, Any]):
        return "UNPROVEN"

    gate = PreEngineExecutionGate(
        cli_version_resolver=fake_version_resolver,
        capability_resolver=fake_cap_resolver,
        git_runner=make_fake_git_runner(),
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False
    assert engine.call_count == 0


# Test E: malformed capability attestation = HOLD, engine call count = 0
def test_malformed_capability_attestation(tmp_path):
    env = make_fixture_env(tmp_path)
    # Write invalid capability attestation
    env["cap_store"].write_text(json.dumps({"invalid": "schema"}), encoding="utf-8")
    req = make_request(env)

    gate = PreEngineExecutionGate(git_runner=make_fake_git_runner())
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False
    assert engine.call_count == 0


# Test F: authorization malformed/schema-invalid = HOLD
def test_malformed_authorization_artifact(tmp_path):
    env = make_fixture_env(tmp_path)
    env["auth_file"].write_text(json.dumps({"schema_version": "0.1.0"}), encoding="utf-8")
    req = make_request(env)

    gate = PreEngineExecutionGate(git_runner=make_fake_git_runner())
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test G: authorization decision HUMAN_REQUIRED = HOLD
def test_authorization_decision_human_required(tmp_path):
    env = make_fixture_env(tmp_path)
    auth = env["auth_data"]
    auth["decision"] = "HUMAN_REQUIRED"
    env["auth_file"].write_text(json.dumps(auth), encoding="utf-8")
    req = make_request(env)

    gate = PreEngineExecutionGate(git_runner=make_fake_git_runner())
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test H: authorization already consumed in canonical STATE = HOLD
def test_authorization_already_consumed_in_state(tmp_path):
    env = make_fixture_env(tmp_path)
    st = env["state_data"]
    st["extensions"]["aos4_independent_verification"]["attempt_6_authorization_consumed"] = True
    env["state_file"].write_text(json.dumps(st), encoding="utf-8")
    req = make_request(env)

    gate = PreEngineExecutionGate(git_runner=make_fake_git_runner())
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test I: authorization status not ISSUED_NOT_CONSUMED = HOLD
def test_authorization_status_not_issued(tmp_path):
    env = make_fixture_env(tmp_path)
    st = env["state_data"]
    st["extensions"]["aos4_independent_verification"]["attempt_6_authorization_status"] = "CONSUMED"
    env["state_file"].write_text(json.dumps(st), encoding="utf-8")
    req = make_request(env)

    gate = PreEngineExecutionGate(git_runner=make_fake_git_runner())
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test J: authorization ID mismatch = HOLD
def test_authorization_id_mismatch(tmp_path):
    env = make_fixture_env(tmp_path)
    st = env["state_data"]
    st["extensions"]["aos4_independent_verification"]["attempt_6_authorization_id"] = "WRONG-ID"
    env["state_file"].write_text(json.dumps(st), encoding="utf-8")
    req = make_request(env)

    gate = PreEngineExecutionGate(git_runner=make_fake_git_runner())
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test K: state execution_actual > 0 = HOLD
def test_state_execution_actual_greater_than_zero(tmp_path):
    env = make_fixture_env(tmp_path)
    st = env["state_data"]
    st["extensions"]["aos4_independent_verification"]["attempt_6_execution_actual"] = 1
    env["state_file"].write_text(json.dumps(st), encoding="utf-8")
    req = make_request(env)

    gate = PreEngineExecutionGate(git_runner=make_fake_git_runner())
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test L: state terminal true = HOLD
def test_state_terminal_true(tmp_path):
    env = make_fixture_env(tmp_path)
    st = env["state_data"]
    st["extensions"]["aos4_independent_verification"]["attempt_6_terminal"] = True
    env["state_file"].write_text(json.dumps(st), encoding="utf-8")
    req = make_request(env)

    gate = PreEngineExecutionGate(git_runner=make_fake_git_runner())
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test M: wrong branch = HOLD
def test_wrong_branch(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = PreEngineExecutionGate(
        git_runner=make_fake_git_runner(branch="main")
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test N: HEAD parent != authorization.control_source_sha = HOLD
def test_head_parent_mismatch(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = PreEngineExecutionGate(
        git_runner=make_fake_git_runner(parent="9999999999999999999999999999999999999999")
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test O: dirty working tree = HOLD
def test_dirty_working_tree(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = PreEngineExecutionGate(
        git_runner=make_fake_git_runner(status_porcelain=" M file.py\n")
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test P: remote branch SHA != local HEAD = HOLD
def test_remote_sha_mismatch(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = PreEngineExecutionGate(
        git_runner=make_fake_git_runner(remote_head="8888888888888888888888888888888888888888")
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test Q: authorization artifact absent from carrier = HOLD
def test_auth_artifact_absent_from_carrier(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = PreEngineExecutionGate(
        git_runner=make_fake_git_runner(carrier_present=False)
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test R: authorization artifact already exists at parent = HOLD
def test_auth_artifact_already_exists_at_parent(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = PreEngineExecutionGate(
        git_runner=make_fake_git_runner(parent_present=True)
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test S: raw result path already exists = HOLD
def test_raw_result_collision(tmp_path):
    env = make_fixture_env(tmp_path)
    env["raw_result"].write_text("existing result", encoding="utf-8")
    req = make_request(env)

    gate = PreEngineExecutionGate(git_runner=make_fake_git_runner())
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test T: Git inspection/read failure = HOLD
def test_git_failure(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    gate = PreEngineExecutionGate(git_runner=make_fake_git_runner(git_fails=True))
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert res["engine_invoked"] is False


# Test U: controller second execution attempt = HOLD / rejected, total calls remain 1
def test_one_shot_controller_prevents_second_execution(tmp_path):
    env = make_fixture_env(tmp_path)
    req = make_request(env)

    def fake_version_resolver(cli_path: Path):
        return "1.1.20"

    def fake_cap_resolver(cli_path: Path, attestation: Dict[str, Any]):
        return "PROVEN"

    gate = PreEngineExecutionGate(
        cli_version_resolver=fake_version_resolver,
        capability_resolver=fake_cap_resolver,
        git_runner=make_fake_git_runner(),
    )
    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res1 = controller.execute(req)
    assert res1["preflight_status"] == "PASS"
    assert res1["engine_invoked"] is True
    assert engine.call_count == 1

    res2 = controller.execute(req)
    assert res2["preflight_status"] == "HOLD"
    assert res2["engine_invoked"] is False
    assert engine.call_count == 1  # Total engine calls MUST remain 1!


# Test V: no hard-coded project/user path assumptions in core gate
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
        "gate": "CUSTOM-GATE",
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
        "project": {"name": "CustomProject"},
        "status": "CUSTOM_STATUS",
        "current_gate": "CUSTOM-GATE",
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
        capability_store_path=cap_store,
    )

    gate = PreEngineExecutionGate(
        cli_version_resolver=lambda p: "2.0.0",
        capability_resolver=lambda p, att: "PROVEN",
        git_runner=make_fake_git_runner(
            branch="feature/custom-branch",
            head="7777777777777777777777777777777777777777",
            parent="4444444444444444444444444444444444444444",
            remote_head="7777777777777777777777777777777777777777",
        ),
    )

    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "PASS"
    assert res["engine_invoked"] is True


# Test W: fail ordering proves unknown hash binary is not executed even for version inspection
def test_unknown_hash_binary_never_executed_for_version(tmp_path):
    env = make_fixture_env(tmp_path)
    env["cli_file"].write_bytes(b"completely unknown or untrusted binary bytes")
    req = make_request(env)

    version_resolver_invoked = []

    def tracking_version_resolver(cli_path: Path):
        version_resolver_invoked.append(True)
        return "1.1.20"

    gate = PreEngineExecutionGate(
        cli_version_resolver=tracking_version_resolver,
        capability_resolver=lambda p, a: "PROVEN",
        git_runner=make_fake_git_runner(),
    )

    engine = FakeEngine()
    controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    res = controller.execute(req)
    assert res["preflight_status"] == "HOLD"
    assert len(version_resolver_invoked) == 0  # PROOF: Version resolver NEVER invoked for untrusted hash!
