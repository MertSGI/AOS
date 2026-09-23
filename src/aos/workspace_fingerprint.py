"""Content-sensitive Git workspace identity for safe agentic session resume."""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


class WorkspaceFingerprintError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceFingerprint:
    sha256: str
    repository_root: str
    head_sha: str
    source_sha: str
    entry_count: int
    initialized_submodule_count: int
    schema_version: str = "1.0.0"

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _git(root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceFingerprintError("git workspace inspection failed") from exc
    if completed.returncode != 0:
        raise WorkspaceFingerprintError(
            f"git {' '.join(args[:2])} failed with exit {completed.returncode}"
        )
    return completed.stdout


def _record(digest: "hashlib._Hash", tag: bytes, *values: bytes) -> None:
    digest.update(len(tag).to_bytes(4, "big"))
    digest.update(tag)
    digest.update(len(values).to_bytes(4, "big"))
    for value in values:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)


def _nul_paths(raw: bytes) -> List[bytes]:
    return [value for value in raw.split(b"\0") if value]


def _stage_entries(raw: bytes) -> Dict[bytes, Tuple[bytes, bytes]]:
    entries: Dict[bytes, Tuple[bytes, bytes]] = {}
    for record in _nul_paths(raw):
        metadata, separator, path = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3:
            raise WorkspaceFingerprintError("unexpected git ls-files --stage record")
        mode, object_id, stage = fields
        if stage == b"0":
            entries[path] = (mode, object_id)
    return entries


def _stream_sha256(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().encode("ascii")


def _path_record(root: Path, path_bytes: bytes) -> Tuple[bytes, ...]:
    relative = os.fsdecode(path_bytes)
    path = root / relative
    try:
        info = path.lstat()
    except FileNotFoundError:
        return path_bytes, b"MISSING", b"0", b"0", hashlib.sha256(b"").hexdigest().encode("ascii")
    mode = oct(stat.S_IMODE(info.st_mode)).encode("ascii")
    if stat.S_ISLNK(info.st_mode):
        target = os.fsencode(os.readlink(path))
        return path_bytes, b"SYMLINK", mode, str(len(target)).encode("ascii"), hashlib.sha256(target).hexdigest().encode("ascii")
    if stat.S_ISREG(info.st_mode):
        return path_bytes, b"FILE", mode, str(info.st_size).encode("ascii"), _stream_sha256(path)
    if stat.S_ISDIR(info.st_mode):
        return path_bytes, b"DIRECTORY", mode, b"0", hashlib.sha256(b"").hexdigest().encode("ascii")
    return path_bytes, b"OTHER", mode, str(info.st_size).encode("ascii"), hashlib.sha256(b"").hexdigest().encode("ascii")


def compute_workspace_fingerprint(
    workspace: str | Path,
    *,
    source_sha: str,
    _visited_roots: Optional[Set[str]] = None,
) -> WorkspaceFingerprint:
    requested = Path(workspace).expanduser().resolve()
    root_raw = _git(requested, "rev-parse", "--show-toplevel").strip()
    if not root_raw:
        raise WorkspaceFingerprintError("workspace is not a Git repository")
    root = Path(os.fsdecode(root_raw)).resolve()
    root_key = os.path.normcase(str(root))
    visited = set(_visited_roots or set())
    if root_key in visited:
        raise WorkspaceFingerprintError("recursive submodule workspace detected")
    visited.add(root_key)

    head = _git(root, "rev-parse", "HEAD").strip()
    object_format = _git(root, "rev-parse", "--show-object-format").strip()
    repository_format = _git(root, "config", "--get", "core.repositoryformatversion").strip()
    stage = _git(root, "ls-files", "--stage", "-z")
    status = _git(root, "status", "--porcelain=v2", "-z", "--untracked-files=all")
    tracked = _nul_paths(_git(root, "ls-files", "-z"))
    untracked = _nul_paths(_git(root, "ls-files", "--others", "--exclude-standard", "-z"))
    index_entries = _stage_entries(stage)

    digest = hashlib.sha256()
    _record(
        digest,
        b"FORMAT",
        b"AOS_GIT_WORKSPACE_FINGERPRINT",
        b"1.0.0",
        repository_format,
        object_format,
    )
    _record(digest, b"ROOT", os.fsencode(str(root)))
    _record(digest, b"HEAD", head)
    _record(digest, b"SOURCE_SHA", source_sha.encode("utf-8"))
    _record(digest, b"INDEX", stage)
    _record(digest, b"STATUS", status)

    all_paths = sorted(set(tracked) | set(untracked))
    for path_bytes in all_paths:
        _record(digest, b"PATH", *_path_record(root, path_bytes))

    initialized_submodules = 0
    for path_bytes, (mode, object_id) in sorted(index_entries.items()):
        if mode != b"160000":
            continue
        submodule_root = root / os.fsdecode(path_bytes)
        if not submodule_root.is_dir() or not (submodule_root / ".git").exists():
            _record(digest, b"SUBMODULE", path_bytes, object_id, b"UNINITIALIZED")
            continue
        child = compute_workspace_fingerprint(
            submodule_root,
            source_sha=object_id.decode("ascii"),
            _visited_roots=visited,
        )
        initialized_submodules += 1
        _record(
            digest,
            b"SUBMODULE",
            path_bytes,
            object_id,
            child.sha256.encode("ascii"),
        )

    return WorkspaceFingerprint(
        sha256=digest.hexdigest(),
        repository_root=str(root),
        head_sha=head.decode("ascii"),
        source_sha=source_sha,
        entry_count=len(all_paths),
        initialized_submodule_count=initialized_submodules,
    )
