"""Offline tests for Antigravity machine-local capability attestation and probe."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from aos.controlled_execution import ControlledExecutionEngine
from aos.git_workspace import GitWorkspace
from aos.source_adapter import ProjectSourceAdapter
from aos.validate import validate_document
from aos.workers.antigravity import (
    ADAPTER_CONTRACT_VERSION,
    AntigravityWorkerAdapter,
    build_antigravity_argv,
    compute_file_sha256,
    get_local_capability_store_path,
    get_reported_cli_version,
    parse_antigravity_json_output,
    parse_antigravity_stream_output,
    resolve_capability_status,
    resolve_executable_identity,
    sanitize_tool_error,
)
from aos.workers.antigravity_probe import (
    run_antigravity_probe,
    write_local_capability_attestation,
)
from aos.workers.base import WorkerAdapter


@pytest.fixture
def temp_capability_env():
    temp_dir = tempfile.mkdtemp(prefix="aos_cap_test_")
    store_file = Path(temp_dir) / "antigravity.json"
    fake_exe = Path(temp_dir) / "fake_bin" / "agy.exe"
    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    fake_exe.write_text("fake binary content", encoding="utf-8")
    yield temp_dir, store_file, str(fake_exe)
    shutil.rmtree(temp_dir, ignore_errors=True)


class MockSourceAdapter(ProjectSourceAdapter):
    def __init__(self, repo: str, ref: str):
        super().__init__(repo, ref)

    def resolve_ref_to_sha(self) -> str:
        return "4c55eecdbe064c74b34af31a1daf9851689e4fe8"

    def fetch_canonical_context(self, exact_sha: str, paths: Dict[str, str]):
        contents = {
            "state": json.dumps({
                "schema_version": "0.1.0",
                "current_status": "READY",
                "current_milestone": "M1",
                "next_action": "Do task",
                "next_action_execution_base_sha": "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
            }),
            "decisions": "# Decisions",
            "evidence": "",
            "roadmap": "# Roadmap",
        }
        return contents, {}

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
            "next_action_execution_base_sha": "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
            "has_ambiguity": False,
            "ambiguity_reasons": [],
            "input_file_hashes": file_hashes,
        }


class MockGitWorkspace(GitWorkspace):
    def __init__(self, repo, base_sha, task_id, branch_name=None):
        super().__init__(repo, base_sha, task_id, branch_name)

    def setup(self) -> str:
        self.workspace_dir = "/tmp/mock_cap_ws"
        self.initial_head_sha = self.base_sha
        return self.workspace_dir

    def get_current_head(self) -> str:
        return self.base_sha

    def get_current_branch(self) -> str:
        return f"aos/{self.task_id.lower()}"

    def get_changed_files(self, from_sha=None):
        return []

    def cleanup(self) -> None:
        pass


def make_generic_descriptor(project_id="generic_project", repo="GenericOrg/GenericRepo"):
    return {
        "schema_version": "0.1.0",
        "project_id": project_id,
        "repository": repo,
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
        "verification": {
            "checks": {
                "unit_tests": {
                    "argv": ["python", "-m", "pytest", "-q"],
                    "timeout_seconds": 300,
                }
            }
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
    required_checks=None,
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
            "forbidden_paths": forbidden_paths or [],
        },
        "worker_requirements": {
            "adapter": adapter,
            "isolated_worktree": isolated_worktree,
        },
        "evidence_requirements": {
            "minimum_level": "E3_ISOLATED_RUNTIME_PROVEN",
            "required_checks": required_checks if required_checks is not None else ["unit_tests"],
        },
        "retry_policy": {
            "max_retries": max_retries,
        },
    }


def make_valid_attestation(
    exe_sha=None,
    cli_ver=None,
    contract_ver=ADAPTER_CONTRACT_VERSION,
    status="PROVEN",
    exe_name=None,
):
    return {
        "schema_version": "0.1.0",
        "worker_adapter": "antigravity",
        "adapter_contract_version": contract_ver,
        "executable_filename": exe_name or "agy.exe",
        "executable_sha256": exe_sha or "550863e77436c18d4b2e3a60cbf6e33b39c33dbf68058294dd6e34a878c9ccaf",
        "reported_cli_version": cli_ver or "1.1.17",
        "capability_status": status,
        "probe_id": "PROBE-20260821-123456-abcdef12",
        "probe_timestamp": "2026-08-21T19:00:00Z",
        "aos_revision_used_for_probe": "fce499bb3c1449a0b2048d7be779116da615f698",
        "capabilities_proven": [
            "non_interactive_instruction",
            "workspace_execution",
            "observable_exit_status",
            "stream_json_observability",
            "controlled_file_edit",
            "no_commit_observed",
            "no_push_observed",
        ],
        "limitations": [
            "This proves behavioral non-interactive execution in the disposable probe environment. It does not prove OS-level sandbox containment."
        ],
    }


def make_valid_stream_ndjson(
    include_write_tool=True,
    status="SUCCESS",
    tool_error=False,
    soft_denial=False,
    raw_message=True,
    cwd="/tmp/ws",
    active_step=False,
    tool_name="write_file",
):
    """Build NDJSON matching the official Antigravity CLI nested protocol.

    Official step_update states are strictly: ACTIVE and DONE.
    """
    events = [
        {"event": "init", "init": {"permission_mode": "ask", "cwd": cwd, "tools": [tool_name, "run_command"]}},
    ]
    if raw_message:
        events.append({"event": "step_update", "step_update": {"step_type": "agent_response", "state": "DONE"}})
    if include_write_tool:
        tool_info: dict = {"name": tool_name}
        if soft_denial:
            tool_info["error"] = {"type": "PERMISSION_DENIED", "message": "Permission denied: user prompt not answered in headless mode"}
        elif tool_error:
            tool_info["error"] = {"type": "TOOL_ERROR", "message": "Disk I/O error"}

        if active_step:
            events.append({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": tool_name, "state": "ACTIVE", "tool_info": tool_info}})
        events.append({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": tool_name, "state": "DONE", "tool_info": tool_info}})
    events.append({"event": "result", "result": {"status": status}})
    return "\n".join(json.dumps(e) for e in events)


class TestAntigravityCapabilityResolution:
    def test_no_attestation_resolves_unproven(self, temp_capability_env):
        """1. Missing attestation file resolves to UNPROVEN."""
        _, store_file, fake_exe = temp_capability_env
        assert not store_file.exists()
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }
        status = resolve_capability_status(fake_exe, store_path=store_file, identity=fake_id)
        assert status == "UNPROVEN"

    def test_malformed_attestation_resolves_unproven(self, temp_capability_env):
        """2. Malformed or invalid JSON attestation resolves to UNPROVEN."""
        _, store_file, fake_exe = temp_capability_env
        store_file.write_text('{"invalid_schema": true}', encoding="utf-8")
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }
        status = resolve_capability_status(fake_exe, store_path=store_file, identity=fake_id)
        assert status == "UNPROVEN"

    def test_wrong_executable_hash_resolves_unproven(self, temp_capability_env):
        """3. Attestation with mismatched executable SHA256 resolves to UNPROVEN."""
        _, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }
        att = make_valid_attestation(exe_sha="0000000000000000000000000000000000000000000000000000000000000000", cli_ver="1.1.17")
        write_local_capability_attestation(att, store_path=store_file)
        status = resolve_capability_status(fake_exe, store_path=store_file, identity=fake_id)
        assert status == "UNPROVEN"

    def test_wrong_cli_version_resolves_unproven(self, temp_capability_env):
        """4. Attestation with mismatched reported CLI version resolves to UNPROVEN."""
        _, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }
        att = make_valid_attestation(exe_sha=fake_id["sha256"], cli_ver="9.9.9")
        write_local_capability_attestation(att, store_path=store_file)
        status = resolve_capability_status(fake_exe, store_path=store_file, identity=fake_id)
        assert status == "UNPROVEN"

    def test_wrong_adapter_contract_version_resolves_unproven(self, temp_capability_env):
        """5. Attestation with wrong adapter contract version resolves to UNPROVEN."""
        _, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }
        att = make_valid_attestation(exe_sha=fake_id["sha256"], cli_ver="1.1.17", contract_ver="0.2.0")
        write_local_capability_attestation(att, store_path=store_file)
        status = resolve_capability_status(fake_exe, store_path=store_file, identity=fake_id)
        assert status == "UNPROVEN"

    def test_old_v022_attestation_resolves_unproven(self, temp_capability_env):
        """5b. Old 0.2.2 attestation resolves to UNPROVEN under contract 0.2.4."""
        _, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }
        att = make_valid_attestation(exe_sha=fake_id["sha256"], cli_ver="1.1.17", contract_ver="0.2.2")
        write_local_capability_attestation(att, store_path=store_file)
        status = resolve_capability_status(fake_exe, store_path=store_file, identity=fake_id)
        assert status == "UNPROVEN"

    def test_old_v023_attestation_resolves_unproven(self, temp_capability_env):
        """5c. Old 0.2.3 attestation resolves to UNPROVEN under contract 0.2.5."""
        _, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }
        att = make_valid_attestation(exe_sha=fake_id["sha256"], cli_ver="1.1.17", contract_ver="0.2.3")
        write_local_capability_attestation(att, store_path=store_file)
        status = resolve_capability_status(fake_exe, store_path=store_file, identity=fake_id)
        assert status == "UNPROVEN"

    def test_old_v024_attestation_resolves_unproven(self, temp_capability_env):
        """5d. Old 0.2.4 attestation resolves to UNPROVEN under contract 0.2.5."""
        _, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }
        att = make_valid_attestation(exe_sha=fake_id["sha256"], cli_ver="1.1.17", contract_ver="0.2.4")
        write_local_capability_attestation(att, store_path=store_file)
        status = resolve_capability_status(fake_exe, store_path=store_file, identity=fake_id)
        assert status == "UNPROVEN"

    def test_matching_attestation_resolves_proven(self, temp_capability_env):
        """6. Attestation matching exact executable SHA256 and reported version resolves to PROVEN."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }
        att = make_valid_attestation(
            exe_sha=fake_id["sha256"],
            cli_ver=fake_id["version"],
            contract_ver=ADAPTER_CONTRACT_VERSION,
            exe_name=fake_id["filename"],
        )
        write_local_capability_attestation(att, store_path=store_file)

        status = resolve_capability_status(fake_exe, store_path=store_file, identity=fake_id)
        assert status == "PROVEN"

        adapter = AntigravityWorkerAdapter(
            cli_command=fake_exe,
            store_path=store_file,
            injected_identity=fake_id,
        )
        assert adapter.capability_status == "PROVEN"
        assert adapter.pinned_identity == fake_id

    def test_arbitrary_proven_override_rejected(self):
        """7a. Passing PROVEN as capability_status_override is strictly rejected."""
        with pytest.raises(ValueError, match="Invalid capability_status_override"):
            AntigravityWorkerAdapter(capability_status_override="PROVEN")

    def test_arbitrary_unproven_override_rejected(self):
        """7b. Passing UNPROVEN as capability_status_override is strictly rejected."""
        with pytest.raises(ValueError, match="Invalid capability_status_override"):
            AntigravityWorkerAdapter(capability_status_override="UNPROVEN")

    def test_test_double_override_accepted(self):
        """7c. TEST_DOUBLE is the only accepted capability_status_override."""
        adapter = AntigravityWorkerAdapter(capability_status_override="TEST_DOUBLE")
        assert adapter.capability_status == "TEST_DOUBLE"

    def test_attestation_schema_validation(self):
        """8. Attestation document conforms strictly to worker_capability_attestation schema."""
        att = make_valid_attestation()
        res = validate_document("worker_capability_attestation", att)
        assert res.is_valid is True

    def test_mock_probe_execution_pass(self, temp_capability_env):
        """9. Mocked probe execution creates result file, verifies git invariants, and writes attestation."""
        temp_dir, store_file, fake_exe = temp_capability_env
        parent_dir = Path(temp_dir) / "probe_parent"
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        def _mock_coding_runner(cmd, cwd, timeout, env):
            # Verify probe/ directory pre-exists before worker executes
            probe_dir = Path(cwd) / "probe"
            assert probe_dir.is_dir()

            # Verify exact command construction
            assert cmd[0] == fake_exe
            assert "--mode=accept-edits" in cmd
            assert "--output-format=stream-json" in cmd
            assert "--dangerously-skip-permissions" not in cmd
            assert "--print" not in cmd
            assert "-p" in cmd
            p_idx = cmd.index("-p")
            prompt_str = cmd[p_idx + 1]

            challenge_line = [l for l in prompt_str.splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            stream_out = make_valid_stream_ndjson(include_write_tool=True, status="SUCCESS", cwd=cwd)
            return subprocess.CompletedProcess(cmd, 0, stdout=stream_out, stderr="")

        res = run_antigravity_probe(
            cli_command=fake_exe,
            runner=_mock_coding_runner,
            store_path=store_file,
            custom_parent_dir=str(parent_dir),
            aos_revision="fce499bb3c1449a0b2048d7be779116da615f698",
            injected_identity=fake_id,
        )

        assert res["status"] == "PASS"
        assert res["attestation"] is not None
        assert res["attestation"]["adapter_contract_version"] == "0.2.5"
        assert res["proof"]["result"] == "PASS"
        assert res["proof"]["changed_paths"] == ["probe/result.txt"]
        assert res["proof"]["output_format"] == "stream-json"
        assert res["proof"]["stream_valid"] is True
        assert res["proof"]["write_tool_advertised"] is True
        assert res["proof"]["write_tool_available"] is True
        assert res["proof"]["completed_write_tool_observed"] is True
        assert res["proof"]["failed_native_write_tool_observed"] is False
        assert res["proof"]["reported_cwd_matches_workspace"] is True
        assert res["proof"]["failed_step_observed"] is False
        assert res["proof"]["failed_tool_observed"] is False
        assert res["proof"]["tool_call_count"] == 1
        assert store_file.is_file()

        adapter = AntigravityWorkerAdapter(
            cli_command=fake_exe,
            store_path=store_file,
            injected_identity=fake_id,
        )
        assert adapter.capability_status == "PROVEN"

    def test_mock_probe_failure_never_writes_attestation(self, temp_capability_env):
        """10. Failed probe (nonzero exit / missing file) never writes attestation."""
        temp_dir, store_file, fake_exe = temp_capability_env
        parent_dir = Path(temp_dir) / "probe_parent_fail"
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        def _mock_failing_runner(cmd, cwd, timeout, env):
            return subprocess.CompletedProcess(cmd, 1, stdout=make_valid_stream_ndjson(status="ERROR"), stderr="Error")

        res = run_antigravity_probe(
            cli_command=fake_exe,
            runner=_mock_failing_runner,
            store_path=store_file,
            custom_parent_dir=str(parent_dir),
            aos_revision="fce499bb3c1449a0b2048d7be779116da615f698",
            injected_identity=fake_id,
        )

        assert res["status"] == "HOLD"
        assert res["attestation"] is None
        assert not store_file.exists()

    def test_probe_scrubs_sensitive_environment_variables(self, temp_capability_env):
        """11. Probe runner receives an environment stripped of sensitive tokens."""
        temp_dir, store_file, fake_exe = temp_capability_env
        parent_dir = Path(temp_dir) / "probe_parent_env"
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        captured_env: Dict[str, str] = {}

        def _env_checking_runner(cmd, cwd, timeout, env):
            nonlocal captured_env
            captured_env = dict(env)
            p_idx = cmd.index("-p")
            prompt_str = cmd[p_idx + 1]
            challenge_line = [l for l in prompt_str.splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout=make_valid_stream_ndjson(cwd=cwd), stderr="")

        os.environ["OPENAI_API_KEY"] = "sk-fake-openai"
        os.environ["GEMINI_API_KEY"] = "fake-gemini"
        os.environ["GROQ_API_KEY"] = "fake-groq"
        os.environ["GH_TOKEN"] = "ghp-fake-token"

        try:
            res = run_antigravity_probe(
                cli_command=fake_exe,
                runner=_env_checking_runner,
                store_path=store_file,
                custom_parent_dir=str(parent_dir),
                aos_revision="fce499bb3c1449a0b2048d7be779116da615f698",
                injected_identity=fake_id,
            )
            assert res["status"] == "PASS"
            assert "OPENAI_API_KEY" not in captured_env
            assert "GEMINI_API_KEY" not in captured_env
            assert "GROQ_API_KEY" not in captured_env
            assert "GH_TOKEN" not in captured_env
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("GROQ_API_KEY", None)
            os.environ.pop("GH_TOKEN", None)

    def test_unproven_capability_engine_holds_zero_worker_calls(self, temp_capability_env):
        """12. ControlledExecutionEngine holds with 0 worker executions when adapter capability is UNPROVEN."""
        _, store_file, fake_exe = temp_capability_env
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"])

        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }
        adapter = AntigravityWorkerAdapter(
            cli_command=fake_exe,
            store_path=store_file,
            injected_identity=fake_id,
        )
        assert adapter.capability_status == "UNPROVEN"

        engine = ControlledExecutionEngine(
            desc, task,
            source_adapter_factory=lambda r, ref: mock_source,
            git_workspace_factory=lambda repo, base, tid, branch: mock_ws,
            worker_adapter_factory=lambda: adapter,
            repo_identity_inspector=lambda path: "GenericOrg/GenericRepo",
        )

        res = engine.execute(local_target_repo_path="/path/to/repo")
        assert res["disposition"] == "HOLD"
        assert any("UNPROVEN" in e for e in res["errors"])


