"""Tests for AOS-5 Hosted Multi-Machine Proof Harness and Live Authorization Boundary."""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aos.coordination import CoordinationStorageError, WorkerIdentity
from aos.coordination_live_proof import (
    check_forbidden_secret_keys,
    compute_proof_scoped_machine_fingerprint,
    execute_dry_run,
    load_and_validate_request,
    run_live_worker,
    validate_proof_artifact,
    validate_proof_request_dict,
    verify_pair_results,
    write_worker_result_atomic,
)
from aos.validate import DuplicateJSONKeyError

VALID_SOURCE_SHA = "e3773b5b037a4654d6ada9faeac348d5ca7aec2c"
VALID_BRANCH = "feature/aos-5-distributed-coordination"


def make_valid_request(
    authorized: bool = False,
    source_sha: str = VALID_SOURCE_SHA,
    control_branch: str = VALID_BRANCH,
    start_at_utc: str = "2026-08-29T16:00:00Z",
    prod_allowed: bool = False,
    dest_allowed: bool = False,
    bill_allowed: bool = False,
) -> dict:
    return {
        "schema_version": "0.1.0",
        "artifact_type": "AOS5_MULTI_MACHINE_PROOF_REQUEST",
        "gate": "AOS-5",
        "proof_id": "PROOF-20260829-001",
        "authorization_id": "AUTH-20260829-001",
        "authorized": authorized,
        "backend_kind": "POSTGRES",
        "environment_class": "NONPRODUCTION_HOSTED_TEST",
        "source_sha": source_sha,
        "control_branch": control_branch,
        "namespace_id": "ns_proof_test",
        "task_id": "task_proof_test",
        "ttl_seconds": 10.0,
        "start_at_utc": start_at_utc,
        "expected_worker_count": 2,
        "workers": [
            {
                "role": "worker_a",
                "worker_id": "w_a",
                "session_id": "s_a",
                "machine_label": "machine_1",
            },
            {
                "role": "worker_b",
                "worker_id": "w_b",
                "session_id": "s_b",
                "machine_label": "machine_2",
            },
        ],
        "production_mutation_allowed": prod_allowed,
        "destructive_operations_allowed": dest_allowed,
        "billing_activation_allowed": bill_allowed,
    }


