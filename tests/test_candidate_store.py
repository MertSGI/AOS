"""Tests for machine-local candidate store and verified candidate persistence."""

import hashlib
import json
import os
import shutil
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
