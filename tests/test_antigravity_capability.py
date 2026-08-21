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
    resolve_capability_status,
    resolve_executable_identity,
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
            "controlled_file_edit",
            "no_commit_observed",
            "no_push_observed",
        ],
        "limitations": [
            "This proves behavioral non-interactive execution in the disposable probe environment. It does not prove OS-level sandbox containment."
        ],
    }


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
        att = make_valid_attestation(exe_sha=fake_id["sha256"], cli_ver="1.1.17", contract_ver="0.1.0")
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
            # Verify exact command construction
            assert cmd[0] == fake_exe
            assert "--mode=accept-edits" in cmd
            assert "--output-format=json" in cmd
            assert "--dangerously-skip-permissions" not in cmd
            assert "--print" not in cmd
            assert "-p" in cmd
            p_idx = cmd.index("-p")
            prompt_str = cmd[p_idx + 1]

            challenge_line = [l for l in prompt_str.splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            probe_dir.mkdir(parents=True, exist_ok=True)
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"status": "SUCCESS", "exit_code": 0}), stderr="")

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
        assert res["attestation"]["adapter_contract_version"] == "0.2.0"
        assert res["proof"]["result"] == "PASS"
        assert res["proof"]["changed_paths"] == ["probe/result.txt"]
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
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Error")

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
            probe_dir.mkdir(parents=True, exist_ok=True)
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"status": "SUCCESS"}), stderr="")

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


class TestAntigravityTransportContractAndIdentityPinning:
    def test_build_antigravity_argv_exact_shape(self):
        """Verify exact argv structure and flags according to contract v0.2.0."""
        argv = build_antigravity_argv(
            executable_path="/usr/bin/agy",
            workspace_path="/tmp/workspace",
            prompt="Do task",
            timeout_seconds=180,
        )
        assert argv[0] == "/usr/bin/agy"
        assert argv[1] == "--mode=accept-edits"
        assert argv[2] == "--add-dir"
        assert argv[3] == "/tmp/workspace"
        assert argv[4] == "--output-format=json"
        assert argv[5] == "--print-timeout=180s"
        assert argv[6] == "-p"
        assert argv[7] == "Do task"
        assert len(argv) == 8

        # Invariant checks
        assert "--print" not in argv
        assert "--dangerously-skip-permissions" not in argv
        assert argv.count("-p") == 1
        assert argv.count("--prompt") == 0

    def test_parse_antigravity_json_output(self):
        """Verify sanitized parsing of top-level JSON response."""
        stdout = json.dumps({"status": "SUCCESS", "exit_code": 0, "timed_out": False, "random_extra": "foo"})
        parsed = parse_antigravity_json_output(stdout)
        assert parsed["status"] == "SUCCESS"
        assert parsed["exit_code"] == 0
        assert parsed["timed_out"] is False
        assert parsed["error_present"] is False
        assert "random_extra" not in parsed

        err_stdout = json.dumps({"status": "ERROR", "error": "Something broke"})
        err_parsed = parse_antigravity_json_output(err_stdout)
        assert err_parsed["status"] == "ERROR"
        assert err_parsed["error_present"] is True

        non_json = "plain text"
        non_parsed = parse_antigravity_json_output(non_json)
        assert non_parsed["status"] is None
        assert non_parsed["error_present"] is False

    def test_runtime_identity_revalidation_failure_prevents_worker_subprocess(self, temp_capability_env):
        """If identity changes before execution, execution fails closed with zero worker calls."""
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

        worker_calls = 0

        def _mock_runner(cmd, cwd, timeout, env):
            nonlocal worker_calls
            worker_calls += 1
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        adapter = AntigravityWorkerAdapter(
            cli_command=fake_exe,
            runner=_mock_runner,
            store_path=store_file,
            injected_identity=dict(fake_id),
        )
        assert adapter.capability_status == "PROVEN"

        # Now simulate binary hash changing on disk
        adapter.injected_identity["sha256"] = "1111111111111111111111111111111111111111111111111111111111111111"

        res = adapter.execute(
            task={"task_id": "TASK-1", "title": "T", "description": "D"},
            workspace_path="/tmp/ws",
            allowed_scope={"paths": ["src/"]},
            base_sha="0000000000000000000000000000000000000000",
        )

        assert worker_calls == 0
        assert adapter.capability_status == "UNPROVEN"
        assert res.exit_code == 1
        assert res.mutation_attempted is False
        assert "revalidation failed" in res.stderr_summary

    def test_runtime_identity_version_change_prevents_worker_subprocess(self, temp_capability_env):
        """If reported version changes before execution, execution fails closed with zero worker calls."""
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

        worker_calls = 0

        def _mock_runner(cmd, cwd, timeout, env):
            nonlocal worker_calls
            worker_calls += 1
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        adapter = AntigravityWorkerAdapter(
            cli_command=fake_exe,
            runner=_mock_runner,
            store_path=store_file,
            injected_identity=dict(fake_id),
        )
        assert adapter.capability_status == "PROVEN"

        # Simulate version update
        adapter.injected_identity["version"] = "1.1.18"

        res = adapter.execute(
            task={"task_id": "TASK-1", "title": "T", "description": "D"},
            workspace_path="/tmp/ws",
            allowed_scope={"paths": ["src/"]},
            base_sha="0000000000000000000000000000000000000000",
        )

        assert worker_calls == 0
        assert adapter.capability_status == "UNPROVEN"
        assert res.exit_code == 1
        assert res.mutation_attempted is False

    def test_runtime_identity_path_change_prevents_worker_subprocess(self, temp_capability_env):
        """If executable path changes before execution, execution fails closed with zero worker calls."""
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

        worker_calls = 0

        def _mock_runner(cmd, cwd, timeout, env):
            nonlocal worker_calls
            worker_calls += 1
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        adapter = AntigravityWorkerAdapter(
            cli_command=fake_exe,
            runner=_mock_runner,
            store_path=store_file,
            injected_identity=dict(fake_id),
        )
        assert adapter.capability_status == "PROVEN"

        # Simulate path change
        adapter.injected_identity["path"] = "/other/path/agy.exe"

        res = adapter.execute(
            task={"task_id": "TASK-1", "title": "T", "description": "D"},
            workspace_path="/tmp/ws",
            allowed_scope={"paths": ["src/"]},
            base_sha="0000000000000000000000000000000000000000",
        )

        assert worker_calls == 0
        assert adapter.capability_status == "UNPROVEN"
        assert res.exit_code == 1
        assert res.mutation_attempted is False

    def test_adapter_contract_bump_invalidates_old_attestations(self, temp_capability_env):
        """Attestations created under contract 0.1.0 are invalid under 0.2.0."""
        _, store_file, fake_exe = temp_capability_env
        fake_id = {
            "path": fake_exe,
            "filename": Path(fake_exe).name,
            "sha256": compute_file_sha256(fake_exe),
            "version": "1.1.17",
        }
        att = make_valid_attestation(
            exe_sha=fake_id["sha256"],
            cli_ver=fake_id["version"],
            contract_ver="0.1.0",
            exe_name=fake_id["filename"],
        )
        write_local_capability_attestation(att, store_path=store_file)

        status = resolve_capability_status(fake_exe, store_path=store_file, identity=fake_id)
        assert status == "UNPROVEN"