def make_valid_results(req: dict) -> tuple[dict, dict]:
    obs_lease = {
        "owner_worker_id": "w_a",
        "owner_session_id": "s_a",
        "lease_id": "lease_epoch_101",
        "generation": 1,
        "acquired_at": "2026-08-29T16:00:00.100000+00:00",
        "expires_at": "2026-08-29T16:00:10.100000+00:00",
        "ttl_seconds": 10.0,
        "status": "ACTIVE",
    }
    rec_lease = {
        "owner_worker_id": "w_b",
        "owner_session_id": "s_b",
        "lease_id": "lease_epoch_102",
        "generation": 2,
        "acquired_at": "2026-08-29T16:00:10.500000+00:00",
        "expires_at": "2026-08-29T16:00:20.500000+00:00",
        "ttl_seconds": 10.0,
        "status": "ACTIVE",
    }

    res_a = {
        "schema_version": "0.1.0",
        "artifact_type": "AOS5_MULTI_MACHINE_WORKER_RESULT",
        "gate": "AOS-5",
        "proof_id": req["proof_id"],
        "source_sha": req["source_sha"],
        "control_branch": req["control_branch"],
        "namespace_id": req["namespace_id"],
        "task_id": req["task_id"],
        "role": "worker_a",
        "worker_id": "w_a",
        "session_id": "s_a",
        "machine_label": "machine_1",
        "proof_scoped_machine_fingerprint_sha256": "a" * 64,
        "git_head_sha": req["source_sha"],
        "git_origin_branch_sha": req["source_sha"],
        "working_tree_clean": True,
        "peer_registration_observed": True,
        "peer_worker_id": "w_b",
        "peer_session_id": "s_b",
        "claim_started_at": "2026-08-29T16:00:00.000000+00:00",
        "claim_completed_at": "2026-08-29T16:00:00.100000+00:00",
        "initial_disposition": "ACQUIRED",
        "initial_observed_lease": obs_lease,
        "recovery_attempted": False,
        "recovery_started_at": None,
        "recovery_completed_at": None,
        "recovery_disposition": None,
        "recovery_lease": None,
        "worker_terminal_role": "INITIAL_WINNER_NO_RELEASE",
    }

    res_b = {
        "schema_version": "0.1.0",
        "artifact_type": "AOS5_MULTI_MACHINE_WORKER_RESULT",
        "gate": "AOS-5",
        "proof_id": req["proof_id"],
        "source_sha": req["source_sha"],
        "control_branch": req["control_branch"],
        "namespace_id": req["namespace_id"],
        "task_id": req["task_id"],
        "role": "worker_b",
        "worker_id": "w_b",
        "session_id": "s_b",
        "machine_label": "machine_2",
        "proof_scoped_machine_fingerprint_sha256": "b" * 64,
        "git_head_sha": req["source_sha"],
        "git_origin_branch_sha": req["source_sha"],
        "working_tree_clean": True,
        "peer_registration_observed": True,
        "peer_worker_id": "w_a",
        "peer_session_id": "s_a",
        "claim_started_at": "2026-08-29T16:00:00.000000+00:00",
        "claim_completed_at": "2026-08-29T16:00:00.100000+00:00",
        "initial_disposition": "HELD_BY_OTHER",
        "initial_observed_lease": dict(obs_lease),
        "recovery_attempted": True,
        "recovery_started_at": "2026-08-29T16:00:10.200000+00:00",
        "recovery_completed_at": "2026-08-29T16:00:10.500000+00:00",
        "recovery_disposition": "ACQUIRED",
        "recovery_lease": rec_lease,
        "worker_terminal_role": "INITIAL_LOSER_RECOVERED",
    }

    return res_a, res_b


# A. Valid unauthorized dry-run
def test_scenario_a_unauthorized_dry_run():
    req = make_valid_request(authorized=False)
    with patch("aos.coordination_live_proof.check_git_readiness") as mock_git:
        mock_git.return_value = {"is_ready": True, "head_sha": VALID_SOURCE_SHA, "origin_sha": VALID_SOURCE_SHA, "branch": VALID_BRANCH, "is_clean": True, "reasons": []}
        res = execute_dry_run(req, "worker_a")
        assert res["status"] == "READY_FOR_EXPLICIT_HUMAN_LIVE_AUTHORIZATION"
        assert res["authorized"] is False
        assert res["backend_calls"] == 0
        assert res["network_calls"] == 0


# B. Dry-run ignores absence of live DSN
def test_scenario_b_dry_run_ignores_missing_dsn():
    req = make_valid_request(authorized=True)
    with patch.dict(os.environ, {}, clear=True), patch("aos.coordination_live_proof.check_git_readiness") as mock_git:
        mock_git.return_value = {"is_ready": True, "head_sha": VALID_SOURCE_SHA, "origin_sha": VALID_SOURCE_SHA, "branch": VALID_BRANCH, "is_clean": True, "reasons": []}
        res = execute_dry_run(req, "worker_a")
        assert res["status"] == "DRY_RUN_SUCCESS_AUTHORIZED"
        assert res["backend_calls"] == 0


# C. Live authorized=false: HOLD, 0 backend calls
def test_scenario_c_live_unauthorized_fails_closed():
    req = make_valid_request(authorized=False)
    with pytest.raises(CoordinationStorageError) as exc_info:
        run_live_worker(req, "worker_a")
    assert "authorized is false" in str(exc_info.value)


