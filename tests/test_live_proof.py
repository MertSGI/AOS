"""Offline unit tests for AOS-2 live_proof runner."""

import os
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aos.live_proof import (
    execute_live_proof,
    main,
    run_readiness_checks,
    scan_traces_for_credentials,
    validate_request_file,
)

DESCRIPTOR_PATH = Path(__file__).parent.parent / "descriptors" / "lari.descriptor.json"
EXPECTATION_PATH = Path(__file__).parent.parent / "descriptors" / "lari.shadow-expectation.json"
POLICY_PATH = Path(__file__).parent.parent / "descriptors" / "lari.planner-policy.json"
REQUEST_PATH = Path(__file__).parent.parent / ".aos-control" / "live-proof-request.json"
WORKFLOW_PATH = Path(__file__).parent.parent / ".github" / "workflows" / "aos2-remote-live-proof.yml"


class TestLiveProofDryRun:
    def test_dry_run_success(self):
        """Dry-run returns READY_FOR_CREDENTIAL_INPUT and code 0."""
        status, code = execute_live_proof(dry_run=True, verify_git=False)
        assert status == "READY_FOR_CREDENTIAL_INPUT"
        assert code == 0

    def test_dry_run_reports_aos_revision(self, capsys):
        """Dry-run outputs the captured aos_revision SHA."""
        status, code = execute_live_proof(dry_run=True, verify_git=False)
        captured = capsys.readouterr().out
        assert "AOS Revision:" in captured
        assert status == "READY_FOR_CREDENTIAL_INPUT"

    def test_dry_run_makes_zero_credential_prompts(self):
        """Dry-run does not call getpass or prompt function."""
        mock_prompt = MagicMock()
        status, code = execute_live_proof(dry_run=True, prompt_func=mock_prompt, verify_git=False)
        assert status == "READY_FOR_CREDENTIAL_INPUT"
        assert mock_prompt.call_count == 0

    def test_dry_run_makes_zero_provider_calls(self):
        """Dry-run makes zero LLM calls."""
        with patch("aos.live_proof.run_benchmark") as mock_bm:
            status, code = execute_live_proof(dry_run=True, verify_git=False)
            assert status == "READY_FOR_CREDENTIAL_INPUT"
            assert mock_bm.call_count == 0


