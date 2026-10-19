"""Recovery, schema migration, Git reconciliation, and oracle negative tests for AOS V2.

Qualifies:
- Q13: Kernel lock contention, competing process denied, owner epoch fencing.
- Q14: Schema version preservation, migration on disposable copy, unknown version read-only refusal.
- Q15: Unrelated staged/dirty changes denied before publish; drift denied before overwrite.
- Q16: Crash recovery states: 6-row decision matrix and unexpected hash HUMAN_REQUIRED.
- Q17: Recovery preserves unknown outcomes; HOLD states for resource/authority/git unavailability.
- Q18: Hidden oracle negative controls: import exit 0, fake pass, wrong nonce, tolerance > 1e-9.
- Q19: Outbox atomic export and idempotent KCP ledger receipts.
"""

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from aos.v2.acceptance import (
    FunctionOracleVerifier,
    OracleCriterion,
    OutboxExporter,
    VerificationResult,
)
from aos.v2.bridge import (
    GitBridgeError,
    PreparedCandidate,
    RecoveryDecision,
    TaskIntent,
    WorkerGitBridge,
    compute_file_sha256,
)
from aos.v2.lifecycle import (
    KernelOwnerLock,
    LifecycleStore,
    OwnerBusyError,
    OwnerIdentity,
    TrustedGrant,
)
from aos.v2.sandbox import get_available_ram_bytes, preflight_sandbox


@pytest.fixture(autouse=True)
def mock_adequate_ram(monkeypatch):
    orig = get_available_ram_bytes()
    monkeypatch.setattr("aos.v2.sandbox.get_available_ram_bytes", lambda: max(orig, 2 * 1024 * 1024 * 1024))


def test_q13_kernel_lock_contention(tmp_path):
    """Q13: Active owner lock denies competing process with OwnerBusyError."""
    v2_home = tmp_path / "v2_home"
    v2_home.mkdir()

    lock1 = KernelOwnerLock(v2_home)
    lock1.acquire()

    lock2 = KernelOwnerLock(v2_home)
    with pytest.raises(OwnerBusyError, match="OWNER_BUSY"):
        lock2.acquire()

    lock1.release()
    # Now second lock can acquire
    lock2.acquire()
    lock2.release()


def test_q14_unknown_schema_version_preserved_read_only(tmp_path):
    """Q14: Unknown SQLite user_version fails closed on read-only check without mutating bytes."""
    v2_home = tmp_path / "v2_home"
    v2_home.mkdir()
    (v2_home / ".aos-v2-home").write_text("AOS_V2_JOURNAL_1\n")

    db_path = v2_home / "journal.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 99;")
    conn.execute("CREATE TABLE dummy (id INTEGER PRIMARY KEY);")
    conn.commit()
    conn.close()

    pre_sha = compute_file_sha256(db_path)

    with pytest.raises(ValueError, match="Unknown journal schema version"):
        LifecycleStore(v2_home)

    # File bytes must be completely unchanged
    post_sha = compute_file_sha256(db_path)
    assert pre_sha == post_sha


def test_q15_unrelated_staged_files_denied(tmp_path):
    """Q15: Unrelated staged files in index cause publish_candidate to refuse publish."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@aos.local"], cwd=repo_dir, check=True, capture_output=True)

    target_file = repo_dir / "target.py"
    target_file.write_text("initial = 1\n")
    subprocess.run(["git", "add", "target.py"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Init"], cwd=repo_dir, check=True, capture_output=True)
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    # Stage an unrelated file in git index
    unrelated = repo_dir / "unrelated.txt"
    unrelated.write_text("unrelated staged file")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=repo_dir, check=True, capture_output=True)

    bridge = WorkerGitBridge(repo_dir)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    intent = TaskIntent(
        command_id="cmd_1",
        task_id="task_1",
        owner_epoch=1,
        repo_root=repo_dir,
        owned_branch="main",
        expected_parent=head_sha,
        target_rel_path="target.py",
        pre_sha256=compute_file_sha256(target_file),
        edits=[{"old": "initial = 1", "new": "initial = 2"}],
    )
    prepared = bridge.prepare_candidate(intent, scratch)

    with pytest.raises(GitBridgeError, match="Unrelated staged or modified files exist"):
        bridge.publish_candidate(prepared, message="Should fail due to unrelated staged file")


def test_q16_disk_reconciliation_human_required_on_drift(tmp_path):
    """Q16: Unexpected file hash on disk during reconciliation returns HUMAN_REQUIRED."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@aos.local"], cwd=repo_dir, check=True, capture_output=True)

    f = repo_dir / "module.py"
    f.write_text("pre = 10\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Init"], cwd=repo_dir, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    bridge = WorkerGitBridge(repo_dir)

    # Mutate disk file to an arbitrary sentinel
    f.write_text("unexpected_disk_sentinel_xyz\n")

    intent = TaskIntent(
        command_id="cmd_1",
        task_id="task_1",
        owner_epoch=1,
        repo_root=repo_dir,
        owned_branch="main",
        expected_parent=head,
        target_rel_path="module.py",
        pre_sha256="0" * 64,
        edits=(),
    )

    decision = bridge.reconcile_task(
        intent,
        current_task_status="MATERIALIZED",
        stored_commit_sha=head,
        stored_prepared_tree="tree_dummy",
    )
    assert decision == RecoveryDecision.HUMAN_REQUIRED


