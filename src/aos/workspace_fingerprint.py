"""Content-sensitive Git workspace identity for safe agentic session resume."""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from aos.process_utils import run_headless


class WorkspaceFingerprintError(RuntimeError):
    """Sanitized workspace inspection failure with a bounded operation cause."""

    def __init__(self, message: str, *, operation: str = "UNKNOWN", safe_cause: str = "UNKNOWN") -> None:
        super().__init__(message)
        self.operation = operation
        self.safe_cause = safe_cause


RUNTIME_OWNED_ROOT_LOCK_PATH = ".aos_workspace_active.lock"
_RUNTIME_OWNED_ROOT_LOCK_PATH_BYTES = RUNTIME_OWNED_ROOT_LOCK_PATH.encode("ascii")
_RUNTIME_OWNED_ROOT_LOCK_STATUS_RECORD = b"? " + _RUNTIME_OWNED_ROOT_LOCK_PATH_BYTES


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
    operation = _git_operation(args)
    try:
        completed = run_headless(
            ["git", "-C", str(root), *args],
            timeout=30,
            text=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceFingerprintError(
            "git workspace inspection timed out",
            operation=operation,
            safe_cause="TIMEOUT",
        ) from exc
    except OSError as exc:
        raise WorkspaceFingerprintError(
            "git workspace inspection failed",
            operation=operation,
            safe_cause="OS_ERROR",
        ) from exc
    if completed.returncode != 0:
        raise WorkspaceFingerprintError(
            f"git workspace inspection failed with exit {completed.returncode}",
            operation=operation,
            safe_cause=f"GIT_EXIT_{completed.returncode}",
        )
    return completed.stdout


def _git_operation(args: Tuple[str, ...]) -> str:
    if args[:2] == ("rev-parse", "--show-toplevel"):
        return "GIT_RESOLVE_TOPLEVEL"
    if args[:2] == ("rev-parse", "HEAD"):
        return "GIT_READ_HEAD"
    if args[:2] == ("rev-parse", "--show-object-format"):
        return "GIT_READ_OBJECT_FORMAT"
    if args[:3] == ("config", "--get", "core.repositoryformatversion"):
        return "GIT_READ_REPOSITORY_FORMAT"
    if args[:2] == ("ls-files", "--stage"):
        return "GIT_READ_INDEX"
    if args and args[0] == "status":
        return "GIT_READ_STATUS"
    if args[:2] == ("ls-files", "--others"):
        return "GIT_LIST_UNTRACKED"
    if args and args[0] == "ls-files":
        return "GIT_LIST_TRACKED"
    return "GIT_WORKSPACE_INSPECTION"


def _record(digest: "hashlib._Hash", tag: bytes, *values: bytes) -> None:
    digest.update(len(tag).to_bytes(4, "big"))
    digest.update(tag)
    digest.update(len(values).to_bytes(4, "big"))
    for value in values:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)


def _nul_paths(raw: bytes) -> List[bytes]:
    return [value for value in raw.split(b"\0") if value]


def _without_runtime_owned_untracked_status(raw: bytes) -> bytes:
    """Remove only the exact porcelain-v2 untracked root-lock record.

    Rename/copy records have a second NUL-delimited path field.  Preserve that
    field even if its bytes resemble a status record.
    """
    filtered: List[bytes] = []
    rename_source = False
    for record in raw.split(b"\0"):
        if rename_source:
            filtered.append(record)
            rename_source = False
            continue
        if record == _RUNTIME_OWNED_ROOT_LOCK_STATUS_RECORD:
            continue
        filtered.append(record)
        if record.startswith(b"2 "):
            rename_source = True
    return b"\0".join(filtered)


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
    except PermissionError as exc:
        raise WorkspaceFingerprintError(
            "workspace path metadata is not readable",
            operation="PATH_LSTAT",
            safe_cause="PERMISSION_DENIED",
        ) from exc
    except OSError as exc:
        raise WorkspaceFingerprintError(
            "workspace path metadata inspection failed",
            operation="PATH_LSTAT",
            safe_cause="OS_ERROR",
        ) from exc
    mode = oct(stat.S_IMODE(info.st_mode)).encode("ascii")
    if stat.S_ISLNK(info.st_mode):
        target = os.fsencode(os.readlink(path))
        return path_bytes, b"SYMLINK", mode, str(len(target)).encode("ascii"), hashlib.sha256(target).hexdigest().encode("ascii")
    if stat.S_ISREG(info.st_mode):
        try:
            content_sha = _stream_sha256(path)
        except FileNotFoundError:
            return path_bytes, b"MISSING", b"0", b"0", hashlib.sha256(b"").hexdigest().encode("ascii")
        except PermissionError as exc:
            raise WorkspaceFingerprintError(
                "workspace file content is not readable",
                operation="PATH_READ",
                safe_cause="PERMISSION_DENIED",
            ) from exc
        except OSError as exc:
            raise WorkspaceFingerprintError(
                "workspace file content inspection failed",
                operation="PATH_READ",
                safe_cause="OS_ERROR",
            ) from exc
        return path_bytes, b"FILE", mode, str(info.st_size).encode("ascii"), content_sha
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

    # Runtime V1 holds this exact root-relative file open while a workspace is
    # active.  It is operational coordination state, not workspace content.
    # Filter both identity inputs only when Git proves it is untracked; a
    # tracked file with the same name remains fully fingerprinted.
    if (
        _RUNTIME_OWNED_ROOT_LOCK_PATH_BYTES in untracked
        and _RUNTIME_OWNED_ROOT_LOCK_PATH_BYTES not in tracked
    ):
        untracked = [
            path for path in untracked
            if path != _RUNTIME_OWNED_ROOT_LOCK_PATH_BYTES
        ]
        status = _without_runtime_owned_untracked_status(status)

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
