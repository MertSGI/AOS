"""Tests for AOS V2 WorkerGitBridge and recovery decision table."""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from aos.v2.bridge import (
    GitBridgeError,
    PreparedCandidate,
    PublishedCommit,
    RecoveryDecision,
    TaskIntent,
    WorkerGitBridge,
    compute_file_sha256,
)


@pytest.fixture
def disposable_repo(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "AOS Tester"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@aos.local"], cwd=repo_dir, check=True, capture_output=True)

    src_dir = repo_dir / "src"
    src_dir.mkdir()
    target_file = src_dir / "calc.py"
    target_file.write_text("def calculate(x):\n    return x + 1\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)

    # Create owned disposable branch
    subprocess.run(["git", "checkout", "-b", "aos-v2/task-1"], cwd=repo_dir, check=True, capture_output=True)

    return repo_dir


def test_prepare_candidate_stages_in_private_index(disposable_repo, tmp_path):
    bridge = WorkerGitBridge(disposable_repo)
    parent_sha = bridge.get_ref_sha("refs/heads/aos-v2/task-1")
    assert parent_sha is not None

    target_file = disposable_repo / "src" / "calc.py"
    pre_sha = compute_file_sha256(target_file)

    intent = TaskIntent(
        command_id="cmd-1",
        task_id="task-1",
        owner_epoch=1,
        repo_root=disposable_repo,
        owned_branch="aos-v2/task-1",
        expected_parent=parent_sha,
        target_rel_path="src/calc.py",
        pre_sha256=pre_sha,
        edits=[{"old": "return x + 1", "new": "return x * 2"}],
    )

    scratch = tmp_path / "scratch"
    prepared = bridge.prepare_candidate(intent, scratch)

    assert isinstance(prepared, PreparedCandidate)
    assert prepared.post_sha256 != pre_sha
    assert (scratch / "private.index").exists()
    assert (prepared.candidate_dir / "src" / "calc.py").read_text() == "def calculate(x):\n    return x * 2\n"

    # User worktree MUST be untouched at this stage!
    assert target_file.read_text() == "def calculate(x):\n    return x + 1\n"
    assert compute_file_sha256(target_file) == pre_sha


def test_prepare_candidate_rejects_prehash_mismatch(disposable_repo, tmp_path):
    bridge = WorkerGitBridge(disposable_repo)
    parent_sha = bridge.get_ref_sha("refs/heads/aos-v2/task-1")

    intent = TaskIntent(
        command_id="cmd-1",
        task_id="task-1",
        owner_epoch=1,
        repo_root=disposable_repo,
        owned_branch="aos-v2/task-1",
        expected_parent=parent_sha,
        target_rel_path="src/calc.py",
        pre_sha256="0000000000000000000000000000000000000000000000000000000000000000",
        edits=[{"old": "return x + 1", "new": "return x * 2"}],
    )

    scratch = tmp_path / "scratch"
    with pytest.raises(GitBridgeError, match="Source pre-hash mismatch"):
        bridge.prepare_candidate(intent, scratch)


def test_publish_candidate_and_materialization(disposable_repo, tmp_path):
    bridge = WorkerGitBridge(disposable_repo)
    parent_sha = bridge.get_ref_sha("refs/heads/aos-v2/task-1")
    target_file = disposable_repo / "src" / "calc.py"
    pre_sha = compute_file_sha256(target_file)

    intent = TaskIntent(
        command_id="cmd-1",
        task_id="task-1",
        owner_epoch=1,
        repo_root=disposable_repo,
        owned_branch="aos-v2/task-1",
        expected_parent=parent_sha,
        target_rel_path="src/calc.py",
        pre_sha256=pre_sha,
        edits=[{"old": "return x + 1", "new": "return x * 2"}],
    )

    scratch = tmp_path / "scratch"
    prepared = bridge.prepare_candidate(intent, scratch)
    published = bridge.publish_candidate(prepared, message="Implement calculation fix")

    assert isinstance(published, PublishedCommit)
    new_head = bridge.get_ref_sha("refs/heads/aos-v2/task-1")
    assert new_head == published.commit_sha
    assert new_head != parent_sha

    # Check commit log for trailers
    log_res = subprocess.run(
        ["git", "log", "-n", "1", "--format=%B", published.commit_sha],
        cwd=disposable_repo, capture_output=True, text=True, check=True
    )
    msg = log_res.stdout
    assert "AOS-Command: cmd-1" in msg
    assert "AOS-Task: task-1" in msg
    assert "AOS-Epoch: 1" in msg
    assert f"AOS-PreHash: {pre_sha}" in msg
    assert f"AOS-PostHash: {prepared.post_sha256}" in msg

    # Materialize source file
    bridge.materialize_source(prepared)
    assert compute_file_sha256(target_file) == prepared.post_sha256
    assert target_file.read_text() == "def calculate(x):\n    return x * 2\n"


def test_cas_update_ref_rejects_diverged_parent(disposable_repo, tmp_path):
    bridge = WorkerGitBridge(disposable_repo)
    parent_sha = bridge.get_ref_sha("refs/heads/aos-v2/task-1")
    target_file = disposable_repo / "src" / "calc.py"
    pre_sha = compute_file_sha256(target_file)

    intent = TaskIntent(
        command_id="cmd-1",
        task_id="task-1",
        owner_epoch=1,
        repo_root=disposable_repo,
        owned_branch="aos-v2/task-1",
        expected_parent=parent_sha,
        target_rel_path="src/calc.py",
        pre_sha256=pre_sha,
        edits=[{"old": "return x + 1", "new": "return x * 2"}],
    )

    scratch = tmp_path / "scratch"
    prepared = bridge.prepare_candidate(intent, scratch)

    # Simulate another commit advancing the branch in background
    (disposable_repo / "other.txt").write_text("concurrent edit\n")
    subprocess.run(["git", "add", "other.txt"], cwd=disposable_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Interfering commit"], cwd=disposable_repo, check=True, capture_output=True)

    # CAS update-ref must fail because parent_sha is no longer current ref
    with pytest.raises(GitBridgeError, match="Git command failed"):
        bridge.publish_candidate(prepared)


def test_reconcile_task_recovery_decision_table(disposable_repo, tmp_path):
    bridge = WorkerGitBridge(disposable_repo)
    parent_sha = bridge.get_ref_sha("refs/heads/aos-v2/task-1")
    target_file = disposable_repo / "src" / "calc.py"
    pre_sha = compute_file_sha256(target_file)

    intent = TaskIntent(
        command_id="cmd-1",
        task_id="task-1",
        owner_epoch=1,
        repo_root=disposable_repo,
        owned_branch="aos-v2/task-1",
        expected_parent=parent_sha,
        target_rel_path="src/calc.py",
        pre_sha256=pre_sha,
        edits=[{"old": "return x + 1", "new": "return x * 2"}],
    )

    # Row 1: NO_EFFECT_PROVEN (no commit, no tree, pre-hashes intact)
    decision = bridge.reconcile_task(
        intent,
        current_task_status="IN_PROGRESS",
        stored_commit_sha=None,
        stored_prepared_tree=None,
    )
    assert decision == RecoveryDecision.NO_EFFECT_PROVEN

    # Row 2: CONTINUE_PREPARED (prepared tree exists, branch and file still at pre-hash)
    decision = bridge.reconcile_task(
        intent,
        current_task_status="IN_PROGRESS",
        stored_commit_sha=None,
        stored_prepared_tree="tree_abc123",
    )
    assert decision == RecoveryDecision.CONTINUE_PREPARED

    # Publish commit to test rows 3, 4, 5
    scratch = tmp_path / "scratch"
    prepared = bridge.prepare_candidate(intent, scratch)
    published = bridge.publish_candidate(prepared)

    # Row 3: FINISH_MATERIALIZATION (commit published on branch, file still at pre-hash on disk)
    decision = bridge.reconcile_task(
        intent,
        current_task_status="IN_PROGRESS",
        stored_commit_sha=published.commit_sha,
        stored_prepared_tree=prepared.tree_sha,
    )
    assert decision == RecoveryDecision.FINISH_MATERIALIZATION

    # Materialize source file
    bridge.materialize_source(prepared)

    # Row 4: ACCEPT_EXISTING (commit published, source materialized, not yet accepted)
    decision = bridge.reconcile_task(
        intent,
        current_task_status="IN_PROGRESS",
        stored_commit_sha=published.commit_sha,
        stored_prepared_tree=prepared.tree_sha,
    )
    assert decision == RecoveryDecision.ACCEPT_EXISTING

    # Row 5: SKIP_ACCEPTED (already accepted and all hashes agree)
    decision = bridge.reconcile_task(
        intent,
        current_task_status="ACCEPTED",
        stored_commit_sha=published.commit_sha,
        stored_prepared_tree=prepared.tree_sha,
    )
    assert decision == RecoveryDecision.SKIP_ACCEPTED

    # Row 6: HUMAN_REQUIRED (unexpected drift: e.g. unknown commit on branch)
    # Simulate someone pushing an unexpected commit
    (disposable_repo / "drift.txt").write_text("drift")
    subprocess.run(["git", "add", "drift.txt"], cwd=disposable_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Drift commit"], cwd=disposable_repo, check=True, capture_output=True)

    decision = bridge.reconcile_task(
        intent,
        current_task_status="IN_PROGRESS",
        stored_commit_sha=published.commit_sha,
        stored_prepared_tree=prepared.tree_sha,
    )
    assert decision == RecoveryDecision.HUMAN_REQUIRED
