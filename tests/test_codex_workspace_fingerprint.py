import os
import subprocess
from pathlib import Path

import pytest

from aos import runtime_worker
import aos.workspace_fingerprint as workspace_fingerprint
from aos.runtime_store import exclusive_file_lock
from aos.workspace_fingerprint import WorkspaceFingerprintError, compute_workspace_fingerprint


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "resource-os@example.invalid")
    _git(root, "config", "user.name", "Resource OS Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    (root / "rename-me.txt").write_text("rename\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root


def _fingerprint(root: Path) -> str:
    head = _git(root, "rev-parse", "HEAD")
    return compute_workspace_fingerprint(root, source_sha=head).sha256


def test_clean_fingerprint_is_deterministic_and_content_sensitive(tmp_path):
    root = _repo(tmp_path)
    first = compute_workspace_fingerprint(root, source_sha=_git(root, "rev-parse", "HEAD"))
    second = compute_workspace_fingerprint(root, source_sha=_git(root, "rev-parse", "HEAD"))

    assert first == second
    assert len(first.sha256) == 64
    assert first.entry_count == 2
    assert compute_workspace_fingerprint(root, source_sha="f" * 40).sha256 != first.sha256

    baseline = first.sha256
    (root / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    assert _fingerprint(root) != baseline


def test_staged_untracked_binary_delete_rename_and_mode_each_change_identity(tmp_path):
    root = _repo(tmp_path)
    seen = {_fingerprint(root)}

    (root / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(root, "add", "staged.txt")
    seen.add(_fingerprint(root))

    (root / "binary.bin").write_bytes(b"\x00\xff\x10RESOURCE_OS")
    seen.add(_fingerprint(root))

    (root / "tracked.txt").unlink()
    seen.add(_fingerprint(root))

    _git(root, "mv", "rename-me.txt", "renamed.txt")
    seen.add(_fingerprint(root))

    _git(root, "update-index", "--chmod=+x", "renamed.txt")
    seen.add(_fingerprint(root))

    assert len(seen) == 6


def test_ignored_files_do_not_change_identity(tmp_path):
    root = _repo(tmp_path)
    (root / ".gitignore").write_text("cache/\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-m", "ignore cache")
    baseline = _fingerprint(root)

    (root / "cache").mkdir()
    (root / "cache" / "runtime.bin").write_bytes(b"ignored")

    assert _fingerprint(root) == baseline


def test_symlink_target_changes_identity_when_supported(tmp_path):
    root = _repo(tmp_path)
    link = root / "link.txt"
    try:
        os.symlink("tracked.txt", link)
    except (OSError, NotImplementedError):
        def stage_symlink(target: bytes) -> None:
            blob = subprocess.run(
                ["git", "-C", str(root), "hash-object", "-w", "--stdin"],
                input=target,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            ).stdout.decode("ascii").strip()
            _git(root, "update-index", "--add", "--cacheinfo", f"120000,{blob},link.txt")

        stage_symlink(b"tracked.txt")
        first = _fingerprint(root)
        stage_symlink(b"rename-me.txt")
        assert _fingerprint(root) != first
        return
    first = _fingerprint(root)
    link.unlink()
    os.symlink("rename-me.txt", link)
    assert _fingerprint(root) != first


def test_initialized_submodule_content_changes_parent_identity(tmp_path):
    child = _repo(tmp_path, "child")
    parent = _repo(tmp_path, "parent")
    _git(
        parent,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(child),
        "deps/child",
    )
    _git(parent, "commit", "-am", "add submodule")
    baseline = compute_workspace_fingerprint(
        parent, source_sha=_git(parent, "rev-parse", "HEAD")
    )

    (parent / "deps" / "child" / "tracked.txt").write_text(
        "submodule dirty\n", encoding="utf-8"
    )
    changed = compute_workspace_fingerprint(
        parent, source_sha=_git(parent, "rev-parse", "HEAD")
    )

    assert baseline.initialized_submodule_count == 1
    assert changed.sha256 != baseline.sha256


def test_exact_untracked_runtime_lock_is_excluded_while_other_content_remains_sensitive(tmp_path):
    root = _repo(tmp_path)
    clean = compute_workspace_fingerprint(root, source_sha=_git(root, "rev-parse", "HEAD"))
    lock_path = root / ".aos_workspace_active.lock"

    lock_path.write_bytes(b"0")
    unlocked = compute_workspace_fingerprint(root, source_sha=clean.source_sha)
    assert unlocked.sha256 == clean.sha256
    assert unlocked.entry_count == clean.entry_count

    with exclusive_file_lock(lock_path):
        held = compute_workspace_fingerprint(root, source_sha=clean.source_sha)
        assert held.sha256 == clean.sha256

        other = root / "new-product-input.txt"
        other.write_text("first\n", encoding="utf-8")
        other_first = compute_workspace_fingerprint(root, source_sha=clean.source_sha)
        assert other_first.sha256 != clean.sha256
        other.write_text("second\n", encoding="utf-8")
        assert compute_workspace_fingerprint(root, source_sha=clean.source_sha).sha256 != other_first.sha256

        (root / "tracked.txt").write_text("tracked change\n", encoding="utf-8")
        tracked_changed = compute_workspace_fingerprint(root, source_sha=clean.source_sha)
        assert tracked_changed.sha256 not in {clean.sha256, other_first.sha256}


def test_nested_runtime_lock_name_is_not_excluded(tmp_path):
    root = _repo(tmp_path)
    baseline = _fingerprint(root)
    nested = root / "some" / "path" / ".aos_workspace_active.lock"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"0")

    assert _fingerprint(root) != baseline


def test_tracked_root_runtime_lock_name_remains_fingerprinted(tmp_path):
    root = _repo(tmp_path)
    lock_path = root / ".aos_workspace_active.lock"
    lock_path.write_text("tracked baseline\n", encoding="utf-8")
    _git(root, "add", ".aos_workspace_active.lock")
    _git(root, "commit", "-m", "track lock-shaped product file")
    baseline = _fingerprint(root)

    lock_path.write_text("tracked changed\n", encoding="utf-8")

    assert _fingerprint(root) != baseline


def test_machine_local_worker_lock_keeps_tracked_lock_shaped_source_readable(tmp_path):
    root = _repo(tmp_path)
    tracked_lock = root / ".aos_workspace_active.lock"
    tracked_lock.write_text("tracked product content\n", encoding="utf-8")
    _git(root, "add", ".aos_workspace_active.lock")
    _git(root, "commit", "-m", "track lock-shaped product file")
    baseline = _fingerprint(root)

    runtime_root = tmp_path / "runtime-state"
    machine_lock = runtime_worker._workspace_lock_path(runtime_root, root)
    machine_lock.parent.mkdir(parents=True)

    assert root not in machine_lock.parents
    with exclusive_file_lock(machine_lock):
        assert tracked_lock.read_text(encoding="utf-8") == "tracked product content\n"
        assert _fingerprint(root) == baseline


def test_other_unreadable_file_errors_are_not_ignored(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    blocked = root / "blocked-product.bin"
    blocked.write_bytes(b"product")
    original = workspace_fingerprint._stream_sha256

    def fail_only_for_blocked(path):
        if path == blocked:
            raise PermissionError("deterministic unreadable product fixture")
        return original(path)

    monkeypatch.setattr(workspace_fingerprint, "_stream_sha256", fail_only_for_blocked)

    with pytest.raises(WorkspaceFingerprintError) as captured:
        _fingerprint(root)

    assert captured.value.operation == "PATH_READ"
    assert captured.value.safe_cause == "PERMISSION_DENIED"
    assert "blocked-product.bin" not in str(captured.value)


def test_git_failure_reports_exact_sanitized_fingerprint_operation(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    original = workspace_fingerprint.run_headless

    def fail_status(command, **kwargs):
        if "status" in command:
            raise subprocess.TimeoutExpired(command, 30)
        return original(command, **kwargs)

    monkeypatch.setattr(workspace_fingerprint, "run_headless", fail_status)

    with pytest.raises(WorkspaceFingerprintError) as captured:
        _fingerprint(root)

    assert captured.value.operation == "GIT_READ_STATUS"
    assert captured.value.safe_cause == "TIMEOUT"