class TestAntigravityExactContentAndStreamObservability:
    def test_exact_challenge_semantics_variations(self, temp_capability_env):
        """Exact UTF-8 challenge content passes; whitespace or newlines fail."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        # 1. Challenge + \n fails
        def _runner_trailing_newline(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            prompt_str = cmd[p_idx + 1]
            challenge_line = [l for l in prompt_str.splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line + "\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout=make_valid_stream_ndjson(cwd=cwd), stderr="")

        res1 = run_antigravity_probe(fake_exe, runner=_runner_trailing_newline, store_path=store_file, injected_identity=fake_id)
        assert res1["status"] == "HOLD"
        assert any("exact UTF-8 required" in e for e in res1["errors"])

        # 2. Leading space fails
        def _runner_leading_space(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            prompt_str = cmd[p_idx + 1]
            challenge_line = [l for l in prompt_str.splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(" " + challenge_line, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout=make_valid_stream_ndjson(cwd=cwd), stderr="")

        res2 = run_antigravity_probe(fake_exe, runner=_runner_leading_space, store_path=store_file, injected_identity=fake_id)
        assert res2["status"] == "HOLD"
        assert any("exact UTF-8 required" in e for e in res2["errors"])

        # 3. Trailing space fails
        def _runner_trailing_space(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            prompt_str = cmd[p_idx + 1]
            challenge_line = [l for l in prompt_str.splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line + " ", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout=make_valid_stream_ndjson(cwd=cwd), stderr="")

        res3 = run_antigravity_probe(fake_exe, runner=_runner_trailing_space, store_path=store_file, injected_identity=fake_id)
        assert res3["status"] == "HOLD"
        assert any("exact UTF-8 required" in e for e in res3["errors"])

    def test_stream_parser_fail_closed_rules(self):
        """Sanitized stream parser enforces strict event count and structure (nested protocol)."""
        # 1. Missing init
        stream_no_init = json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
        p1 = parse_antigravity_stream_output(stream_no_init)
        assert p1["is_valid_stream"] is False
        assert any("Expected exactly 1 init event" in e for e in p1["parser_errors"])

        # 2. Missing terminal result
        stream_no_result = json.dumps({"event": "init", "init": {"permission_mode": "ask"}})
        p2 = parse_antigravity_stream_output(stream_no_result)
        assert p2["is_valid_stream"] is False
        assert any("Expected exactly 1 terminal result" in e for e in p2["parser_errors"])

        # 3. Duplicate terminal results
        stream_dup_result = (
            json.dumps({"event": "init", "init": {"permission_mode": "ask"}})
            + "\n" + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
            + "\n" + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
        )
        p3 = parse_antigravity_stream_output(stream_dup_result)
        assert p3["is_valid_stream"] is False
        assert any("Expected exactly 1 terminal result" in e for e in p3["parser_errors"])

        # 4. Malformed JSON line
        stream_malformed = json.dumps({"event": "init", "init": {}}) + "\n{not json\n" + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
        p4 = parse_antigravity_stream_output(stream_malformed)
        assert p4["is_valid_stream"] is False
        assert any("Malformed JSON" in e for e in p4["parser_errors"])

        # 5. Non-success terminal status (stream is structurally valid, but terminal_error_present is true)
        stream_error = json.dumps({"event": "init", "init": {}}) + "\n" + json.dumps({"event": "result", "result": {"status": "ERROR"}})
        p5 = parse_antigravity_stream_output(stream_error)
        assert p5["is_valid_stream"] is True
        assert p5["terminal_status"] == "ERROR"
        assert p5["terminal_error_present"] is True

        # 6. Old flat schema rejected
        stream_flat = json.dumps({"type": "init", "permission_mode": "ask"})
        p6 = parse_antigravity_stream_output(stream_flat)
        assert p6["is_valid_stream"] is False
        assert any("deprecated 'type' discriminator" in e for e in p6["parser_errors"])

        # 7. Missing nested payload rejected
        stream_no_payload = json.dumps({"event": "init"})
        p7 = parse_antigravity_stream_output(stream_no_payload)
        assert p7["is_valid_stream"] is False
        assert any("missing nested 'init' payload" in e for e in p7["parser_errors"])

        # 8. Unknown event type rejected
        stream_unknown = (
            json.dumps({"event": "init", "init": {"permission_mode": "ask"}})
            + "\n" + json.dumps({"event": "custom_unknown_event", "custom_unknown_event": {}})
        )
        p8 = parse_antigravity_stream_output(stream_unknown)
        assert p8["is_valid_stream"] is False
        assert any("Unknown event type" in e for e in p8["parser_errors"])

        # 9. Invalid / unknown step_update state rejected
        stream_bad_state = (
            json.dumps({"event": "init", "init": {"permission_mode": "ask"}})
            + "\n" + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_file", "state": "completed"}})
            + "\n" + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
        )
        p9 = parse_antigravity_stream_output(stream_bad_state)
        assert p9["is_valid_stream"] is False
        assert any("invalid state" in e for e in p9["parser_errors"])

        # 10. Non-string tool_name rejected
        stream_bad_tool_name = (
            json.dumps({"event": "init", "init": {"permission_mode": "ask"}})
            + "\n" + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": 12345, "state": "DONE"}})
            + "\n" + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
        )
        p10 = parse_antigravity_stream_output(stream_bad_tool_name)
        assert p10["is_valid_stream"] is False
        assert any("missing valid string tool_name" in e for e in p10["parser_errors"])

    def test_sanitized_stream_parser_no_leakage(self):
        """Stream parser does not leak raw tool parameters, paths, or message transcripts (nested protocol)."""
        raw_stream = (
            json.dumps({"event": "init", "init": {"permission_mode": "ask", "cwd": "/secret/path/to/ws", "tools": ["write_file"]}}) + "\n"
            + json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response", "state": "DONE", "text_delta": "Sensitive agent reasoning with token sk-12345"}}) + "\n"
            + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_file", "state": "DONE", "tool_info": {"args": {"path": "/secret/probe/result.txt", "content": "secret"}}}}) + "\n"
            + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
        )
        parsed = parse_antigravity_stream_output(raw_stream, workspace_path="/secret/path/to/ws")
        assert parsed["is_valid_stream"] is True
        assert parsed["agent_response_observed"] is True
        assert parsed["reported_cwd_matches_workspace"] is True
        assert parsed["write_tool_advertised"] is True
        assert parsed["completed_write_tool_observed"] is True
        assert parsed["tool_call_count"] == 1

        # Verify tool call fields are sanitized
        tc = parsed["tool_calls"][0]
        assert set(tc.keys()) == {"tool_name", "state", "error_present", "error_type"}
        assert tc["tool_name"] == "write_file"
        assert tc["state"] == "DONE"
        assert "args" not in tc
        assert "content" not in str(parsed)
        assert "sk-12345" not in str(parsed)
        assert "text_delta" not in str(parsed)

    def test_official_done_write_event_passes(self, temp_capability_env):
        """Official DONE write event + exact file creation passes capability probe."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        def _runner(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            challenge_line = [l for l in cmd[p_idx + 1].splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            stream = make_valid_stream_ndjson(include_write_tool=True, status="SUCCESS", cwd=cwd)
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "PASS"
        assert res["proof"]["completed_write_tool_observed"] is True
        assert res["proof"]["write_tool_advertised"] is True

    def test_active_only_write_event_fails(self, temp_capability_env):
        """ACTIVE write event without DONE fails probe (state=ACTIVE is not completed)."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        def _runner_active_only(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            challenge_line = [l for l in cmd[p_idx + 1].splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            # Stream with ACTIVE write tool step only (no DONE step)
            stream = (
                json.dumps({"event": "init", "init": {"cwd": cwd, "tools": ["write_file"]}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_file", "state": "ACTIVE", "tool_info": {}}}) + "\n"
                + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner_active_only, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["completed_write_tool_observed"] is False
        assert any("No completed file-write tool execution event" in e for e in res["errors"])

    def test_active_followed_by_done_write_event_passes(self, temp_capability_env):
        """ACTIVE followed by DONE write event passes capability probe."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        def _runner(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            challenge_line = [l for l in cmd[p_idx + 1].splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            stream = make_valid_stream_ndjson(include_write_tool=True, status="SUCCESS", cwd=cwd, active_step=True)
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "PASS"
        assert res["proof"]["completed_write_tool_observed"] is True

    def test_done_write_event_with_tool_error_fails(self, temp_capability_env):
        """DONE write event with tool_info.error fails capability probe."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        def _runner_tool_err(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            challenge_line = [l for l in cmd[p_idx + 1].splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            stream = make_valid_stream_ndjson(include_write_tool=True, status="SUCCESS", cwd=cwd, tool_error=True)
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner_tool_err, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["completed_write_tool_observed"] is False
        assert any("No completed file-write tool execution event" in e for e in res["errors"])

    def test_unknown_write_like_tool_fails(self, temp_capability_env):
        """Unknown tool containing 'write' substring (e.g. write_something) fails capability probe."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        def _runner(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            challenge_line = [l for l in cmd[p_idx + 1].splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            stream = (
                json.dumps({"event": "init", "init": {"cwd": cwd, "tools": ["write_something_custom"]}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_something_custom", "state": "DONE", "tool_info": {}}}) + "\n"
                + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["write_tool_advertised"] is False
        assert res["proof"]["completed_write_tool_observed"] is False
        assert any("Native file-write tool was not advertised" in e for e in res["errors"])

    def test_write_step_without_init_advertisement_fails(self, temp_capability_env):
        """Write tool step observed without init.tools advertisement fails probe."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        def _runner(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            challenge_line = [l for l in cmd[p_idx + 1].splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            # Init does not advertise write_to_file, but step uses it
            stream = (
                json.dumps({"event": "init", "init": {"cwd": cwd, "tools": ["read_file"]}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_file", "state": "DONE", "tool_info": {}}}) + "\n"
                + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["write_tool_advertised"] is False
        assert res["proof"]["completed_write_tool_observed"] is True
        assert any("Native file-write tool was not advertised" in e for e in res["errors"])

    def test_init_advertisement_without_completed_write_step_fails(self, temp_capability_env):
        """Init advertising write_to_file without completed write step fails probe."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        def _runner(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            challenge_line = [l for l in cmd[p_idx + 1].splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            # Init advertises write_to_file, but step only has agent_response
            stream = (
                json.dumps({"event": "init", "init": {"cwd": cwd, "tools": ["write_to_file"]}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response", "state": "DONE"}}) + "\n"
                + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["write_tool_advertised"] is True
        assert res["proof"]["completed_write_tool_observed"] is False
        assert any("No completed file-write tool execution event" in e for e in res["errors"])

    def test_init_cwd_exact_match_passes(self, temp_capability_env):
        """Exact workspace cwd in init event passes workspace cwd gate."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        def _runner(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            challenge_line = [l for l in cmd[p_idx + 1].splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            stream = make_valid_stream_ndjson(include_write_tool=True, status="SUCCESS", cwd=cwd)
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "PASS"
        assert res["proof"]["reported_cwd_matches_workspace"] is True

    def test_init_cwd_mismatch_fails(self, temp_capability_env):
        """Mismatch in init reported cwd triggers HOLD."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        def _runner(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            challenge_line = [l for l in cmd[p_idx + 1].splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            # Mismatched cwd
            stream = make_valid_stream_ndjson(include_write_tool=True, status="SUCCESS", cwd="/some/wrong/directory")
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["reported_cwd_matches_workspace"] is False
        assert any("CLI reported cwd does not match" in e for e in res["errors"])

    def test_missing_init_cwd_fails(self, temp_capability_env):
        """Missing init.cwd triggers HOLD."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        def _runner(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            challenge_line = [l for l in cmd[p_idx + 1].splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            # Stream without cwd in init
            stream = (
                json.dumps({"event": "init", "init": {"tools": ["write_file"]}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_file", "state": "DONE", "tool_info": {}}}) + "\n"
                + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["reported_cwd_matches_workspace"] is False
        assert any("CLI reported cwd does not match" in e for e in res["errors"])

    def test_stderr_only_permission_denial_classified_hold(self, temp_capability_env):
        """Stderr-only permission denial triggers HOLD even if stream contains no denial and status=SUCCESS."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        def _runner_stderr_only(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            challenge_line = [l for l in cmd[p_idx + 1].splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            # Perfectly valid stream with NO denial in stream
            stream = make_valid_stream_ndjson(include_write_tool=True, status="SUCCESS", soft_denial=False, cwd=cwd)
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="Permission denied: user prompt not answered in headless mode")

        res = run_antigravity_probe(fake_exe, runner=_runner_stderr_only, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["permission_soft_denial_observed"] is True
        assert res["proof"]["stderr_present"] is True
        assert any("PERMISSION_SOFT_DENIAL" in e for e in res["errors"])

    def test_stream_only_permission_denial_classified_hold(self, temp_capability_env):
        """Stream-only permission denial triggers HOLD even if stderr is empty."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        def _runner_stream_only(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            challenge_line = [l for l in cmd[p_idx + 1].splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            stream = make_valid_stream_ndjson(include_write_tool=True, soft_denial=True, cwd=cwd)
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner_stream_only, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["permission_soft_denial_observed"] is True
        assert res["proof"]["stderr_present"] is False
        assert any("PERMISSION_SOFT_DENIAL" in e for e in res["errors"])

    def test_stream_plus_stderr_permission_denial_classified_hold(self, temp_capability_env):
        """Stream + stderr both containing denial signals triggers HOLD."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }

        def _runner_both(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            challenge_line = [l for l in cmd[p_idx + 1].splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            stream = make_valid_stream_ndjson(include_write_tool=True, soft_denial=True, cwd=cwd)
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="Permission denied: interactive review required")

        res = run_antigravity_probe(fake_exe, runner=_runner_both, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["permission_soft_denial_observed"] is True
        assert res["proof"]["stderr_present"] is True
        assert any("PERMISSION_SOFT_DENIAL" in e for e in res["errors"])

    def test_build_antigravity_argv_output_format_validation(self):
        """build_antigravity_argv strictly validates output_format."""
        argv_json = build_antigravity_argv("/bin/agy", "/tmp/ws", "prompt", 180, output_format="json")
        assert "--output-format=json" in argv_json

        argv_stream = build_antigravity_argv("/bin/agy", "/tmp/ws", "prompt", 180, output_format="stream-json")
        assert "--output-format=stream-json" in argv_stream

        with pytest.raises(ValueError, match="Unsupported output_format"):
            build_antigravity_argv("/bin/agy", "/tmp/ws", "prompt", 180, output_format="xml")

    def test_active_followed_by_error_generic_tool_error(self, temp_capability_env):
        """B. ACTIVE -> ERROR native write with generic tool error: valid stream, failure preserved, HOLD."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.18",
        }

        def _runner(cmd, cwd, timeout, env):
            stream = (
                json.dumps({"event": "init", "init": {"cwd": cwd, "tools": ["write_file"], "permission_mode": "request-review"}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_file", "state": "ACTIVE", "tool_info": {}}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_file", "state": "ERROR", "tool_info": {"error": {"type": "IOError", "message": "Failed to write file"}}}}) + "\n"
                + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["stream_valid"] is True
        assert res["proof"]["terminal_status"] == "SUCCESS"
        assert res["proof"]["completed_write_tool_observed"] is False
        assert res["proof"]["failed_native_write_tool_observed"] is True
        assert res["proof"]["failed_step_observed"] is True
        assert res["proof"]["failed_tool_observed"] is True
        assert res["proof"]["failed_tool_name"] == "write_file"
        assert res["proof"]["failed_tool_state"] == "ERROR"
        assert res["proof"]["failed_tool_error_present"] is True
        assert res["proof"]["failed_tool_error_type"] == "FILE_WRITE_ERROR"
        assert res["proof"]["tool_failure_classification"] == "FILE_WRITE_ERROR"
        assert res["proof"]["error_message_present"] is True
        assert res["proof"]["error_message_byte_length"] > 0
        assert res["proof"]["error_message_sha256"] is not None
        assert "Failed to write file" not in str(res["proof"])
        assert res["proof"]["permission_soft_denial_observed"] is False

    def test_active_followed_by_error_permission_denial(self, temp_capability_env):
        """C. ACTIVE -> ERROR native write with permission error in tool_info: permission_soft_denial_observed true, HOLD."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.18",
        }

        def _runner(cmd, cwd, timeout, env):
            stream = (
                json.dumps({"event": "init", "init": {"cwd": cwd, "tools": ["write_to_file"], "permission_mode": "request-review"}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_to_file", "state": "ACTIVE", "tool_info": {}}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_to_file", "state": "ERROR", "tool_info": {"error": {"type": "PERMISSION_DENIED", "message": "Permission denied: interactive approval needed"}}}}) + "\n"
                + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["stream_valid"] is True
        assert res["proof"]["completed_write_tool_observed"] is False
        assert res["proof"]["failed_native_write_tool_observed"] is True
        assert res["proof"]["failed_tool_observed"] is True
        assert res["proof"]["failed_tool_error_present"] is True
        assert res["proof"]["failed_tool_error_type"] == "PERMISSION_DENIED"
        assert res["proof"]["permission_soft_denial_observed"] is True

    def test_active_followed_by_error_no_tool_info_error(self, temp_capability_env):
        """A & D. ACTIVE -> ERROR with NO tool_info.error: valid stream, error_present false, error_type null, TOOL_FAILURE_UNKNOWN, HOLD."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.18",
        }

        def _runner(cmd, cwd, timeout, env):
            stream = (
                json.dumps({"event": "init", "init": {"cwd": cwd, "tools": ["write_to_file"], "permission_mode": "request-review"}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_to_file", "state": "ACTIVE", "tool_info": {}}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_to_file", "state": "ERROR", "tool_info": {}}}) + "\n"
                + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["stream_valid"] is True
        assert res["proof"]["completed_write_tool_observed"] is False
        assert res["proof"]["failed_native_write_tool_observed"] is True
        assert res["proof"]["failed_tool_observed"] is True
        assert res["proof"]["failed_tool_state"] == "ERROR"
        assert res["proof"]["failed_tool_error_present"] is False
        assert res["proof"]["failed_tool_error_type"] is None
        assert res["proof"]["error_message_present"] is False
        assert res["proof"]["error_message_byte_length"] == 0
        assert res["proof"]["error_message_sha256"] is None
        assert res["proof"]["tool_failure_classification"] == "TOOL_FAILURE_UNKNOWN"
        assert res["proof"]["permission_soft_denial_observed"] is False

    def test_active_error_followed_by_done_probe_holds(self, temp_capability_env):
        """E. ACTIVE -> ERROR -> DONE with exact challenge file: completed_write_tool_observed true, but failed_native_write_tool_observed true -> probe HOLD."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.18",
        }

        def _runner(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            challenge_line = [l for l in cmd[p_idx + 1].splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            stream = (
                json.dumps({"event": "init", "init": {"cwd": cwd, "tools": ["write_file"], "permission_mode": "request-review"}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_file", "state": "ACTIVE", "tool_info": {}}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_file", "state": "ERROR", "tool_info": {"error": {"type": "IOError", "message": "Failed to write file"}}}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_file", "state": "DONE", "tool_info": {}}}) + "\n"
                + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["completed_write_tool_observed"] is True
        assert res["proof"]["failed_native_write_tool_observed"] is True
        assert any("Failed native file-write tool execution event observed" in e for e in res["errors"])

    def test_done_with_tool_info_error_fails(self, temp_capability_env):
        """G. DONE with tool_info.error: completed_write_tool_observed false, failed_native_write_tool_observed true, HOLD."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.18",
        }

        def _runner(cmd, cwd, timeout, env):
            p_idx = cmd.index("-p")
            challenge_line = [l for l in cmd[p_idx + 1].splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            stream = (
                json.dumps({"event": "init", "init": {"cwd": cwd, "tools": ["write_file"], "permission_mode": "request-review"}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_file", "state": "DONE", "tool_info": {"error": {"type": "FileError", "message": "Failed to write file"}}}}) + "\n"
                + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["completed_write_tool_observed"] is False
        assert res["proof"]["failed_native_write_tool_observed"] is True

    def test_error_classifier_precision(self):
        """H, I, J, K. Error classifier distinguishes controlled file write terms and avoids false positives on 'io'."""
        # H. 'operation failed' must NOT classify FILE_WRITE_ERROR
        res_h = sanitize_tool_error({"type": "RuntimeError", "message": "operation failed"})
        assert res_h["classification"] == "TOOL_ERROR"
        assert res_h["error_type"] == "TOOL_ERROR"

        # I. 'version mismatch' must NOT classify FILE_WRITE_ERROR
        res_i = sanitize_tool_error({"type": "VersionError", "message": "version mismatch detected"})
        assert res_i["classification"] == "TOOL_ERROR"
        assert res_i["error_type"] == "TOOL_ERROR"

        # J. 'Failed to write file' classifies FILE_WRITE_ERROR
        res_j = sanitize_tool_error({"type": "CustomError", "message": "Failed to write file"})
        assert res_j["classification"] == "FILE_WRITE_ERROR"
        assert res_j["error_type"] == "FILE_WRITE_ERROR"

        # K. 'I/O error while writing' classifies FILE_WRITE_ERROR
        res_k = sanitize_tool_error({"type": "CustomError", "message": "I/O error while writing"})
        assert res_k["classification"] == "FILE_WRITE_ERROR"
        assert res_k["error_type"] == "FILE_WRITE_ERROR"

    def test_error_followed_by_terminal_error(self, temp_capability_env):
        """F. ERROR followed by terminal ERROR: terminal_status ERROR captured, HOLD."""
        temp_dir, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.18",
        }

        def _runner(cmd, cwd, timeout, env):
            stream = (
                json.dumps({"event": "init", "init": {"cwd": cwd, "tools": ["write_to_file"], "permission_mode": "request-review"}}) + "\n"
                + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_to_file", "state": "ERROR", "tool_info": {}}}) + "\n"
                + json.dumps({"event": "result", "result": {"status": "ERROR"}})
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")

        res = run_antigravity_probe(fake_exe, runner=_runner, store_path=store_file, injected_identity=fake_id)
        assert res["status"] == "HOLD"
        assert res["proof"]["terminal_status"] == "ERROR"
        assert res["proof"]["terminal_error_present"] is True

    def test_error_with_raw_secret_message_sanitization(self):
        """G. ERROR with raw secret/path-like error message: raw message never persisted."""
        raw_stream = (
            json.dumps({"event": "init", "init": {"cwd": "/tmp/ws", "tools": ["write_file"], "permission_mode": "request-review"}}) + "\n"
            + json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_file", "state": "ERROR", "tool_info": {"error": {"type": "FileWriteError", "message": "Failed writing to /secret/user/token/sk-supersecret-token-12345"}}}}) + "\n"
            + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
        )
        parsed = parse_antigravity_stream_output(raw_stream, workspace_path="/tmp/ws")
        assert parsed["is_valid_stream"] is True
        assert parsed["failed_tool_observed"] is True
        assert parsed["failed_tool_error_present"] is True
        assert parsed["error_message_present"] is True
        assert parsed["error_message_byte_length"] > 0
        assert parsed["error_message_sha256"] is not None
        assert "sk-supersecret-token-12345" not in str(parsed)
        assert "/secret/user/token" not in str(parsed)

    def test_permission_mode_request_review_alone_does_not_set_denial(self):
        """J. permission_mode=request-review alone does NOT set permission_soft_denial_observed."""
        raw_stream = (
            json.dumps({"event": "init", "init": {"cwd": "/tmp/ws", "tools": ["write_file"], "permission_mode": "request-review"}}) + "\n"
            + json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response", "state": "DONE"}}) + "\n"
            + json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
        )
        parsed = parse_antigravity_stream_output(raw_stream, workspace_path="/tmp/ws")
        assert parsed["is_valid_stream"] is True
        assert parsed["permission_mode"] == "request-review"
        assert parsed["permission_soft_denial_observed"] is False