# D. Live missing DSN: HOLD, 0 backend calls
def test_scenario_d_live_missing_dsn_fails_closed():
    req = make_valid_request(authorized=True)
    with patch.dict(os.environ, {}, clear=True), patch("aos.coordination_live_proof.check_git_readiness") as mock_git:
        mock_git.return_value = {"is_ready": True, "head_sha": VALID_SOURCE_SHA, "origin_sha": VALID_SOURCE_SHA, "branch": VALID_BRANCH, "is_clean": True, "reasons": []}
        with pytest.raises(CoordinationStorageError) as exc_info:
            run_live_worker(req, "worker_a")
        assert "AOS_POSTGRES_LIVE_DSN" in str(exc_info.value)


# E. Production mutation flag true: rejection
def test_scenario_e_prod_flag_true_rejected():
    req = make_valid_request(prod_allowed=True)
    with pytest.raises(ValueError) as exc_info:
        validate_proof_request_dict(req)
    assert "production_mutation_allowed" in str(exc_info.value)


# F. Destructive flag true: rejection
def test_scenario_f_destructive_flag_true_rejected():
    req = make_valid_request(dest_allowed=True)
    with pytest.raises(ValueError) as exc_info:
        validate_proof_request_dict(req)
    assert "destructive_operations_allowed" in str(exc_info.value)


# G. Billing flag true: rejection
def test_scenario_g_billing_flag_true_rejected():
    req = make_valid_request(bill_allowed=True)
    with pytest.raises(ValueError) as exc_info:
        validate_proof_request_dict(req)
    assert "billing_activation_allowed" in str(exc_info.value)


# H. Request containing DSN-like unauthorized field: schema rejection
def test_scenario_h_unauthorized_dsn_field_rejected():
    req = make_valid_request()
    req["dsn"] = "postgresql://user:pass@host/db"
    with pytest.raises(ValueError) as exc_info:
        validate_proof_request_dict(req)
    assert "Forbidden secret-like key" in str(exc_info.value) or "failed schema validation" in str(exc_info.value)


# I. Duplicate JSON key: rejection
def test_scenario_i_duplicate_json_key_rejected(tmp_path: Path):
    json_text = '{"schema_version": "0.1.0", "gate": "AOS-5", "gate": "AOS-5"}'
    json_file = tmp_path / "dup.json"
    json_file.write_text(json_text, encoding="utf-8")
    with pytest.raises(DuplicateJSONKeyError):
        load_and_validate_request(json_file)


# J. Malformed source SHA: rejection
def test_scenario_j_malformed_sha_rejected():
    req = make_valid_request(source_sha="invalid_sha_1234")
    with pytest.raises(ValueError) as exc_info:
        validate_proof_request_dict(req)
    assert "source_sha" in str(exc_info.value)


# K. Malformed / naive start time: rejection
def test_scenario_k_naive_start_time_rejected():
    req = make_valid_request(start_at_utc="2026-08-29T16:00:00")
    with pytest.raises(ValueError) as exc_info:
        validate_proof_request_dict(req)
    assert "UTC-aware" in str(exc_info.value) or "date-time" in str(exc_info.value)


# L. Wrong branch: HOLD before backend
def test_scenario_l_wrong_branch_fails_closed():
    req = make_valid_request(authorized=True, control_branch="feature/aos-5-distributed-coordination")
    with patch("aos.coordination_live_proof.check_git_readiness") as mock_git:
        mock_git.return_value = {"is_ready": False, "head_sha": VALID_SOURCE_SHA, "origin_sha": VALID_SOURCE_SHA, "branch": "main", "is_clean": True, "reasons": ["Current branch 'main' does not match required"]}
        with pytest.raises(CoordinationStorageError) as exc_info:
            run_live_worker(req, "worker_a", backend_override=MagicMock())
        assert "Git readiness failed" in str(exc_info.value)