class TestLiveProofCIMode:
    def test_ci_mode_accepts_existing_env_credentials_without_prompt(self, monkeypatch, tmp_path):
        """CI mode uses existing env keys without calling getpass or input."""
        monkeypatch.setenv("GEMINI_API_KEY", "ci-gemini-key-123")
        monkeypatch.setenv("GROQ_API_KEY", "ci-groq-key-456")

        req_path = tmp_path / "valid_request.json"
        req_path.write_text(json.dumps({"schema_version": "0.1.0", "gate": "AOS-2", "authorized": True, "request_id": "REQ-123"}), encoding="utf-8")

        mock_prompt = MagicMock()
        mock_input = MagicMock()

        with patch("aos.live_proof.run_benchmark", return_value={"benchmark_status": "PASS", "providers": {"gemini": {"status": "PASS"}, "groq": {"status": "PASS"}}, "total_runs": 6, "total_pass": 6}) as mock_bm:
            status, code = execute_live_proof(
                ci=True,
                request_path=str(req_path),
                prompt_func=mock_prompt,
                input_func=mock_input,
                trace_dir_override=tmp_path,
                verify_git=False,
            )
            assert status == "LIVE_FREE_PROVIDER_PROOF_READY_FOR_INDEPENDENT_VERIFICATION"
            assert code == 0
            assert mock_prompt.call_count == 0
            assert mock_input.call_count == 0
            assert mock_bm.call_count == 1

    def test_ci_mode_missing_gemini_secret_holds_before_provider_call(self, monkeypatch, tmp_path):
        """CI mode with missing Gemini key holds before calling run_benchmark."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "ci-groq-key")

        req_path = tmp_path / "valid_request.json"
        req_path.write_text(json.dumps({"schema_version": "0.1.0", "gate": "AOS-2", "authorized": True, "request_id": "REQ-123"}), encoding="utf-8")

        with patch("aos.live_proof.run_benchmark") as mock_bm:
            status, code = execute_live_proof(
                ci=True,
                request_path=str(req_path),
                verify_git=False,
            )
            assert status == "HOLD"
            assert code == 1
            assert mock_bm.call_count == 0

    def test_ci_mode_missing_groq_secret_holds_before_provider_call(self, monkeypatch, tmp_path):
        """CI mode with missing Groq key holds before calling run_benchmark."""
        monkeypatch.setenv("GEMINI_API_KEY", "ci-gemini-key")
        monkeypatch.delenv("GROQ_API_KEY", raising=False)

        req_path = tmp_path / "valid_request.json"
        req_path.write_text(json.dumps({"schema_version": "0.1.0", "gate": "AOS-2", "authorized": True, "request_id": "REQ-123"}), encoding="utf-8")

        with patch("aos.live_proof.run_benchmark") as mock_bm:
            status, code = execute_live_proof(
                ci=True,
                request_path=str(req_path),
                verify_git=False,
            )
            assert status == "HOLD"
            assert code == 1
            assert mock_bm.call_count == 0

    def test_request_authorized_false_holds_before_provider_call(self, monkeypatch, tmp_path):
        """CI mode with authorized=False holds before provider call."""
        monkeypatch.setenv("GEMINI_API_KEY", "ci-gemini-key")
        monkeypatch.setenv("GROQ_API_KEY", "ci-groq-key")

        req_path = tmp_path / "unauth_request.json"
        req_path.write_text(json.dumps({"schema_version": "0.1.0", "gate": "AOS-2", "authorized": False, "request_id": "NOT_AUTHORIZED"}), encoding="utf-8")

        with patch("aos.live_proof.run_benchmark") as mock_bm:
            status, code = execute_live_proof(
                ci=True,
                request_path=str(req_path),
                verify_git=False,
            )
            assert status == "HOLD"
            assert code == 1
            assert mock_bm.call_count == 0

    def test_wrong_gate_in_request_holds(self, monkeypatch, tmp_path):
        """CI mode with gate!='AOS-2' holds before provider call."""
        monkeypatch.setenv("GEMINI_API_KEY", "ci-gemini-key")
        monkeypatch.setenv("GROQ_API_KEY", "ci-groq-key")

        req_path = tmp_path / "wrong_gate_request.json"
        req_path.write_text(json.dumps({"schema_version": "0.1.0", "gate": "AOS-99", "authorized": True, "request_id": "REQ-99"}), encoding="utf-8")

        with patch("aos.live_proof.run_benchmark") as mock_bm:
            status, code = execute_live_proof(
                ci=True,
                request_path=str(req_path),
                verify_git=False,
            )
            assert status == "HOLD"
            assert code == 1
            assert mock_bm.call_count == 0

    def test_committed_request_file_structure(self):
        """Committed request file .aos-control/live-proof-request.json has valid schema and gate."""
        assert REQUEST_PATH.exists()
        data = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
        assert data.get("gate") == "AOS-2"
        assert "authorized" in data
        assert "request_id" in data


class TestWorkflowContract:
    def test_workflow_file_exists(self):
        """Workflow file .github/workflows/aos2-remote-live-proof.yml exists."""
        assert WORKFLOW_PATH.exists()

    def test_workflow_triggers_and_permissions(self):
        """Workflow file has contents: read, no pull_request/schedule triggers, and exact branch/path scope."""
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "permissions:" in content
        assert "contents: read" in content
        assert "pull_request:" not in content
        assert "schedule:" not in content
        assert "feature/aos-2-shadow-orchestrator" in content
        assert ".aos-control/live-proof-request.json" in content
        assert "python -m aos.live_proof --ci" in content
        assert "secrets.GEMINI_API_KEY" in content
        assert "secrets.GROQ_API_KEY" in content
        assert "secrets.OPENAI_API_KEY" not in content


class TestLiveProofReadinessFailures:
    def test_head_equals_origin_feature_readiness_pass(self):
        """When current HEAD == origin/feature/aos-2-shadow-orchestrator, readiness passes."""
        sha = "1111111111111111111111111111111111111111"
        with patch("aos.live_proof.get_git_info", return_value=("feature/aos-2-shadow-orchestrator", sha, "9109859e6d6a231598c22f68224f512f198c9a49", sha, True)):
            ok, reason, _, _, revision = run_readiness_checks(verify_git=True)
            assert ok is True
            assert reason == "READINESS_OK"
            assert revision == sha

    def test_head_not_equals_origin_feature_holds_before_prompt(self):
        """When current HEAD != origin/feature, readiness fails before prompt."""
        with patch("aos.live_proof.get_git_info", return_value=("feature/aos-2-shadow-orchestrator", "local-sha-123", "9109859e6d6a231598c22f68224f512f198c9a49", "remote-sha-456", True)):
            mock_prompt = MagicMock()
            status, code = execute_live_proof(
                dry_run=False,
                prompt_func=mock_prompt,
                verify_git=True,
            )
            assert status == "HOLD"
            assert code == 1
            assert mock_prompt.call_count == 0

    def test_invalid_descriptor_holds_before_prompt(self, tmp_path):
        """Invalid project descriptor fails readiness check before credential prompt."""
        bad_desc = tmp_path / "bad_desc.json"
        bad_desc.write_text("{}", encoding="utf-8")

        mock_prompt = MagicMock()
        status, code = execute_live_proof(
            dry_run=False,
            descriptor_path=str(bad_desc),
            prompt_func=mock_prompt,
            verify_git=False,
        )
        assert status == "HOLD"
        assert code == 1
        assert mock_prompt.call_count == 0

    def test_wrong_branch_holds_before_prompt(self):
        """Wrong git branch fails readiness check before prompt."""
        with patch("aos.live_proof.get_git_info", return_value=("wrong-branch", "sha1", "9109859e6d6a231598c22f68224f512f198c9a49", "sha1", True)):
            mock_prompt = MagicMock()
            status, code = execute_live_proof(
                dry_run=False,
                prompt_func=mock_prompt,
                verify_git=True,
            )
            assert status == "HOLD"
            assert code == 1
            assert mock_prompt.call_count == 0

    def test_wrong_origin_main_holds_before_prompt(self):
        """Wrong origin/main SHA fails readiness check before prompt."""
        with patch("aos.live_proof.get_git_info", return_value=("feature/aos-2-shadow-orchestrator", "sha1", "wrong-main-sha", "sha1", True)):
            mock_prompt = MagicMock()
            status, code = execute_live_proof(
                dry_run=False,
                prompt_func=mock_prompt,
                verify_git=True,
            )
            assert status == "HOLD"
            assert code == 1
            assert mock_prompt.call_count == 0

    def test_dirty_tracked_git_state_holds_before_prompt(self):
        """Dirty tracked git working tree fails readiness check before prompt."""
        with patch("aos.live_proof.get_git_info", return_value=("feature/aos-2-shadow-orchestrator", "sha1", "9109859e6d6a231598c22f68224f512f198c9a49", "sha1", False)):
            mock_prompt = MagicMock()
            status, code = execute_live_proof(
                dry_run=False,
                prompt_func=mock_prompt,
                verify_git=True,
            )
            assert status == "HOLD"
            assert code == 1
            assert mock_prompt.call_count == 0

    def test_paid_fallback_enabled_holds_before_prompt(self, tmp_path):
        """Paid fallback enabled in policy fails readiness check before prompt."""
        policy_data = {
            "schema_version": "0.1.0",
            "routing_mode": "DETERMINISTIC",
            "allow_paid_fallback": True,
            "allow_provider_fallback": True,
            "data_classification": "PUBLIC",
            "risk_routes": {"R0": {"preferred_providers": ["gemini", "groq"]}},
            "providers": {
                "gemini": {
                    "provider_id": "gemini",
                    "model_id": "gemini-3.6-flash",
                    "credential_env_var": "GEMINI_API_KEY",
                    "billing_class": "FREE_TIER",
                    "structured_output": True,
                    "cloud_local": "CLOUD",
                    "enabled": True,
                    "allowed_data_classifications": ["PUBLIC"],
                },
                "groq": {
                    "provider_id": "groq",
                    "model_id": "openai/gpt-oss-120b",
                    "credential_env_var": "GROQ_API_KEY",
                    "billing_class": "FREE_TIER",
                    "structured_output": True,
                    "cloud_local": "CLOUD",
                    "enabled": True,
                    "allowed_data_classifications": ["PUBLIC"],
                },
            },
        }
        policy_path = tmp_path / "paid_policy.json"
        policy_path.write_text(json.dumps(policy_data), encoding="utf-8")

        mock_prompt = MagicMock()
        status, code = execute_live_proof(
            dry_run=False,
            routing_policy_path=str(policy_path),
            prompt_func=mock_prompt,
            verify_git=False,
        )
        assert status == "HOLD"
        assert code == 1
        assert mock_prompt.call_count == 0

    def test_openai_selected_holds_before_prompt(self):
        """OpenAI requested in providers fails readiness check before prompt."""
        mock_prompt = MagicMock()
        status, code = execute_live_proof(
            dry_run=False,
            provider_ids=["openai"],
            prompt_func=mock_prompt,
            verify_git=False,
        )
        assert status == "HOLD"
        assert code == 1
        assert mock_prompt.call_count == 0


class TestLiveProofSecretHandling:
    def test_existing_credential_requires_confirmation_accepted(self, monkeypatch, tmp_path):
        """If existing key is in env and user responds 'y', existing key is used without getpass."""
        monkeypatch.setenv("GEMINI_API_KEY", "existing-gemini-key")
        monkeypatch.setenv("GROQ_API_KEY", "existing-groq-key")

        mock_prompt = MagicMock()
        mock_input = MagicMock(side_effect=["y", "y"])

        with patch("aos.live_proof.run_benchmark", return_value={"benchmark_status": "PASS", "providers": {"gemini": {"status": "PASS"}, "groq": {"status": "PASS"}}, "total_runs": 6, "total_pass": 6}):
            status, code = execute_live_proof(
                dry_run=False,
                prompt_func=mock_prompt,
                input_func=mock_input,
                trace_dir_override=tmp_path,
                verify_git=False,
            )
            assert mock_input.call_count == 2
            assert mock_prompt.call_count == 0
            assert status == "LIVE_FREE_PROVIDER_PROOF_READY_FOR_INDEPENDENT_VERIFICATION"

    def test_rejected_existing_credential_causes_masked_getpass_prompt(self, monkeypatch, tmp_path):
        """If user rejects existing key with 'n', masked getpass prompt is triggered for new key."""
        monkeypatch.setenv("GEMINI_API_KEY", "old-gemini-key")
        monkeypatch.delenv("GROQ_API_KEY", raising=False)

        mock_prompt = MagicMock(side_effect=["new-gemini-key", "new-groq-key"])
        mock_input = MagicMock(side_effect=["n"])

        with patch("aos.live_proof.run_benchmark", return_value={"benchmark_status": "PASS", "providers": {"gemini": {"status": "PASS"}, "groq": {"status": "PASS"}}, "total_runs": 6, "total_pass": 6}):
            status, code = execute_live_proof(
                dry_run=False,
                prompt_func=mock_prompt,
                input_func=mock_input,
                trace_dir_override=tmp_path,
                verify_git=False,
            )
            assert mock_input.call_count == 1
            assert mock_prompt.call_count == 2
            assert status == "LIVE_FREE_PROVIDER_PROOF_READY_FOR_INDEPENDENT_VERIFICATION"

    def test_no_cli_secret_argument_support(self):
        """CLI parser does NOT accept --gemini-key or --groq-key flags."""
        with pytest.raises(SystemExit):
            main(["--gemini-key", "secret"])

    def test_temporary_env_values_visible_to_benchmark_and_cleaned_after_pass(self, monkeypatch, tmp_path):
        """Env values are populated during benchmark and strictly removed afterward on PASS."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)

        mock_prompt = MagicMock(side_effect=["test-gemini-key-123", "test-groq-key-456"])
        env_during_call = {}

        def fake_run_benchmark(*args, **kwargs):
            env_during_call["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY")
            env_during_call["GROQ_API_KEY"] = os.environ.get("GROQ_API_KEY")
            return {
                "benchmark_status": "PASS",
                "providers": {"gemini": {"status": "PASS"}, "groq": {"status": "PASS"}},
                "total_runs": 6,
                "total_pass": 6,
            }

        with patch("aos.live_proof.run_benchmark", side_effect=fake_run_benchmark):
            status, code = execute_live_proof(
                dry_run=False,
                prompt_func=mock_prompt,
                trace_dir_override=tmp_path,
                verify_git=False,
            )

        assert env_during_call["GEMINI_API_KEY"] == "test-gemini-key-123"
        assert env_during_call["GROQ_API_KEY"] == "test-groq-key-456"
        assert os.environ.get("GEMINI_API_KEY") is None
        assert os.environ.get("GROQ_API_KEY") is None

    def test_env_values_removed_after_hold(self, monkeypatch, tmp_path):
        """Env values are strictly removed after HOLD result."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)

        mock_prompt = MagicMock(side_effect=["test-gemini-key", "test-groq-key"])

        with patch("aos.live_proof.run_benchmark", return_value={"benchmark_status": "HOLD", "providers": {"gemini": {"status": "HOLD"}}}):
            status, code = execute_live_proof(
                dry_run=False,
                prompt_func=mock_prompt,
                trace_dir_override=tmp_path,
                verify_git=False,
            )

        assert status == "HOLD"
        assert os.environ.get("GEMINI_API_KEY") is None
        assert os.environ.get("GROQ_API_KEY") is None

    def test_env_values_removed_after_raised_exception(self, monkeypatch, tmp_path):
        """Env values are strictly removed even if benchmark raises an unhandled exception."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)

        mock_prompt = MagicMock(side_effect=["test-gemini-key", "test-groq-key"])

        with patch("aos.live_proof.run_benchmark", side_effect=RuntimeError("Crash!")):
            with pytest.raises(RuntimeError):
                execute_live_proof(
                    dry_run=False,
                    prompt_func=mock_prompt,
                    trace_dir_override=tmp_path,
                    verify_git=False,
                )

        assert os.environ.get("GEMINI_API_KEY") is None
        assert os.environ.get("GROQ_API_KEY") is None

    def test_credential_leak_in_trace_causes_hold(self, monkeypatch, tmp_path):
        """Trace secret scanner detects key leak in emitted trace file and triggers HOLD."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)

        leaked_key = "AIzaSySecretGeminiKeyForTest12345"
        mock_prompt = MagicMock(side_effect=[leaked_key, "test-groq-key"])

        # Write a trace file containing the leaked key
        leaked_trace = tmp_path / "TRACE-LEAK-001.json"
        leaked_trace.write_text(json.dumps({"key_leak": leaked_key}), encoding="utf-8")

        with patch("aos.live_proof.run_benchmark", return_value={"benchmark_status": "PASS", "providers": {"gemini": {"status": "PASS"}, "groq": {"status": "PASS"}}, "total_runs": 6, "total_pass": 6}):
            status, code = execute_live_proof(
                dry_run=False,
                prompt_func=mock_prompt,
                trace_dir_override=tmp_path,
                verify_git=False,
            )

        assert status == "HOLD"
        assert code == 1


class TestLiveProofSummaryOutputs:
    def test_compact_pass_summary(self, monkeypatch, tmp_path, capsys):
        """Compact summary formatting for PASS result."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)

        mock_prompt = MagicMock(side_effect=["key1", "key2"])

        with patch("aos.live_proof.run_benchmark", return_value={"benchmark_status": "PASS", "providers": {"gemini": {"status": "PASS"}, "groq": {"status": "PASS"}}, "total_runs": 6, "total_pass": 6}):
            status, code = execute_live_proof(
                dry_run=False,
                prompt_func=mock_prompt,
                trace_dir_override=tmp_path,
                verify_git=False,
            )

        captured = capsys.readouterr().out
        assert "AOS-2 Live Free Provider Proof" in captured
        assert "3/3 PASS" in captured
        assert "Overall:\n6/6 PASS" in captured
        assert "LIVE_FREE_PROVIDER_PROOF_READY_FOR_INDEPENDENT_VERIFICATION" in captured

    def test_compact_hold_summary(self, monkeypatch, tmp_path, capsys):
        """Compact summary formatting for HOLD result."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)

        mock_prompt = MagicMock(side_effect=["key1", "key2"])

        with patch("aos.live_proof.run_benchmark", return_value={"benchmark_status": "HOLD", "providers": {"gemini": {"status": "HOLD"}}, "total_runs": 3, "total_pass": 0}):
            status, code = execute_live_proof(
                dry_run=False,
                prompt_func=mock_prompt,
                trace_dir_override=tmp_path,
                verify_git=False,
            )

        captured = capsys.readouterr().out
        assert "Overall: HOLD" in captured