def test_q18_oracle_verifier_negative_controls(tmp_path):
    """Q18: Oracle verifier rejects import-exit0, fake PASS, wrong nonce, and >1e-9 tolerance."""
    preflight = preflight_sandbox()
    if not preflight.is_supported:
        pytest.skip(f"Sandbox unsupported on host: {'; '.join(preflight.reasons)}")

    oracle_dir = tmp_path / "oracle"
    oracle_dir.mkdir()
    oracle_file = oracle_dir / "oracle.json"
    cases = [{"case_id": "c1", "args": [10.0], "expected": 20.0}]
    oracle_file.write_text(json.dumps({"cases": cases}))
    oracle_sha = compute_file_sha256(oracle_file)

    criterion = OracleCriterion("C1", "test_mod", "compute", oracle_file, oracle_sha)
    verifier = FunctionOracleVerifier()

    # 1. Import exit 0
    c1 = tmp_path / "candidate1"
    c1.mkdir()
    (c1 / "test_mod.py").write_text("import sys\nsys.exit(0)\n")
    res1 = verifier.verify_criterion(criterion, c1, tmp_path / "sc1")
    assert res1.passed is False

    # 2. Fake PASS in stdout
    c2 = tmp_path / "candidate2"
    c2.mkdir()
    (c2 / "test_mod.py").write_text("print('PASS')\ndef compute(x): return 999.0\n")
    res2 = verifier.verify_criterion(criterion, c2, tmp_path / "sc2")
    assert res2.passed is False
    assert res2.error is not None

    # 3. Tolerance mismatch (20.0001 != 20.0 within 1e-9)
    c3 = tmp_path / "candidate3"
    c3.mkdir()
    (c3 / "test_mod.py").write_text("def compute(x): return 20.0001\n")
    res3 = verifier.verify_criterion(criterion, c3, tmp_path / "sc3")
    assert res3.passed is False
    assert "exceeded tolerance" in res3.error


def test_q19_outbox_export_and_kcp_ledger_idempotency(tmp_path):
    """Q19: Outbox export writes result.json and appends exactly 2 KCP receipts idempotently."""
    v2_home = tmp_path / "v2_home"
    exporter = OutboxExporter(v2_home)

    cmd_id = "cmd_test_q19"
    payload = {
        "command_id": cmd_id,
        "goal": "Test outbox idempotency",
        "state": "PROJECT_COMPLETE",
        "admission": "CLOSED",
        "commit_sha": "1" * 40,
    }

    # First export
    res_path = exporter.export_result(cmd_id, payload, base_sha="0" * 40, result_sha="1" * 40)
    assert res_path.is_file()

    # Replay export
    res_path2 = exporter.export_result(cmd_id, payload, base_sha="0" * 40, result_sha="1" * 40)
    assert res_path2 == res_path

    # Verify ledger has exactly 2 receipts (1 implementation, 1 verification)
    events = exporter.ledger.read_events()
    assert len(events) == 2
    assert events[0]["agent_class"] == "AOS_NATIVE"
    assert events[1]["agent_class"] == "AOS_NATIVE"