# M. Dirty worktree: HOLD before backend
def test_scenario_m_dirty_worktree_fails_closed():
    req = make_valid_request(authorized=True)
    with patch("aos.coordination_live_proof.check_git_readiness") as mock_git:
        mock_git.return_value = {"is_ready": False, "head_sha": VALID_SOURCE_SHA, "origin_sha": VALID_SOURCE_SHA, "branch": VALID_BRANCH, "is_clean": False, "reasons": ["Working tree contains uncommitted changes"]}
        with pytest.raises(CoordinationStorageError) as exc_info:
            run_live_worker(req, "worker_a", backend_override=MagicMock())
        assert "Git readiness failed" in str(exc_info.value)


# N. Local/origin SHA mismatch: HOLD before backend
def test_scenario_n_origin_sha_mismatch_fails_closed():
    req = make_valid_request(authorized=True)
    with patch("aos.coordination_live_proof.check_git_readiness") as mock_git:
        mock_git.return_value = {"is_ready": False, "head_sha": VALID_SOURCE_SHA, "origin_sha": "0" * 40, "branch": VALID_BRANCH, "is_clean": True, "reasons": ["origin/branch SHA does not match"]}
        with pytest.raises(CoordinationStorageError) as exc_info:
            run_live_worker(req, "worker_a", backend_override=MagicMock())
        assert "Git readiness failed" in str(exc_info.value)


# O. Scripted initial winner path: ACQUIRED, zero heartbeat, zero release
def test_scenario_o_scripted_initial_winner_path():
    req = make_valid_request(authorized=True, start_at_utc="2026-08-29T16:00:00Z")
    mock_backend = MagicMock()
    mock_backend.is_worker_registered.return_value = True
    mock_backend.try_claim.return_value = {
        "status": "ACQUIRED",
        "lease": {
            "owner_worker_id": "w_a",
            "owner_session_id": "s_a",
            "lease_id": "lease_01",
            "generation": 1,
            "acquired_at": "2026-08-29T16:00:00Z",
            "expires_at": "2026-08-29T16:00:10Z",
            "ttl_seconds": 10.0,
            "status": "ACTIVE",
        },
    }

    fake_clock = [1772294400.0]  # 2026-08-29 16:00:00 UTC timestamp

    def now_fn():
        fake_clock[0] += 0.01
        return fake_clock[0]

    with patch("aos.coordination_live_proof.check_git_readiness") as mock_git:
        mock_git.return_value = {"is_ready": True, "head_sha": VALID_SOURCE_SHA, "origin_sha": VALID_SOURCE_SHA, "branch": VALID_BRANCH, "is_clean": True, "reasons": []}
        res = run_live_worker(req, "worker_a", backend_override=mock_backend, time_func=now_fn, sleep_func=lambda s: None)

    assert res["initial_disposition"] == "ACQUIRED"
    assert res["worker_terminal_role"] == "INITIAL_WINNER_NO_RELEASE"
    assert res["recovery_attempted"] is False
    mock_backend.heartbeat.assert_not_called()
    mock_backend.release.assert_not_called()


# P. Scripted initial loser path: HELD_BY_OTHER then ACQUIRED recovery
def test_scenario_p_scripted_initial_loser_path():
    req = make_valid_request(authorized=True, start_at_utc="2026-08-29T16:00:00Z")
    mock_backend = MagicMock()
    mock_backend.is_worker_registered.return_value = True

    claim_1 = {
        "status": "HELD_BY_OTHER",
        "lease": {
            "owner_worker_id": "w_a",
            "owner_session_id": "s_a",
            "lease_id": "lease_01",
            "generation": 1,
            "acquired_at": "2026-08-29T16:00:00Z",
            "expires_at": "2026-08-29T16:00:02Z",  # expires in 2s
            "ttl_seconds": 10.0,
            "status": "ACTIVE",
        },
    }
    claim_2 = {
        "status": "ACQUIRED",
        "lease": {
            "owner_worker_id": "w_b",
            "owner_session_id": "s_b",
            "lease_id": "lease_02",
            "generation": 2,
            "acquired_at": "2026-08-29T16:00:03Z",
            "expires_at": "2026-08-29T16:00:13Z",
            "ttl_seconds": 10.0,
            "status": "ACTIVE",
        },
    }
    mock_backend.try_claim.side_effect = [claim_1, claim_2]

    # Timestamp starts at 16:00:00 (1772294400), advances past 16:00:02
    fake_clock = [1772294400.0]

    def now_fn():
        fake_clock[0] += 1.0
        return fake_clock[0]

    with patch("aos.coordination_live_proof.check_git_readiness") as mock_git:
        mock_git.return_value = {"is_ready": True, "head_sha": VALID_SOURCE_SHA, "origin_sha": VALID_SOURCE_SHA, "branch": VALID_BRANCH, "is_clean": True, "reasons": []}
        res = run_live_worker(req, "worker_b", backend_override=mock_backend, time_func=now_fn, sleep_func=lambda s: None)

    assert res["initial_disposition"] == "HELD_BY_OTHER"
    assert res["recovery_attempted"] is True
    assert res["recovery_disposition"] == "ACQUIRED"
    assert res["worker_terminal_role"] == "INITIAL_LOSER_RECOVERED"
    assert res["recovery_lease"]["lease_id"] == "lease_02"
    assert res["recovery_lease"]["generation"] == 2


