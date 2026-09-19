import subprocess
import sys
from pathlib import Path

# Add project root to sys.path to import materialize_slot
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

import materialize_slot
from aos.provenance import ProvenanceError


def test_materialize_missing_sha_fails(monkeypatch):
    """Materializer must fail closed when arguments are missing."""
    with pytest.raises(SystemExit) as exc:
        materialize_slot.main([])
    assert exc.value.code != 0


def test_materialize_invalid_sha_fails():
    """Materializer must refuse non-40-char or malformed SHA."""
    with pytest.raises(ProvenanceError) as exc:
        materialize_slot.materialize(
            sha="not-a-sha",
            ci_run_id=12345,
        )
    assert "not a valid 40-character hex SHA" in str(exc.value)


def test_materialize_git_head_mismatch_fails(tmp_path: Path, monkeypatch):
    """Materializer must fail closed if local authoritative Git HEAD != requested source SHA."""
    sha1 = "e97faa55b8b3f668055c791a89c8f77966450935"
    sha2 = "454c5cf910cac642afd15c281fe4cbaf83a2507f"

    # Mock get_authoritative_git_head to return sha1
    monkeypatch.setattr("materialize_slot.get_authoritative_git_head", lambda _: sha1)

    with pytest.raises(ProvenanceError) as exc:
        materialize_slot.materialize(
            sha=sha2,
            ci_run_id=12345,
            repo_root=tmp_path,
        )
    assert "does not match requested source SHA" in str(exc.value)


def test_materialize_ci_head_mismatch_fails(tmp_path: Path, monkeypatch):
    """Materializer must fail closed if GitHub CI run head_sha != requested source SHA."""
    sha = "e97faa55b8b3f668055c791a89c8f77966450935"
    other_sha = "454c5cf910cac642afd15c281fe4cbaf83a2507f"

    monkeypatch.setattr("materialize_slot.get_authoritative_git_head", lambda _: sha)

    # Mock urllib.request.urlopen to return CI head_sha == other_sha
    class DummyResp:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b'{"status": "completed", "conclusion": "success", "head_sha": "' + other_sha.encode() + b'"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: DummyResp())

    with pytest.raises(ProvenanceError) as exc:
        materialize_slot.materialize(
            sha=sha,
            ci_run_id=12345,
            repo_root=tmp_path,
        )
    assert "CI run 12345 head_sha mismatch" in str(exc.value)


def test_materialize_ci_conclusion_not_success_fails(tmp_path: Path, monkeypatch):
    """Materializer must fail closed if GitHub CI run conclusion is not success."""
    sha = "e97faa55b8b3f668055c791a89c8f77966450935"

    monkeypatch.setattr("materialize_slot.get_authoritative_git_head", lambda _: sha)

    class DummyResp:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b'{"status": "completed", "conclusion": "failure", "head_sha": "' + sha.encode() + b'"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: DummyResp())

    with pytest.raises(ProvenanceError) as exc:
        materialize_slot.materialize(
            sha=sha,
            ci_run_id=12345,
            repo_root=tmp_path,
        )
    assert "conclusion is not 'success'" in str(exc.value)


def test_materialize_success_when_all_match(tmp_path: Path, monkeypatch):
    """Materializer succeeds and builds slot when SHA, Git HEAD, and CI run match."""
    sha = "e97faa55b8b3f668055c791a89c8f77966450935"

    # Setup mock repo root with src/aos
    src_aos = tmp_path / "src" / "aos"
    src_aos.mkdir(parents=True, exist_ok=True)
    (src_aos / "__init__.py").write_text("# init", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='aos'", encoding="utf-8")

    monkeypatch.setattr("materialize_slot.get_authoritative_git_head", lambda _: sha)

    class DummyResp:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b'{"status": "completed", "conclusion": "success", "head_sha": "' + sha.encode() + b'"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: DummyResp())

    out_dir = tmp_path / "target_candidate"
    monkeypatch.setenv("LOCALAPPDATA", str(out_dir))

    slot_root = materialize_slot.materialize(
        sha=sha,
        ci_run_id=12345,
        repo_root=tmp_path,
    )
    assert slot_root.is_dir()
    manifest_path = slot_root / "candidate-manifest.json"
    assert manifest_path.is_file()
