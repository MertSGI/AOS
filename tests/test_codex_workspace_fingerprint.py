import os
import subprocess
from pathlib import Path

from aos.workspace_fingerprint import compute_workspace_fingerprint


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