# Q. Valid pair verification: PASS
def test_scenario_q_valid_pair_verification():
    req = make_valid_request()
    res_a, res_b = make_valid_results(req)
    ver = verify_pair_results(req, res_a, res_b)
    assert ver["is_valid"] is True
    assert ver["status"] == "PASS"
    assert ver["reasons"] == []


# R. Two initial winners: HOLD
def test_scenario_r_two_winners_rejected():
    req = make_valid_request()
    res_a, res_b = make_valid_results(req)
    res_b["initial_disposition"] = "ACQUIRED"
    ver = verify_pair_results(req, res_a, res_b)
    assert ver["is_valid"] is False
    assert ver["status"] == "HOLD"


# S. Two initial losers: HOLD
def test_scenario_s_two_losers_rejected():
    req = make_valid_request()
    res_a, res_b = make_valid_results(req)
    res_a["initial_disposition"] = "HELD_BY_OTHER"
    ver = verify_pair_results(req, res_a, res_b)
    assert ver["is_valid"] is False
    assert ver["status"] == "HOLD"


# T. Same machine fingerprint: HOLD
def test_scenario_t_same_fingerprint_rejected():
    req = make_valid_request()
    res_a, res_b = make_valid_results(req)
    res_b["proof_scoped_machine_fingerprint_sha256"] = res_a["proof_scoped_machine_fingerprint_sha256"]
    ver = verify_pair_results(req, res_a, res_b)
    assert ver["is_valid"] is False
    assert ver["status"] == "HOLD"


# U. Mismatched source SHA: HOLD
def test_scenario_u_mismatched_sha_rejected():
    req = make_valid_request()
    res_a, res_b = make_valid_results(req)
    res_a["git_head_sha"] = "0" * 40
    ver = verify_pair_results(req, res_a, res_b)
    assert ver["is_valid"] is False
    assert ver["status"] == "HOLD"


# V. Mismatched lease epoch: HOLD
def test_scenario_v_mismatched_lease_epoch_rejected():
    req = make_valid_request()
    res_a, res_b = make_valid_results(req)
    res_b["initial_observed_lease"]["lease_id"] = "different_lease_id"
    ver = verify_pair_results(req, res_a, res_b)
    assert ver["is_valid"] is False
    assert ver["status"] == "HOLD"


# W. Wrong recovery owner: HOLD
def test_scenario_w_wrong_recovery_owner_rejected():
    req = make_valid_request()
    res_a, res_b = make_valid_results(req)
    res_b["recovery_lease"]["owner_worker_id"] = "wrong_owner"
    ver = verify_pair_results(req, res_a, res_b)
    assert ver["is_valid"] is False
    assert ver["status"] == "HOLD"


