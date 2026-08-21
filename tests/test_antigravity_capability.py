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
    compute_file_sha256,
    get_local_capability_store_path,
    get_reported_cli_version,
    resolve_capability_status,
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
    yield temp_dir, store_file
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
    exe_path = shutil.which("agy")
    if not exe_sha:
        exe_sha = compute_file_sha256(exe_path) if exe_path else "550863e77436c18d4b2e3a60cbf6e33b39c33dbf68058294dd6e34a878c9ccaf"
    if not cli_ver:
        cli_ver = get_reported_cli_version("agy") or "1.1.17"
    if not exe_name:
        exe_name = Path(exe_path).name if exe_path else "agy"
    return {
        "schema_version": "0.1.0",
        "worker_adapter": "antigravity",
        "adapter_contract_version": contract_ver,
        "executable_filename": exe_name,
        "executable_sha256": exe_sha,
        "reported_cli_version": cli_ver,
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
        _, store_file = temp_capability_env
        assert not store_file.exists()
        status = resolve_capability_status("agy", store_path=store_file)
        assert status == "UNPROVEN"

    def test_malformed_attestation_resolves_unproven(self, temp_capability_env):
        """2. Malformed or invalid JSON attestation resolves to UNPROVEN."""
        _, store_file = temp_capability_env
        store_file.write_text('{"invalid_schema": true}', encoding="utf-8")
        status = resolve_capability_status("agy", store_path=store_file)
        assert status == "UNPROVEN"

    def test_wrong_executable_hash_resolves_unproven(self, temp_capability_env):
        """3. Attestation with mismatched executable SHA256 resolves to UNPROVEN."""
        _, store_file = temp_capability_env
        att = make_valid_attestation(exe_sha="0000000000000000000000000000000000000000000000000000000000000000")
        write_local_capability_attestation(att, store_path=store_file)
        status = resolve_capability_status("agy", store_path=store_file)
        assert status == "UNPROVEN"

    def test_wrong_cli_version_resolves_unproven(self, temp_capability_env):
        """4. Attestation with mismatched reported CLI version resolves to UNPROVEN."""
        _, store_file = temp_capability_env
        exe_path = shutil.which("agy")
        current_sha = compute_file_sha256(exe_path) if exe_path else "550863e77436c18d4b2e3a60cbf6e33b39c33dbf68058294dd6e34a878c9ccaf"
        att = make_valid_attestation(exe_sha=current_sha, cli_ver="9.9.9")
        write_local_capability_attestation(att, store_path=store_file)
        status = resolve_capability_status("agy", store_path=store_file)
        assert status == "UNPROVEN"

    def test_wrong_adapter_contract_version_resolves_unproven(self, temp_capability_env):
        """5. Attestation with wrong adapter contract version resolves to UNPROVEN."""
        _, store_file = temp_capability_env
        exe_path = shutil.which("agy")
        current_sha = compute_file_sha256(exe_path) if exe_path else "550863e77436c18d4b2e3a60cbf6e33b39c33dbf68058294dd6e34a878c9ccaf"
        att = make_valid_attestation(exe_sha=current_sha, contract_ver="9.9.9")
        write_local_capability_attestation(att, store_path=store_file)
        status = resolve_capability_status("agy", store_path=store_file)
        assert status == "UNPROVEN"

    def test_matching_attestation_resolves_proven(self, temp_capability_env):
        """6. Attestation matching exact executable SHA256 and reported version resolves to PROVEN."""
        temp_dir, store_file = temp_capability_env
        exe_path = shutil.which("agy")
        if exe_path:
            current_sha = compute_file_sha256(exe_path)
            ver_res = subprocess.run(["agy", "--version"], capture_output=True, text=True)
            current_ver = ver_res.stdout.strip()
            cli_cmd = "agy"
            filename = Path(exe_path).name
            v_runner = None
        else:
            fake_exe = Path(temp_dir) / "agy"
            fake_exe.write_text("fake binary", encoding="utf-8")
            exe_path = str(fake_exe)
            current_sha = compute_file_sha256(fake_exe)
            current_ver = "1.1.15"
            cli_cmd = str(fake_exe)
            filename = "agy"
            v_runner = lambda args: subprocess.CompletedProcess(args, 0, stdout="1.1.15", stderr="")

        att = make_valid_attestation(exe_sha=current_sha, cli_ver=current_ver, exe_name=filename)
        write_local_capability_attestation(att, store_path=store_file)

        status = resolve_capability_status(cli_cmd, store_path=store_file, version_runner=v_runner)
        assert status == "PROVEN"

        adapter = AntigravityWorkerAdapter(cli_command=cli_cmd, store_path=store_file)
        if v_runner:
            adapter.capability_status = resolve_capability_status(cli_cmd, store_path=store_file, version_runner=v_runner)
        assert adapter.capability_status == "PROVEN"

    def test_test_double_override_available_only_for_tests(self):
        """7. TEST_DOUBLE status is explicitly passed and distinct from PROVEN/UNPROVEN."""
        adapter = AntigravityWorkerAdapter(capability_status_override="TEST_DOUBLE")
        assert adapter.capability_status == "TEST_DOUBLE"

    def test_attestation_schema_validation(self):
        """8. Attestation document conforms strictly to worker_capability_attestation schema."""
        att = make_valid_attestation()
        res = validate_document("worker_capability_attestation", att)
        assert res.is_valid is True

    def test_mock_probe_execution_pass(self, temp_capability_env):
        """9. Mocked probe execution creates result file, verifies git invariants, and writes attestation."""
        temp_dir, store_file = temp_capability_env
        parent_dir = Path(temp_dir) / "probe_parent"

        def _mock_coding_runner(cmd, cwd, timeout, env):
            # Simulate worker creating probe/result.txt with challenge content
            prompt_str = cmd[-1]
            challenge_line = [l for l in prompt_str.splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            probe_dir.mkdir(parents=True, exist_ok=True)
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="Success", stderr="")

        res = run_antigravity_probe(
            cli_command="agy",
            runner=_mock_coding_runner,
            store_path=store_file,
            custom_parent_dir=str(parent_dir),
            aos_revision="fce499bb3c1449a0b2048d7be779116da615f698",
        )

        assert res["status"] == "PASS"
        assert res["attestation"] is not None
        assert res["proof"]["result"] == "PASS"
        assert res["proof"]["changed_paths"] == ["probe/result.txt"]
        assert store_file.is_file()

        # Check that adapter resolves PROVEN (on local machine with agy or CI mock)
        if shutil.which("agy"):
            adapter = AntigravityWorkerAdapter(store_path=store_file)
            assert adapter.capability_status == "PROVEN"
        else:
            mock_exe = Path(tempfile.gettempdir()) / "aos_mock_bin" / "agy"
            status = resolve_capability_status(
                str(mock_exe),
                store_path=store_file,
                version_runner=lambda args: subprocess.CompletedProcess(args, 0, stdout="1.1.17", stderr=""),
            )
            assert status == "PROVEN"

    def test_mock_probe_failure_never_writes_attestation(self, temp_capability_env):
        """10. Failed probe (nonzero exit / missing file) never writes attestation."""
        temp_dir, store_file = temp_capability_env
        parent_dir = Path(temp_dir) / "probe_parent_fail"

        def _mock_failing_runner(cmd, cwd, timeout, env):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Error")

        res = run_antigravity_probe(
            cli_command="agy",
            runner=_mock_failing_runner,
            store_path=store_file,
            custom_parent_dir=str(parent_dir),
            aos_revision="fce499bb3c1449a0b2048d7be779116da615f698",
        )

        assert res["status"] == "HOLD"
        assert res["attestation"] is None
        assert not store_file.exists()

    def test_probe_scrubs_sensitive_environment_variables(self, temp_capability_env):
        """11. Probe runner receives an environment stripped of sensitive tokens."""
        temp_dir, store_file = temp_capability_env
        parent_dir = Path(temp_dir) / "probe_parent_env"

        captured_env: Dict[str, str] = {}

        def _env_checking_runner(cmd, cwd, timeout, env):
            nonlocal captured_env
            captured_env = dict(env)
            prompt_str = cmd[-1]
            challenge_line = [l for l in prompt_str.splitlines() if "AOS-CAPABILITY-CHALLENGE-" in l][0].strip()
            probe_dir = Path(cwd) / "probe"
            probe_dir.mkdir(parents=True, exist_ok=True)
            (probe_dir / "result.txt").write_text(challenge_line, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="Success", stderr="")

        os.environ["OPENAI_API_KEY"] = "sk-fake-openai"
        os.environ["GEMINI_API_KEY"] = "fake-gemini"
        os.environ["GROQ_API_KEY"] = "fake-groq"
        os.environ["GH_TOKEN"] = "ghp-fake-token"

        try:
            res = run_antigravity_probe(
                cli_command="agy",
                runner=_env_checking_runner,
                store_path=store_file,
                custom_parent_dir=str(parent_dir),
                aos_revision="fce499bb3c1449a0b2048d7be779116da615f698",
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
        _, store_file = temp_capability_env
        desc = make_generic_descriptor()
        task = make_generic_task()
        mock_source = MockSourceAdapter("GenericOrg/GenericRepo", "control/main")
        mock_ws = MockGitWorkspace("repo", task["base_sha"], task["task_id"])

        adapter = AntigravityWorkerAdapter(store_path=store_file)
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
