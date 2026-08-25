"""Tests for verification workspace isolation and disposable copy management."""

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import pytest

from aos.candidate_store import compute_file_sha256
from aos.verification_workspace import (
    VerificationWorkspaceCopy,
    VerificationWorkspaceError,
    inspect_workspace_boundary_state,
    verify_copy_zero_remotes,
)


@pytest.fixture
def git_worker_workspace():
    """Create a temporary git repository simulating a worker workspace."""
    tmp_dir = tempfile.mkdtemp(prefix="aos_test_git_ws_")
    ws_path = Path(tmp_dir)

    subprocess.run(["git", "init"], cwd=str(ws_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "AOS Tester"], cwd=str(ws_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@aos.test"], cwd=str(ws_path), check=True, capture_output=True)

    # Initial commit
    base_file = ws_path / "README.md"
    base_file.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(ws_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(ws_path), check=True, capture_output=True)

    # Worker modification (uncommitted working tree mutation)
    src_file = ws_path / "src" / "target.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("def run():\n    return 42\n", encoding="utf-8")

    yield ws_path

    if ws_path.exists():
        shutil.rmtree(ws_path, ignore_errors=True)


class TestVerificationWorkspace:
    def test_fresh_copy_matches_worker_boundary(self, git_worker_workspace):
        """Verification copy HEAD, branch, changed_paths, and file hashes match original."""
        orig_state = inspect_workspace_boundary_state(git_worker_workspace)

        copy_mgr = VerificationWorkspaceCopy(git_worker_workspace, check_id="test_check_1")
        with copy_mgr as copy_dir:
            assert copy_dir.exists()
            assert copy_dir != git_worker_workspace

            copy_state = inspect_workspace_boundary_state(copy_dir)
            assert copy_state["head"] == orig_state["head"]
            assert copy_state["branch"] == orig_state["branch"]
            assert copy_state["changed_paths"] == orig_state["changed_paths"]
            assert copy_state["file_states"] == orig_state["file_states"]

        assert copy_mgr.copy_dir is None
        assert not copy_dir.exists()

    def test_copy_has_zero_git_remotes(self, git_worker_workspace):
        """Verification copy has zero git remotes."""
        copy_mgr = VerificationWorkspaceCopy(git_worker_workspace, check_id="remotes_check")
        with copy_mgr as copy_dir:
            res = subprocess.run(["git", "-C", str(copy_dir), "remote"], capture_output=True, text=True)
            assert res.returncode == 0
            assert res.stdout.strip() == ""
            # verify_copy_zero_remotes helper passes
            verify_copy_zero_remotes(copy_dir)

    def test_verification_copy_mutation_does_not_alter_original_worker_workspace(self, git_worker_workspace):
        """A & B & C: Mutations in verification copy do not alter original worker workspace."""
        orig_bytes = (git_worker_workspace / "src" / "target.py").read_bytes()
        orig_state = inspect_workspace_boundary_state(git_worker_workspace)

        copy_mgr = VerificationWorkspaceCopy(git_worker_workspace, check_id="mutation_check")
        with copy_mgr as copy_dir:
            # 1. Create untracked .aos-runtime/live-proof/manifest.json in verification copy
            art_file = copy_dir / ".aos-runtime" / "live-proof" / "manifest-123.json"
            art_file.parent.mkdir(parents=True, exist_ok=True)
            art_file.write_text('{"test": "proof"}', encoding="utf-8")

            # 2. Create arbitrary untracked file
            untracked = copy_dir / "untracked_side_effect.txt"
            untracked.write_text("pytest runtime cache", encoding="utf-8")

            # 3. Mutate an existing allowed file
            (copy_dir / "src" / "target.py").write_text("def run():\n    return 999\n", encoding="utf-8")

            # Confirm mutations exist in copy
            assert art_file.exists()
            assert untracked.exists()
            assert (copy_dir / "src" / "target.py").read_bytes() != orig_bytes

        # Check ORIGINAL worker workspace
        assert not (git_worker_workspace / ".aos-runtime").exists()
        assert not (git_worker_workspace / "untracked_side_effect.txt").exists()
        assert (git_worker_workspace / "src" / "target.py").read_bytes() == orig_bytes

        final_orig_state = inspect_workspace_boundary_state(git_worker_workspace)
        assert final_orig_state == orig_state

    def test_symlink_in_source_fails_closed(self, git_worker_workspace):
        """G. symlink in source workspace causes fail closed on copy creation."""
        symlink_path = git_worker_workspace / "link.txt"
        target_path = git_worker_workspace / "README.md"
        try:
            symlink_path.symlink_to(target_path)
        except OSError:
            pytest.skip("Symlink creation not supported or permitted on this OS/user")

        copy_mgr = VerificationWorkspaceCopy(git_worker_workspace, check_id="symlink_check")
        with pytest.raises(VerificationWorkspaceError, match="Symlinks are not permitted"):
            copy_mgr.create_and_verify()