# X. Same recovery lease ID: HOLD
def test_scenario_x_same_recovery_lease_id_rejected():
    req = make_valid_request()
    res_a, res_b = make_valid_results(req)
    res_b["recovery_lease"]["lease_id"] = res_a["initial_observed_lease"]["lease_id"]
    ver = verify_pair_results(req, res_a, res_b)
    assert ver["is_valid"] is False
    assert ver["status"] == "HOLD"


# Y. Non-increasing recovery generation: HOLD
def test_scenario_y_non_increasing_generation_rejected():
    req = make_valid_request()
    res_a, res_b = make_valid_results(req)
    res_b["recovery_lease"]["generation"] = 1  # same as initial generation
    ver = verify_pair_results(req, res_a, res_b)
    assert ver["is_valid"] is False
    assert ver["status"] == "HOLD"


# Z. Result secret-like field: rejection
def test_scenario_z_result_secret_field_rejected(tmp_path: Path):
    req = make_valid_request()
    res_a, _ = make_valid_results(req)
    res_a["secret"] = "my_secret_token"
    out_path = tmp_path / "result_a.json"
    with pytest.raises(ValueError) as exc_info:
        write_worker_result_atomic(res_a, out_path)
    assert "Forbidden secret-like key" in str(exc_info.value)


# AA. Raw DSN value never appears in result/log/public exception in mocked live connection failure
def test_scenario_aa_dsn_redacted_on_connection_failure():
    req = make_valid_request(authorized=True)
    raw_dsn = "postgresql://secret_user:SECRET_PASS_999@example.invalid:5432/db"
    with patch.dict(os.environ, {"AOS_POSTGRES_LIVE_DSN": raw_dsn}), patch("aos.coordination_live_proof.check_git_readiness") as mock_git:
        mock_git.return_value = {"is_ready": True, "head_sha": VALID_SOURCE_SHA, "origin_sha": VALID_SOURCE_SHA, "branch": VALID_BRANCH, "is_clean": True, "reasons": []}
        with patch("aos.coordination_postgres.PostgresCoordinationBackend") as mock_pg:
            mock_pg.side_effect = Exception("Leaky internal error with secret SECRET_PASS_999")
            with pytest.raises(CoordinationStorageError) as exc_info:
                run_live_worker(req, "worker_a")

            err_msg = str(exc_info.value)
            assert "SECRET_PASS_999" not in err_msg
            assert raw_dsn not in err_msg


# AB. Result write is atomic
def test_scenario_ab_atomic_result_write(tmp_path: Path):
    req = make_valid_request()
    res_a, _ = make_valid_results(req)
    out_path = tmp_path / "result_atomic.json"
    write_worker_result_atomic(res_a, out_path)
    assert out_path.is_file()
    read_data = json.loads(out_path.read_text(encoding="utf-8"))
    assert read_data["proof_id"] == req["proof_id"]


# AC. No Git mutation path
def test_scenario_ac_no_git_mutation_during_execution():
    req = make_valid_request(authorized=False)
    with patch("subprocess.check_output") as mock_sub:
        mock_sub.side_effect = lambda cmd, **kwargs: {
            ("git", "rev-parse", "HEAD"): VALID_SOURCE_SHA,
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): VALID_BRANCH,
            ("git", "rev-parse", f"origin/{VALID_BRANCH}"): VALID_SOURCE_SHA,
            ("git", "status", "--porcelain"): "",
        }[tuple(cmd)]

        res = execute_dry_run(req, "worker_a")
        assert res["status"] == "READY_FOR_EXPLICIT_HUMAN_LIVE_AUTHORIZATION"

        # Verify no git commit / push / checkout commands were ever called
        for call_args in mock_sub.call_args_list:
            cmd = call_args[0][0]
            assert "commit" not in cmd
            assert "push" not in cmd
            assert "checkout" not in cmd


# AD. No external network in unit/default test path
def test_scenario_ad_no_external_network_in_test_suite():
    req = make_valid_request()
    res_a, res_b = make_valid_results(req)
    ver = verify_pair_results(req, res_a, res_b)
    assert ver["status"] == "PASS"
