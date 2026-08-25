"""Tests for machine-local candidate store and verified candidate persistence."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import pytest

from aos.candidate_store import (
    CANDIDATE_STORE_CONTRACT_VERSION,
    CandidateStoreError,
    canonical_json_bytes,
    compute_file_sha256,
    get_default_candidate_store_dir,
    persist_verified_candidate,
)
from aos.controlled_execution import ControlledExecutionEngine
from aos.validate import validate_document


@pytest.fixture
def fake_workspace():
    """Create a temporary workspace directory with sample files."""
    tmp_dir = tempfile.mkdtemp(prefix="aos_test_ws_")
    ws_path = Path(tmp_dir)

    # Create dummy files
    f1 = ws_path / "src" / "sample.py"
    f1.parent.mkdir(parents=True, exist_ok=True)
    f1.write_text("print('hello world')\n", encoding="utf-8")

    f2 = ws_path / "docs" / "readme.txt"
    f2.parent.mkdir(parents=True, exist_ok=True)
    f2.write_text("documentation content\n", encoding="utf-8")

    yield ws_path

    if ws_path.exists():
        shutil.rmtree(ws_path, ignore_errors=True)


@pytest.fixture
def fake_candidate_store():
    """Create a temporary candidate store directory."""
    tmp_dir = tempfile.mkdtemp(prefix="aos_test_store_")
    store_path = Path(tmp_dir)

    yield store_path

    if store_path.exists():
        shutil.rmtree(store_path, ignore_errors=True)


class TestCandidateStore:
    def test_successful_candidate_persistence_creates_opaque_id(self, fake_workspace, fake_candidate_store):
        """A. Successful candidate persistence creates opaque candidate ID."""
        res = persist_verified_candidate(
            workspace_path=str(fake_workspace),
            project_id="aos",
            task_id="AOS4-REF-001",
            gate="AOS-4",
            control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
            execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            worker_branch="aos/aos4-ref-001",
            initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            changed_paths=["src/sample.py"],
            candidate_store_dir=str(fake_candidate_store),
        )

        assert res["status"] == "PERSISTED"
        assert res["candidate_store_contract_version"] == CANDIDATE_STORE_CONTRACT_VERSION
        candidate_id = res["candidate_id"]
        assert candidate_id.startswith("cand_")
        assert len(candidate_id) == 21  # "cand_" + 16 hex chars
        assert "manifest_sha256" in res
        assert len(res["manifest_sha256"]) == 64

    def test_manifest_contains_no_absolute_path(self, fake_workspace, fake_candidate_store):
        """B. Manifest contains no absolute path."""
        res = persist_verified_candidate(
            workspace_path=str(fake_workspace),
            project_id="aos",
            task_id="AOS4-REF-001",
            gate="AOS-4",
            control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
            execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            worker_branch="aos/aos4-ref-001",
            initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            changed_paths=["src/sample.py"],
            candidate_store_dir=str(fake_candidate_store),
        )

        cand_dir = fake_candidate_store / res["candidate_id"]
        manifest_path = cand_dir / "manifest.json"
        manifest_raw = manifest_path.read_text(encoding="utf-8")
        manifest = json.loads(manifest_raw)

        # Check that paths inside manifest are strictly relative
        for p in manifest["changed_paths"]:
            assert not os.path.isabs(p["path"])
            assert not p["path"].startswith("/")
            assert not p["path"].startswith("\\")

        # Verify no usernames or absolute drive letters in raw manifest
        assert str(fake_workspace) not in manifest_raw
        assert str(fake_candidate_store) not in manifest_raw

    def test_exact_file_bytes_produce_expected_sha256(self, fake_workspace, fake_candidate_store):
        """C. Exact file bytes produce expected SHA256 in manifest."""
        sample_file = fake_workspace / "src" / "sample.py"
        expected_sha = hashlib.sha256(sample_file.read_bytes()).hexdigest()

        res = persist_verified_candidate(
            workspace_path=str(fake_workspace),
            project_id="aos",
            task_id="AOS4-REF-001",
            gate="AOS-4",
            control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
            execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            worker_branch="aos/aos4-ref-001",
            initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            changed_paths=["src/sample.py"],
            candidate_store_dir=str(fake_candidate_store),
        )

        cand_dir = fake_candidate_store / res["candidate_id"]
        manifest = json.loads((cand_dir / "manifest.json").read_text(encoding="utf-8"))
        entry = manifest["changed_paths"][0]
        assert entry["path"] == "src/sample.py"
        assert entry["state"] == "PRESENT"
        assert entry["sha256"] == expected_sha
        assert entry["size_bytes"] == sample_file.stat().st_size

    def test_deleted_path_produces_deleted_state(self, fake_workspace, fake_candidate_store):
        """D. Deleted path produces DELETED state in manifest."""
        res = persist_verified_candidate(
            workspace_path=str(fake_workspace),
            project_id="aos",
            task_id="AOS4-REF-001",
            gate="AOS-4",
            control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
            execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            worker_branch="aos/aos4-ref-001",
            initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            changed_paths=["deleted_file.txt"],
            candidate_store_dir=str(fake_candidate_store),
        )

        cand_dir = fake_candidate_store / res["candidate_id"]
        manifest = json.loads((cand_dir / "manifest.json").read_text(encoding="utf-8"))
        entry = manifest["changed_paths"][0]
        assert entry["path"] == "deleted_file.txt"
        assert entry["state"] == "DELETED"
        assert "sha256" not in entry

    def test_manifest_hash_deterministic_for_identical_candidate_content(self):
        """E. Manifest hash is deterministic for identical candidate content."""
        data1 = {
            "schema_version": "0.1.0",
            "candidate_id": "cand_123",
            "changed_paths": [{"path": "a.txt", "sha256": "abc", "size_bytes": 10, "state": "PRESENT"}],
            "created_at": "2026-08-24T18:00:00Z"
        }
        data2 = {
            "created_at": "2026-08-24T18:00:00Z",
            "changed_paths": [{"path": "a.txt", "sha256": "abc", "size_bytes": 10, "state": "PRESENT"}],
            "candidate_id": "cand_123",
            "schema_version": "0.1.0",
        }
        b1 = canonical_json_bytes(data1)
        b2 = canonical_json_bytes(data2)
        assert b1 == b2
        assert hashlib.sha256(b1).hexdigest() == hashlib.sha256(b2).hexdigest()

    def test_changing_candidate_content_changes_manifest_hash(self):
        """F. Changing candidate content changes manifest hash."""
        data1 = {
            "schema_version": "0.1.0",
            "candidate_id": "cand_123",
            "changed_paths": [{"path": "a.txt", "sha256": "abc", "size_bytes": 10, "state": "PRESENT"}],
            "created_at": "2026-08-24T18:00:00Z"
        }
        data2 = {
            "schema_version": "0.1.0",
            "candidate_id": "cand_123",
            "changed_paths": [{"path": "a.txt", "sha256": "different", "size_bytes": 10, "state": "PRESENT"}],
            "created_at": "2026-08-24T18:00:00Z"
        }
        assert canonical_json_bytes(data1) != canonical_json_bytes(data2)

    def test_candidate_store_env_override(self, fake_workspace, monkeypatch):
        """I. AOS_CANDIDATE_STORE_DIR override works."""
        override_dir = tempfile.mkdtemp(prefix="aos_override_store_")
        monkeypatch.setenv("AOS_CANDIDATE_STORE_DIR", override_dir)

        res = persist_verified_candidate(
            workspace_path=str(fake_workspace),
            project_id="aos",
            task_id="AOS4-REF-001",
            gate="AOS-4",
            control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
            execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            worker_branch="aos/aos4-ref-001",
            initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            changed_paths=["src/sample.py"],
        )

        target = Path(override_dir) / res["candidate_id"]
        assert target.is_dir()
        shutil.rmtree(override_dir, ignore_errors=True)

    def test_candidate_store_inside_source_repo_rejected(self, fake_workspace):
        """J. Candidate store directory inside source repo is rejected."""
        source_repo = tempfile.mkdtemp(prefix="aos_src_repo_")
        inside_store = Path(source_repo) / ".aos_candidates"

        with pytest.raises(CandidateStoreError, match="inside source repository"):
            persist_verified_candidate(
                workspace_path=str(fake_workspace),
                project_id="aos",
                task_id="AOS4-REF-001",
                gate="AOS-4",
                control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
                execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                worker_branch="aos/aos4-ref-001",
                initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                changed_paths=["src/sample.py"],
                source_repo_path=source_repo,
                candidate_store_dir=str(inside_store),
            )

        shutil.rmtree(source_repo, ignore_errors=True)

    def test_failed_persistence_cleans_partial_destination(self, fake_workspace, fake_candidate_store, monkeypatch):
        """L. Failed persistence cleans partial destination directory."""
        # Force an error during copying or manifest generation
        def _bad_copy(*args, **kwargs):
            raise IOError("Disk full mock")

        monkeypatch.setattr(shutil, "copytree", _bad_copy)

        with pytest.raises(CandidateStoreError, match="Failed to persist candidate"):
            persist_verified_candidate(
                workspace_path=str(fake_workspace),
                project_id="aos",
                task_id="AOS4-REF-001",
                gate="AOS-4",
                control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
                execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                worker_branch="aos/aos4-ref-001",
                initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                changed_paths=["src/sample.py"],
                candidate_store_dir=str(fake_candidate_store),
            )

        # Ensure no orphan directories remain in store
        entries = list(fake_candidate_store.iterdir())
        assert entries == []

    def test_changed_file_symlink_to_external_file_rejected(self, fake_workspace, fake_candidate_store):
        """A. changed-file symlink to an external file => CandidateStoreError, external target bytes never appear in candidate store."""
        external_file = fake_candidate_store.parent / "external_secret.txt"
        external_file.write_text("SUPER_SECRET_EXTERNAL_CONTENT\n", encoding="utf-8")

        symlink_path = fake_workspace / "src" / "linked_secret.py"
        try:
            os.symlink(str(external_file), str(symlink_path))
        except OSError:
            pytest.skip("Symlinks not supported on this platform/user privilege level")

        with pytest.raises(CandidateStoreError, match="Symlinks are not permitted"):
            persist_verified_candidate(
                workspace_path=str(fake_workspace),
                project_id="aos",
                task_id="AOS4-REF-001",
                gate="AOS-4",
                control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
                execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                worker_branch="aos/aos4-ref-001",
                initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                changed_paths=["src/linked_secret.py"],
                candidate_store_dir=str(fake_candidate_store),
            )

        # Ensure no external content bytes in candidate store
        for root, dirs, files in os.walk(fake_candidate_store):
            for f in files:
                content = (Path(root) / f).read_bytes()
                assert b"SUPER_SECRET_EXTERNAL_CONTENT" not in content

    def test_unchanged_workspace_symlink_rejected(self, fake_workspace, fake_candidate_store):
        """B. unchanged workspace symlink => CandidateStoreError."""
        symlink_path = fake_workspace / "docs" / "symlink_doc.txt"
        target_path = fake_workspace / "docs" / "readme.txt"
        try:
            os.symlink(str(target_path), str(symlink_path))
        except OSError:
            pytest.skip("Symlinks not supported on this platform/user privilege level")

        with pytest.raises(CandidateStoreError, match="Symlinks are not permitted"):
            persist_verified_candidate(
                workspace_path=str(fake_workspace),
                project_id="aos",
                task_id="AOS4-REF-001",
                gate="AOS-4",
                control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
                execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                worker_branch="aos/aos4-ref-001",
                initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                changed_paths=["src/sample.py"],
                candidate_store_dir=str(fake_candidate_store),
            )

    def test_symlinked_directory_rejected(self, fake_workspace, fake_candidate_store):
        """C. symlinked directory => CandidateStoreError."""
        symlink_dir = fake_workspace / "symlink_dir"
        target_dir = fake_workspace / "docs"
        try:
            os.symlink(str(target_dir), str(symlink_dir), target_is_directory=True)
        except OSError:
            pytest.skip("Directory symlinks not supported on this platform/user privilege level")

        with pytest.raises(CandidateStoreError, match="Symlinks are not permitted"):
            persist_verified_candidate(
                workspace_path=str(fake_workspace),
                project_id="aos",
                task_id="AOS4-REF-001",
                gate="AOS-4",
                control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
                execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                worker_branch="aos/aos4-ref-001",
                initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                changed_paths=["src/sample.py"],
                candidate_store_dir=str(fake_candidate_store),
            )

    def test_dangling_symlink_rejected(self, fake_workspace, fake_candidate_store):
        """D. dangling symlink => CandidateStoreError."""
        dangling_symlink = fake_workspace / "src" / "dangling_link.py"
        non_existent = fake_workspace / "src" / "non_existent_target.py"
        try:
            os.symlink(str(non_existent), str(dangling_symlink))
        except OSError:
            pytest.skip("Symlinks not supported on this platform/user privilege level")

        with pytest.raises(CandidateStoreError, match="Symlinks are not permitted"):
            persist_verified_candidate(
                workspace_path=str(fake_workspace),
                project_id="aos",
                task_id="AOS4-REF-001",
                gate="AOS-4",
                control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
                execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                worker_branch="aos/aos4-ref-001",
                initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                changed_paths=["src/sample.py"],
                candidate_store_dir=str(fake_candidate_store),
            )

    def test_mocked_symlink_in_source_workspace_rejected_deterministic(self, fake_workspace, fake_candidate_store, monkeypatch):
        """Deterministic mock test for symlink rejection in scan."""
        orig_is_symlink = Path.is_symlink

        def _mock_is_symlink(p):
            if "sample.py" in str(p):
                return True
            return orig_is_symlink(p)

        monkeypatch.setattr(Path, "is_symlink", _mock_is_symlink)

        with pytest.raises(CandidateStoreError, match="Symlinks are not permitted"):
            persist_verified_candidate(
                workspace_path=str(fake_workspace),
                project_id="aos",
                task_id="AOS4-REF-001",
                gate="AOS-4",
                control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
                execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                worker_branch="aos/aos4-ref-001",
                initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                changed_paths=["src/sample.py"],
                candidate_store_dir=str(fake_candidate_store),
            )

    def test_copytree_invoked_with_symlinks_true(self, fake_workspace, fake_candidate_store, monkeypatch):
        """E. candidate persistence invokes copytree with symlinks=True, never symlinks=False."""
        captured_symlinks_args = []
        original_copytree = shutil.copytree

        def _spy_copytree(src, dst, *args, **kwargs):
            if kwargs.get("symlinks") is not None:
                captured_symlinks_args.append(kwargs.get("symlinks"))
            return original_copytree(src, dst, *args, **kwargs)

        monkeypatch.setattr(shutil, "copytree", _spy_copytree)

        persist_verified_candidate(
            workspace_path=str(fake_workspace),
            project_id="aos",
            task_id="AOS4-REF-001",
            gate="AOS-4",
            control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
            execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            worker_branch="aos/aos4-ref-001",
            initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            changed_paths=["src/sample.py"],
            candidate_store_dir=str(fake_candidate_store),
        )

        assert captured_symlinks_args == [True]

    def test_candidate_workspace_with_git_remote_rejected(self, fake_workspace, fake_candidate_store, monkeypatch):
        """F. candidate workspace with a Git remote after copy => persistence fails closed."""
        orig_run = subprocess.run

        def _mock_run(cmd, *args, **kwargs):
            if len(cmd) >= 4 and cmd[0] == "git" and cmd[1] == "-C" and cmd[3] == "remote":
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="origin\n", stderr="")
            return orig_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _mock_run)

        # Mock .git directory in fake workspace
        (fake_workspace / ".git").mkdir(parents=True, exist_ok=True)

        with pytest.raises(CandidateStoreError, match="contains git remotes after persistence"):
            persist_verified_candidate(
                workspace_path=str(fake_workspace),
                project_id="aos",
                task_id="AOS4-REF-001",
                gate="AOS-4",
                control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
                execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                worker_branch="aos/aos4-ref-001",
                initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                changed_paths=["src/sample.py"],
                candidate_store_dir=str(fake_candidate_store),
            )

    def test_clean_zero_remote_git_candidate_succeeds(self, fake_workspace, fake_candidate_store, monkeypatch):
        """G. clean zero-remote Git candidate => persistence succeeds."""
        orig_run = subprocess.run

        def _mock_run(cmd, *args, **kwargs):
            if len(cmd) >= 4 and cmd[0] == "git" and cmd[1] == "-C" and cmd[3] == "remote":
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            return orig_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _mock_run)

        (fake_workspace / ".git").mkdir(parents=True, exist_ok=True)

        res = persist_verified_candidate(
            workspace_path=str(fake_workspace),
            project_id="aos",
            task_id="AOS4-REF-001",
            gate="AOS-4",
            control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
            execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            worker_branch="aos/aos4-ref-001",
            initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            changed_paths=["src/sample.py"],
            candidate_store_dir=str(fake_candidate_store),
        )

        assert res["status"] == "PERSISTED"

    def test_absolute_changed_path_rejected(self, fake_workspace, fake_candidate_store):
        """H. absolute changed path => CandidateStoreError."""
        abs_path = str((fake_workspace / "src" / "sample.py").resolve())
        with pytest.raises(CandidateStoreError, match="Changed path must be repository-relative and not absolute"):
            persist_verified_candidate(
                workspace_path=str(fake_workspace),
                project_id="aos",
                task_id="AOS4-REF-001",
                gate="AOS-4",
                control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
                execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                worker_branch="aos/aos4-ref-001",
                initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                changed_paths=[abs_path],
                candidate_store_dir=str(fake_candidate_store),
            )

    def test_traversal_changed_path_rejected(self, fake_workspace, fake_candidate_store):
        """I. ../ traversal changed path => CandidateStoreError."""
        with pytest.raises(CandidateStoreError, match="Changed path contains traversal components"):
            persist_verified_candidate(
                workspace_path=str(fake_workspace),
                project_id="aos",
                task_id="AOS4-REF-001",
                gate="AOS-4",
                control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
                execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                worker_branch="aos/aos4-ref-001",
                initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                changed_paths=["../escaped.txt"],
                candidate_store_dir=str(fake_candidate_store),
            )

    def test_quarantine_candidate_persistence_creates_unverified_snapshot(self, fake_workspace, fake_candidate_store):
        """L, M, N, O: Quarantine candidate persistence returns QUARANTINED_UNVERIFIED and contract 0.1.0."""
        from aos.candidate_store import QUARANTINE_STORE_CONTRACT_VERSION, persist_quarantine_candidate

        res = persist_quarantine_candidate(
            workspace_path=str(fake_workspace),
            project_id="aos",
            task_id="AOS4-REF-001",
            gate="AOS-4",
            control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
            execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            worker_branch="aos/aos4-ref-001",
            initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
            worker_changed_paths=["src/sample.py"],
            quarantine_store_dir=str(fake_candidate_store),
        )

        assert res["status"] == "QUARANTINED_UNVERIFIED"
        assert res["quarantine_store_contract_version"] == QUARANTINE_STORE_CONTRACT_VERSION
        quar_id = res["quarantine_id"]
        assert quar_id.startswith("quar_")
        assert len(quar_id) == 21
        assert "manifest_sha256" in res
        assert res["worker_changed_paths"] == ["src/sample.py"]

        quar_dir = fake_candidate_store / quar_id
        assert quar_dir.exists()
        manifest_file = quar_dir / "manifest.json"
        assert manifest_file.exists()
        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
        assert manifest_data["quarantine_id"] == quar_id
        assert manifest_data["quarantine_store_contract_version"] == "0.1.0"
        assert len(manifest_data["changed_paths"]) == 1
        assert manifest_data["changed_paths"][0]["path"] == "src/sample.py"
        assert manifest_data["changed_paths"][0]["state"] == "PRESENT"

    def test_quarantine_store_collision_fails_closed(self, fake_workspace, fake_candidate_store, monkeypatch):
        """P. quarantine store collision fails closed."""
        from aos.candidate_store import persist_quarantine_candidate
        import uuid

        class MockUUID:
            hex = "11112222333344445555666677778888"

        monkeypatch.setattr(uuid, "uuid4", lambda: MockUUID())

        # Pre-create colliding directory
        colliding_dir = fake_candidate_store / "quar_1111222233334444"
        colliding_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(CandidateStoreError, match="Quarantine directory collision"):
            persist_quarantine_candidate(
                workspace_path=str(fake_workspace),
                project_id="aos",
                task_id="AOS4-REF-001",
                gate="AOS-4",
                control_source_sha="c95dcd7638138b86f26889b474cc1f303d7a15b7",
                execution_base_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                worker_branch="aos/aos4-ref-001",
                initial_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                final_head_sha="1dfd59f850383dc4f40a59fd42462facb2b89315",
                worker_changed_paths=["src/sample.py"],
                quarantine_store_dir=str(fake_candidate_store),
            )
