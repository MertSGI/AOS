"""Tests for AOS V2 independent acceptance and outbox export."""

import json
from pathlib import Path

import pytest

from aos.v2.acceptance import (
    FunctionOracleVerifier,
    OracleCriterion,
    OutboxExporter,
    VerificationResult,
)
from aos.v2.bridge import compute_file_sha256
from aos.v2.sandbox import get_available_ram_bytes


@pytest.fixture(autouse=True)
def mock_adequate_ram(monkeypatch):
    orig = get_available_ram_bytes()
    monkeypatch.setattr("aos.v2.sandbox.get_available_ram_bytes", lambda: max(orig, 2 * 1024 * 1024 * 1024))


@pytest.fixture
def sample_oracle(tmp_path):
    oracle_dir = tmp_path / "evaluator"
    oracle_dir.mkdir()
    oracle_path = oracle_dir / "oracle.json"

    # 5 samples + 12 cases = 17 test cases for discount calculation
    cases = []
    # Test cases: (price, rate) -> expected
    for i in range(1, 18):
        price = float(i * 10)
        rate = round(0.05 * (i % 10), 2)
        expected = round(price * (1.0 - rate), 6)
        cases.append({
            "case_id": f"case_{i}",
            "args": [price, rate],
            "expected": expected,
        })

    data = {
        "criterion_id": "CRIT_DISCOUNT_1",
        "module": "discount",
        "symbol": "apply_discount",
        "tolerance": 1e-9,
        "cases": cases,
    }
    oracle_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    oracle_sha = compute_file_sha256(oracle_path)
    return oracle_path, oracle_sha


def test_oracle_verifier_positive_match(tmp_path, sample_oracle):
    oracle_path, oracle_sha = sample_oracle

    # Create candidate module with correct implementation
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "discount.py").write_text(
        "def apply_discount(price, rate):\n"
        "    return round(price * (1.0 - rate), 6)\n"
    )

    criterion = OracleCriterion(
        criterion_id="CRIT_DISCOUNT_1",
        module_name="discount",
        symbol_name="apply_discount",
        oracle_file_path=oracle_path,
        expected_oracle_sha=oracle_sha,
    )

    verifier = FunctionOracleVerifier()
    res = verifier.verify_criterion(criterion, candidate, tmp_path / "scratch")

    assert isinstance(res, VerificationResult)
    assert res.passed is True
    assert res.error is None
    assert res.details["case_count"] == 17
    assert res.details["max_diff"] <= 1e-9


def test_oracle_verifier_rejects_value_mismatch(tmp_path, sample_oracle):
    oracle_path, oracle_sha = sample_oracle

    # Create candidate module that produces wrong calculation
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "discount.py").write_text(
        "def apply_discount(price, rate):\n"
        "    return price - 1.0  # Incorrect formula\n"
    )

    criterion = OracleCriterion(
        criterion_id="CRIT_DISCOUNT_1",
        module_name="discount",
        symbol_name="apply_discount",
        oracle_file_path=oracle_path,
        expected_oracle_sha=oracle_sha,
    )

    verifier = FunctionOracleVerifier()
    res = verifier.verify_criterion(criterion, candidate, tmp_path / "scratch")

    assert res.passed is False
    assert "exceeded tolerance" in res.error
    assert len(res.details.get("mismatches", [])) > 0


def test_oracle_verifier_denies_import_time_exit_and_fake_pass(tmp_path, sample_oracle):
    oracle_path, oracle_sha = sample_oracle

    # Counterexample from contract: prints PASS and exits 0 on import!
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "discount.py").write_text(
        "import sys\n"
        "print('PASS - all tests succeeded!')\n"
        "sys.exit(0)\n"
    )

    criterion = OracleCriterion(
        criterion_id="CRIT_DISCOUNT_1",
        module_name="discount",
        symbol_name="apply_discount",
        oracle_file_path=oracle_path,
        expected_oracle_sha=oracle_sha,
    )

    verifier = FunctionOracleVerifier()
    res = verifier.verify_criterion(criterion, candidate, tmp_path / "scratch")

    # MUST be denied! Printed PASS alone does NOT deceive the oracle verifier
    assert res.passed is False
    assert res.error is not None


def test_oracle_verifier_rejects_oracle_hash_mismatch(tmp_path, sample_oracle):
    oracle_path, _ = sample_oracle

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "discount.py").write_text("def apply_discount(p, r): return p\n")

    criterion = OracleCriterion(
        criterion_id="CRIT_DISCOUNT_1",
        module_name="discount",
        symbol_name="apply_discount",
        oracle_file_path=oracle_path,
        expected_oracle_sha="1111111111111111111111111111111111111111111111111111111111111111",
    )

    verifier = FunctionOracleVerifier()
    res = verifier.verify_criterion(criterion, candidate, tmp_path / "scratch")

    assert res.passed is False
    assert "Oracle SHA256 mismatch" in res.error


def test_outbox_exporter_records_receipts_idempotently(tmp_path):
    v2_home = tmp_path / "v2_home"
    exporter = OutboxExporter(v2_home)

    command_id = "cmd-goal-1"
    payload = {
        "command_id": command_id,
        "goal": "repair discount",
        "status": "PROJECT_COMPLETE",
        "commit_sha": "a" * 40,
        "finished_at": "2026-10-19T10:00:00Z",
    }

    base_sha = "b" * 40
    result_sha = "c" * 40

    # First export
    result_path = exporter.export_result(
        command_id, payload, base_sha=base_sha, result_sha=result_sha
    )
    assert result_path.is_file()
    assert (v2_home / "knowledge" / "ledger.jsonl").exists()

    # Second export (idempotent replay)
    result_path2 = exporter.export_result(
        command_id, payload, base_sha=base_sha, result_sha=result_sha
    )
    assert result_path2 == result_path
    events = exporter.ledger.read_events()
    # Exactly one implementation receipt and one verification receipt
    assert len(events) == 2
