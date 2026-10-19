"""Transactional command/task journal, isolated from protected Runtime V1.

This is an internal controller API, not an authorization endpoint or executor.
Grants and verification records must be resolved by trusted server components.
SQLite transactions cannot make external Git/filesystem effects atomic: an
interrupted RUNNING task must be reconciled, never automatically replayed.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

ACTIVE = frozenset({"QUEUED", "PLANNING", "RUNNING", "VERIFYING", "RECOVERING"})
HELD = frozenset({"PAUSED_SAFE", "WAITING_RESOURCE", "WAITING_AUTHORITY", "HUMAN_REQUIRED"})
TERMINAL = frozenset({"PROJECT_COMPLETE", "FAILED", "CANCELLED", "SUPERSEDED"})
TRANSITIONS = {
    "QUEUED": {"PLANNING", *HELD},
    "PLANNING": {"RUNNING", "VERIFYING", *HELD},
    "RUNNING": {"VERIFYING", "RECOVERING", *HELD},
    "RECOVERING": {"PLANNING", "RUNNING", "VERIFYING", *HELD},
    "VERIFYING": {"RUNNING", *HELD},
    **{state: {"QUEUED"} for state in HELD},
}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")


class LifecycleConflict(ValueError):
    """Stale generation, immutable identity conflict, or invalid transition."""


class OwnerBusyError(LifecycleConflict):
    """Raised when kernel owner lock is held by another process (OWNER_BUSY)."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _reject_constant(val: str) -> None:
    raise ValueError(f"Non-standard JSON constant rejected: {val}")


def _check_json_depth(val: Any, depth: int, max_depth: int) -> None:
    if depth > max_depth:
        raise ValueError(f"JSON nesting depth exceeds maximum limit of {max_depth}")
    if isinstance(val, dict):
        for v in val.values():
            _check_json_depth(v, depth + 1, max_depth)
    elif isinstance(val, (list, tuple)):
        for item in val:
            _check_json_depth(item, depth + 1, max_depth)


def _strict_json_loads(data: bytes | str, max_depth: int = 8) -> Any:
    """Bounded strict UTF-8 JSON decoder: rejects BOM/non-UTF8, duplicates, NaN/Infinity, nesting>8."""
    if isinstance(data, bytes):
        if data.startswith(b"\xef\xbb\xbf"):
            raise ValueError("UTF-8 BOM is forbidden")
        text = data.decode("utf-8")
    else:
        if data.startswith("\ufeff"):
            raise ValueError("BOM is forbidden")
        text = data

    def check_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        res: dict[str, Any] = {}
        for k, v in pairs:
            if k in res:
                raise ValueError(f"Duplicate JSON key: {k}")
            res[k] = v
        return res

    parsed = json.loads(text, object_pairs_hook=check_duplicate_keys, parse_constant=_reject_constant)
    _check_json_depth(parsed, 1, max_depth)
    return parsed


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid identifier")
    return value


_REQUEST_KEY = re.compile(r"^[A-Za-z0-9_.:\-/+=@#~%]{1,256}\Z")


def _request_key(value: str) -> str:
    if not isinstance(value, str) or not _REQUEST_KEY.fullmatch(value):
        raise ValueError("Invalid request key")
    return value


def _digest(value: str, length: int = 64) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{%d}" % length, value):
        raise ValueError("Invalid immutable digest")
    return value


def _relative(value: str) -> str:
    p = PurePosixPath(value)
    if not value or p.is_absolute() or ".." in p.parts or "\\" in value or ":" in value or "\x00" in value or str(p) != value or value == ".":
        raise ValueError("Scope must contain normalized relative file paths")
    for part in p.parts:
        stem = part.split(".", 1)[0].casefold()
        if (part.casefold() == ".git" or part.rstrip(" .") != part
                or re.search(r'[<>"|?*\x00-\x1f]', part)
                or stem in {"con", "prn", "aux", "nul", "conin$", "conout$"}
                or re.fullmatch(r"(?:com|lpt)[1-9¹²³]", stem)):
            raise ValueError("Git metadata and nonportable Windows path aliases are forbidden")
    return value


def _no_links(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    for part in (candidate, *candidate.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("Symlink/junction roots are not allowed")
        if part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("Reparse-point paths are not allowed")
    return candidate.resolve()


def _set_windows_owner_only_dacl(path: Path) -> None:
    """Set protected DACL granting ONLY current owner full access, disabling inheritance."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32

    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi32.SetFileSecurityW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return
    try:
        info_len = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(info_len))
        buf = ctypes.create_string_buffer(info_len.value)
        if not advapi32.GetTokenInformation(token, 1, buf, info_len.value, ctypes.byref(info_len)):
            return
        sid_ptr = ctypes.c_void_p.from_buffer_copy(buf.raw[:ctypes.sizeof(ctypes.c_void_p)]).value
        p_str = ctypes.c_void_p()
        if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(p_str)):
            return
        sid = ctypes.wstring_at(p_str)
        kernel32.LocalFree(p_str)

        # D:P specifies DACL protected (SE_DACL_PROTECTED) - prevents inheritance from parent folders
        # (A;;FA;;;{sid}) grants Allow Full Access to owner SID only
        sddl = f"D:P(A;;FA;;;{sid})"
        sd = ctypes.c_void_p()
        if advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):
            advapi32.SetFileSecurityW(str(path), 0x00000004 | 0x80000000, sd)
            kernel32.LocalFree(sd)
    finally:
        kernel32.CloseHandle(token)


def write_private_file(path: Path | str, content: str | bytes) -> Path:
    """Atomically write file with strict owner-only permissions from birth."""
    target = _no_links(Path(path))
    if not target.is_absolute():
        raise ValueError("Private file path must be absolute")
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = content.encode("utf-8") if isinstance(content, str) else content

    nonce = secrets.token_hex(8)
    tmp_path = target.parent / f".tmp_{target.name}_{nonce}"
    if os.name == "nt":
        tmp_path.write_bytes(raw)
        _set_windows_owner_only_dacl(tmp_path)
    else:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with open(fd, "wb") as f:
            f.write(raw)
    os.replace(str(tmp_path), str(target))
    if os.name == "nt":
        _set_windows_owner_only_dacl(target)
    else:
        target.chmod(0o600)
    return target


def validate_private_file(path: Path | str) -> Path:
    """Validate that path exists, is a regular single-link file without reparse points or insecure permissions."""
    target = _no_links(Path(path))
    if not target.is_absolute():
        raise ValueError("Path must be absolute")
    if not target.is_file():
        raise ValueError(f"Private file '{target}' does not exist or is not a regular file")
    st = target.stat()
    if st.st_nlink != 1:
        raise ValueError(f"Private file '{target}' must not have hard links (st_nlink={st.st_nlink})")
    if os.name != "nt":
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise ValueError(f"Private file '{target}' is not owned by current user")
        if st.st_mode & 0o077:
            raise ValueError(f"Private file '{target}' has insecure permissions (0o{st.st_mode:o})")
    return target



def _get_process_birth_time() -> float:
    """Return process creation timestamp."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]
            c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            kernel32 = ctypes.windll.kernel32
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            h = kernel32.GetCurrentProcess()
            if kernel32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                # Windows FILETIME is in 100-nanosecond intervals since Jan 1, 1601 UTC
                intervals = (c.dwHighDateTime << 32) + c.dwLowDateTime
                return (intervals - 116444736000000000) / 10000000.0
        except Exception:
            pass
    return time.time()


@dataclass(frozen=True)
class OwnerIdentity:
    pid: int
    birth_timestamp: float
    executable_hash: str
    source_identity: str
    home_identity: str
    nonce: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "birth_timestamp": self.birth_timestamp,
            "executable_hash": self.executable_hash,
            "source_identity": self.source_identity,
            "home_identity": self.home_identity,
            "nonce": self.nonce,
        }

    def digest(self) -> str:
        return hashlib.sha256(_json(self.to_dict()).encode("utf-8")).hexdigest()


class KernelOwnerLock:
    """Exclusive process-lifetime kernel lock with no TTL stealing."""
    def __init__(self, home: Path) -> None:
        self.home = _no_links(Path(home))
        self.lock_path = self.home / "journal.lock"
        self._file = None
        self._locked = False
        self._identity = None
        self._ov = None

    @property
    def is_locked(self) -> bool:
        return self._locked

    @property
    def identity(self) -> OwnerIdentity:
        if self._identity is None:
            raise LifecycleConflict("Lock not acquired")
        return self._identity

    def acquire(self) -> OwnerIdentity:
        if self._locked:
            return self._identity

        self.home.mkdir(parents=True, exist_ok=True)
        # Calculate executable hash
        try:
            exe_hash = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
        except Exception:
            exe_hash = hashlib.sha256(sys.executable.encode("utf-8")).hexdigest()

        # Calculate source identity
        try:
            source_file = Path(__file__).resolve()
            source_id = hashlib.sha256(source_file.read_bytes()).hexdigest()
        except Exception:
            source_id = "unknown_source"

        nonce = secrets.token_hex(16)
        birth = _get_process_birth_time()
        identity = OwnerIdentity(
            pid=os.getpid(),
            birth_timestamp=birth,
            executable_hash=exe_hash,
            source_identity=source_id,
            home_identity=str(self.home),
            nonce=nonce,
        )

        f = open(self.lock_path, "a+b")
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
            LOCKFILE_EXCLUSIVE_LOCK = 0x00000002

            class OVERLAPPED(ctypes.Structure):
                _fields_ = [
                    ("Internal", ctypes.c_size_t),
                    ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE),
                ]

            ov = OVERLAPPED()
            handle = msvcrt.get_osfhandle(f.fileno())
            kernel32 = ctypes.windll.kernel32
            kernel32.LockFileEx.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED),
            ]
            kernel32.LockFileEx.restype = wintypes.BOOL
            res = kernel32.LockFileEx(
                handle,
                LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                0,
                0,
                1,
                ctypes.byref(ov),
            )
            if not res:
                err = kernel32.GetLastError()
                f.close()
                if err in (33, 32):  # ERROR_LOCK_VIOLATION or ERROR_SHARING_VIOLATION
                    raise OwnerBusyError("OWNER_BUSY: kernel owner lock held by another process")
                raise LifecycleConflict(f"Failed to acquire kernel owner lock on Windows (error {err})")
            self._ov = ov
        else:
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                f.close()
                raise OwnerBusyError("OWNER_BUSY: kernel owner lock held by another process")

        # Write identity metadata to lock file
        try:
            f.seek(0)
            f.truncate()
            f.write(_json(identity.to_dict()).encode("utf-8") + b"\n")
            f.flush()
        except Exception:
            pass

        self._file = f
        self._locked = True
        self._identity = identity
        return identity

    def release(self) -> None:
        if not self._locked:
            return
        if self._file is not None:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                import msvcrt
                try:
                    handle = msvcrt.get_osfhandle(self._file.fileno())
                    kernel32 = ctypes.windll.kernel32
                    kernel32.UnlockFileEx.argtypes = [
                        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                        wintypes.DWORD, ctypes.POINTER(type(self._ov)),
                    ]
                    kernel32.UnlockFileEx.restype = wintypes.BOOL
                    kernel32.UnlockFileEx(handle, 0, 0, 1, ctypes.byref(self._ov))
                except Exception:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
        self._locked = False
        self._identity = None
        self._ov = None

    def __enter__(self) -> OwnerIdentity:
        return self.acquire()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()

    def __del__(self) -> None:
        self.release()


@dataclass(frozen=True)
class TrustedGrant:
    """Construct only from a server-owned registry, never a request payload."""
    project_id: str
    authority_id: str
    workspace: str
    base_sha: str
    write_files: tuple[str, ...]

    def binding(self) -> dict[str, Any]:
        _identifier(self.project_id)
        _identifier(self.authority_id)
        if self.project_id.lower() in {"lari", "lari-ui-v2"}:
            raise ValueError("Protected Product lanes are forbidden in this V2 slice")
        root = _no_links(Path(self.workspace))
        if not Path(self.workspace).is_absolute() or not root.is_dir():
            raise ValueError("Existing absolute workspace is required")
        _digest(self.base_sha, 40)
        if not self.write_files or len(set(self.write_files)) != len(self.write_files):
            raise ValueError("Nonempty unique file scope required")
        for name in self.write_files:
            target = root / _relative(name)
            _no_links(target)
            if not target.resolve().is_relative_to(root):
                raise ValueError("Scope escape")
            if target.exists() and (not target.is_file() or target.stat().st_nlink != 1):
                raise ValueError("Write scope must not alias directories or hard-linked files")
        return {"project_id": self.project_id, "authority_id": self.authority_id,
                "workspace": str(root), "base_sha": self.base_sha,
                "write_files": sorted(self.write_files), "cost_mode": "ZERO_PAID", "production": "NO_GO"}


class LifecycleStore:
    """One opt-in local SQLite journal; transactional schema 2 with owner fencing."""
    def __init__(self, home: Path) -> None:
        root = _no_links(Path(home))
        if any(p.name.casefold() == "runtime-v1" or (p / "runtime-config.json").exists()
               for p in (root, *root.parents)):
            raise ValueError("Protected V1 runtime homes are forbidden")
        marker = root / ".aos-v2-home"
        _no_links(marker)
        if root.exists() and any(root.iterdir()) and not marker.is_file():
            raise ValueError("Refusing to adopt a nonempty unmarked directory")
        root.mkdir(parents=True, exist_ok=True)
        if marker.exists() and marker.read_text() != "AOS_V2_JOURNAL_1\n":
            raise ValueError("Unknown journal marker version")
        if not marker.exists():
            marker.write_text("AOS_V2_JOURNAL_1\n")
        self.root = root
        self.path = root / "journal.sqlite3"
        _no_links(self.path)

        # Inspect schema version read-only BEFORE opening write/WAL connection
        if self.path.exists():
            posix_path = self.path.resolve().as_posix()
            ro_uri = f"file:///{posix_path}?mode=ro" if not posix_path.startswith("/") else f"file://{posix_path}?mode=ro"
            with closing(sqlite3.connect(ro_uri, uri=True)) as ro_db:
                version = ro_db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise ValueError(f"Unknown journal schema version ({version}); newer software required")
                if version == 2:
                    tables = {r[0] for r in ro_db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                    required_tables = {"commands", "tasks", "events", "outbox", "engine_meta", "attempts", "verifications"}
                    if not required_tables.issubset(tables):
                        raise ValueError("Incomplete schema 2 shape in existing journal; refusing read-only")
            if version == 1:
                self._migrate_schema_1_to_2()
        else:
            with closing(self._connect()) as db:
                self._init_schema_2(db)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _init_schema_2(self, db: sqlite3.Connection) -> None:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS commands(
                id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                request_key TEXT NOT NULL,
                binding TEXT NOT NULL,
                request TEXT NOT NULL,
                state TEXT NOT NULL,
                admission TEXT NOT NULL,
                revision INTEGER NOT NULL,
                result TEXT,
                contract TEXT,
                control TEXT,
                owner_epoch INTEGER NOT NULL DEFAULT 0,
                UNIQUE(project, request_key)
            );
            CREATE TABLE IF NOT EXISTS tasks(
                command_id TEXT NOT NULL REFERENCES commands(id),
                id TEXT NOT NULL,
                identity TEXT NOT NULL,
                state TEXT NOT NULL,
                receipt TEXT,
                effect_phase TEXT NOT NULL DEFAULT 'INTENT',
                fence TEXT,
                expected_parent TEXT,
                prepared_tree TEXT,
                commit_sha TEXT,
                effect TEXT,
                PRIMARY KEY(command_id, id)
            );
            CREATE TABLE IF NOT EXISTS events(
                command_id TEXT NOT NULL REFERENCES commands(id),
                seq INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(command_id, seq)
            );
            CREATE TABLE IF NOT EXISTS outbox(
                command_id TEXT PRIMARY KEY REFERENCES commands(id),
                payload TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS engine_meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts(
                attempt_id TEXT PRIMARY KEY,
                command_id TEXT NOT NULL REFERENCES commands(id),
                task_id TEXT,
                purpose TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                request_sha TEXT NOT NULL,
                response_id TEXT,
                usage_json TEXT,
                finish_reason TEXT,
                quota_headers TEXT,
                latency_ms REAL,
                evidence_ref TEXT,
                billed_cost TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verifications(
                id TEXT PRIMARY KEY,
                command_id TEXT NOT NULL REFERENCES commands(id),
                task_id TEXT,
                kind TEXT NOT NULL,
                driver_id TEXT NOT NULL,
                oracle_sha TEXT NOT NULL,
                response_vector_sha TEXT NOT NULL,
                passed INTEGER NOT NULL,
                details_json TEXT,
                created_at TEXT NOT NULL
            );
            PRAGMA user_version = 2;
        """)

    def _migrate_schema_1_to_2(self) -> None:
        """Perform transactional, copy-first migration from schema 1 to schema 2."""
        backup_path = self.root / f"journal.sqlite3.bak.{int(time.time())}"
        shutil.copy2(self.path, backup_path)
        bck_sha = hashlib.sha256(backup_path.read_bytes()).hexdigest()

        migrated_path = self.root / f"journal.sqlite3.migrating.{os.getpid()}"
        shutil.copy2(backup_path, migrated_path)

        mig_conn = sqlite3.connect(migrated_path, timeout=10)
        try:
            mig_conn.execute("PRAGMA foreign_keys=OFF")
            mig_conn.execute("BEGIN IMMEDIATE")
            mig_conn.execute("ALTER TABLE commands ADD COLUMN contract TEXT")
            mig_conn.execute("ALTER TABLE commands ADD COLUMN control TEXT")
            mig_conn.execute("ALTER TABLE commands ADD COLUMN owner_epoch INTEGER NOT NULL DEFAULT 0")
            mig_conn.execute("ALTER TABLE tasks ADD COLUMN effect_phase TEXT NOT NULL DEFAULT 'INTENT'")
            mig_conn.execute("ALTER TABLE tasks ADD COLUMN fence TEXT")
            mig_conn.execute("ALTER TABLE tasks ADD COLUMN expected_parent TEXT")
            mig_conn.execute("ALTER TABLE tasks ADD COLUMN prepared_tree TEXT")
            mig_conn.execute("ALTER TABLE tasks ADD COLUMN commit_sha TEXT")
            mig_conn.execute("ALTER TABLE tasks ADD COLUMN effect TEXT")
            mig_conn.execute("CREATE TABLE IF NOT EXISTS engine_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            mig_conn.execute("""
                CREATE TABLE IF NOT EXISTS attempts(
                    attempt_id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL REFERENCES commands(id),
                    task_id TEXT,
                    purpose TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    request_sha TEXT NOT NULL,
                    response_id TEXT,
                    usage_json TEXT,
                    finish_reason TEXT,
                    quota_headers TEXT,
                    latency_ms REAL,
                    evidence_ref TEXT,
                    billed_cost TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            mig_conn.execute("""
                CREATE TABLE IF NOT EXISTS verifications(
                    id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL REFERENCES commands(id),
                    task_id TEXT,
                    kind TEXT NOT NULL,
                    driver_id TEXT NOT NULL,
                    oracle_sha TEXT NOT NULL,
                    response_vector_sha TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    details_json TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            mig_conn.execute("INSERT OR REPLACE INTO engine_meta VALUES('schema_backup_sha', ?)", (bck_sha,))
            mig_conn.execute("PRAGMA user_version = 2")
            mig_conn.execute("PRAGMA foreign_keys=ON")
            fk_errors = mig_conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_errors:
                raise LifecycleConflict(f"Foreign key violations in migrated copy: {fk_errors}")
            mig_conn.commit()
        except Exception:
            mig_conn.rollback()
            mig_conn.close()
            if migrated_path.exists():
                migrated_path.unlink()
            raise
        finally:
            mig_conn.close()

        os.replace(migrated_path, self.path)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _command(db: sqlite3.Connection, command_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM commands WHERE id=?", (_identifier(command_id),)).fetchone()
        if row is None:
            raise KeyError(command_id)
        return row

    @staticmethod
    def _event(db: sqlite3.Connection, cid: str, kind: str, value: Any) -> None:
        seq = db.execute("SELECT coalesce(max(seq),0)+1 FROM events WHERE command_id=?", (cid,)).fetchone()[0]
        db.execute("INSERT INTO events(command_id, seq, kind, payload) VALUES(?,?,?,?)", (cid, seq, kind, _json(value)))

    def admit(self, command_id: str, request_key: str, goal: str,
              criteria: tuple[str, ...], grant: TrustedGrant, *,
              contract_json: str | None = None, owner_epoch: int = 0) -> str:
        _identifier(command_id)
        _request_key(request_key)
        binding = grant.binding()
        workspace = Path(binding["workspace"])
        if self.root.is_relative_to(workspace) or workspace.is_relative_to(self.root):
            raise ValueError("Runtime and source workspace must be disjoint")
        if not goal.strip() or len(goal) > 8000 or not criteria or len(set(criteria)) != len(criteria):
            raise ValueError("Bounded goal and nonempty unique acceptance criteria required")
        for criterion in criteria:
            _identifier(criterion)
        req = _json({"goal": goal, "criteria": sorted(criteria)})
        encoded = _json(binding)
        with self._transaction() as db:
            prior = db.execute("SELECT * FROM commands WHERE project=? AND request_key=?", (grant.project_id, request_key)).fetchone()
            if prior:
                if prior["binding"] != encoded or prior["request"] != req:
                    raise LifecycleConflict("Idempotency key reused with different immutable input")
                return str(prior["id"])
            db.execute(
                "INSERT INTO commands(id, project, request_key, binding, request, state, admission, revision, result, contract, control, owner_epoch) "
                "VALUES(?,?,?,?,?,?,?,?,NULL,?,NULL,?)",
                (command_id, grant.project_id, request_key, encoded, req, "QUEUED", "ACTIVE", 0, contract_json, owner_epoch)
            )
            self._event(db, command_id, "command.accepted", {"binding_sha256": _hash(binding)})
        return command_id

    def transition(self, command_id: str, expected_revision: int, target: str, *, reason: str, owner_epoch: int | None = None) -> None:
        with self._transaction() as db:
            row = self._command(db, command_id)
            if row["revision"] != expected_revision or target not in TRANSITIONS.get(row["state"], set()):
                raise LifecycleConflict("Stale revision or forbidden transition")
            if owner_epoch is not None and owner_epoch < row["owner_epoch"]:
                raise LifecycleConflict("Stale owner epoch")
            if target == "QUEUED" and db.execute("SELECT 1 FROM tasks WHERE command_id=? AND state='RUNNING'", (command_id,)).fetchone():
                raise LifecycleConflict("Uncertain effects require reconciliation before resume")
            admission = "HOLD" if target in HELD else "ACTIVE"
            effective_epoch = max(owner_epoch, row["owner_epoch"]) if owner_epoch is not None else row["owner_epoch"]
            db.execute(
                "UPDATE commands SET state=?,admission=?,revision=revision+1,owner_epoch=? WHERE id=?",
                (target, admission, effective_epoch, command_id)
            )
            self._event(db, command_id, "command.transition", {"state": target, "reason": reason, "owner_epoch": effective_epoch})

    def claim_recovery(self, command_id: str, expected_revision: int, owner_epoch: int, observation_ref: str) -> None:
        """Enter RECOVERING / ACTIVE state only after fresh trusted recovery checks."""
        _identifier(command_id)
        _identifier(observation_ref)
        with self._transaction() as db:
            row = self._command(db, command_id)
            if row["revision"] != expected_revision:
                raise LifecycleConflict("Stale revision on recovery claim")
            if row["state"] in TERMINAL:
                raise LifecycleConflict("Cannot claim recovery on terminal command")
            if owner_epoch < row["owner_epoch"]:
                raise LifecycleConflict("Stale owner epoch on recovery claim")
            db.execute(
                "UPDATE commands SET state='RECOVERING',admission='ACTIVE',revision=revision+1,owner_epoch=? WHERE id=?",
                (owner_epoch, command_id)
            )
            self._event(db, command_id, "command.recovery_claimed", {
                "owner_epoch": owner_epoch,
                "observation_ref": observation_ref,
            })

    def begin_task(self, command_id: str, task_id: str, *, pre_fingerprint: str,
                   write_files: tuple[str, ...]) -> str:
        _identifier(task_id)
        _digest(pre_fingerprint)
        if not write_files or len(set(write_files)) != len(write_files):
            raise ValueError("A task needs a nonempty unique write set")
        identity = _json({"pre_fingerprint": pre_fingerprint,
                          "write_files": sorted(_relative(p) for p in write_files)})
        with self._transaction() as db:
            row = self._command(db, command_id)
            if row["state"] not in {"RUNNING", "RECOVERING"} or row["admission"] != "ACTIVE":
                raise LifecycleConflict("Command is not dispatchable")
            if not set(write_files).issubset(json.loads(row["binding"])["write_files"]):
                raise ValueError("Task write set exceeds server grant")
            prior = db.execute("SELECT * FROM tasks WHERE command_id=? AND id=?", (command_id, task_id)).fetchone()
            if prior:
                if prior["identity"] != identity:
                    raise LifecycleConflict("Task identity changed")
                return "ALREADY_ACCEPTED" if prior["state"] == "ACCEPTED" else "RECONCILE_REQUIRED"
            db.execute(
                "INSERT INTO tasks(command_id, id, identity, state, receipt, effect_phase) VALUES(?,?,?,'RUNNING',NULL,'INTENT')",
                (command_id, task_id, identity)
            )
            self._event(db, command_id, "task.intent", {"task_id": task_id, "identity_sha256": _hash(json.loads(identity))})
        return "EXECUTE_ONCE"

    def update_task_phase(
        self,
        command_id: str,
        task_id: str,
        phase: str,
        *,
        fence: str | None = None,
        expected_parent: str | None = None,
        prepared_tree: str | None = None,
        commit_sha: str | None = None,
        effect: dict[str, Any] | None = None,
    ) -> None:
        """Update the durable phase and hashes of a task row."""
        _identifier(task_id)
        valid_phases = {"INTENT", "PREPARED", "VERIFIED", "COMMIT_INTENT", "REF_PUBLISHED", "MATERIALIZED", "ACCEPTED"}
        if phase not in valid_phases:
            raise LifecycleConflict(f"Invalid effect phase: '{phase}'")

        effect_json = _json(effect) if effect is not None else None
        with self._transaction() as db:
            task = db.execute("SELECT * FROM tasks WHERE command_id=? AND id=?", (command_id, task_id)).fetchone()
            if task is None:
                raise KeyError(task_id)
            db.execute(
                "UPDATE tasks SET effect_phase=?,fence=coalesce(?,fence),expected_parent=coalesce(?,expected_parent),"
                "prepared_tree=coalesce(?,prepared_tree),commit_sha=coalesce(?,commit_sha),effect=coalesce(?,effect) "
                "WHERE command_id=? AND id=?",
                (phase, fence, expected_parent, prepared_tree, commit_sha, effect_json, command_id, task_id)
            )

    def accept_task(self, command_id: str, task_id: str, *, result_sha: str,
                    artifacts: dict[str, str], verification_ref: str) -> None:
        """Trusted verifier callback, including reconciliation after external commit."""
        _digest(result_sha, 40)
        _identifier(verification_ref)
        if not artifacts:
            raise ValueError("Artifact evidence required")
        for p, digest in artifacts.items():
            _relative(p)
            _digest(digest)
        receipt = _json({"result_sha": result_sha, "artifacts": artifacts, "verification_ref": verification_ref})
        with self._transaction() as db:
            command = self._command(db, command_id)
            if command["state"] in TERMINAL:
                raise LifecycleConflict("Terminal command is immutable")
            task = db.execute("SELECT * FROM tasks WHERE command_id=? AND id=?", (command_id, _identifier(task_id))).fetchone()
            if task is None or set(artifacts) != set(json.loads(task["identity"])["write_files"]):
                raise LifecycleConflict("Receipt does not match durable task intent")
            if task["state"] == "ACCEPTED":
                if task["receipt"] != receipt:
                    raise LifecycleConflict("Accepted task receipt changed")
                return
            db.execute(
                "UPDATE tasks SET state='ACCEPTED',receipt=?,effect_phase='ACCEPTED' WHERE command_id=? AND id=?",
                (receipt, command_id, task_id)
            )
            self._event(db, command_id, "task.accepted", {"task_id": task_id, "receipt": json.loads(receipt)})

    def finalize(self, command_id: str, expected_revision: int, state: str,
                 criterion_receipts: dict[str, str]) -> None:
        """Atomically close state/result/admission/event/outbox. No external effects."""
        if state not in TERMINAL:
            raise ValueError("A terminal state is required")
        for name, digest in criterion_receipts.items():
            _identifier(name)
            _digest(digest)
        with self._transaction() as db:
            row = self._command(db, command_id)
            if row["revision"] != expected_revision or row["state"] in TERMINAL:
                raise LifecycleConflict("Stale revision or terminal command")
            if state == "PROJECT_COMPLETE":
                required = set(json.loads(row["request"])["criteria"])
                counts = db.execute("SELECT count(*),sum(state!='ACCEPTED') FROM tasks WHERE command_id=?", (command_id,)).fetchone()
                if row["state"] != "VERIFYING" or set(criterion_receipts) != required or counts[0] == 0 or counts[1] != 0:
                    raise LifecycleConflict("Success requires verified tasks and every goal predicate")
            result = _json({"schema_version": 2, "command_id": command_id, "state": state,
                            "criterion_receipts": criterion_receipts, "binding_sha256": _hash(json.loads(row["binding"]))})
            db.execute("UPDATE commands SET state=?,admission='CLOSED',revision=revision+1,result=? WHERE id=?", (state, result, command_id))
            self._event(db, command_id, "run.finished", json.loads(result))
            db.execute("INSERT INTO outbox(command_id,payload) VALUES(?,?)", (command_id, result))

    def snapshot(self, command_id: str) -> dict[str, Any]:
        db = self._connect()
        try:
            db.execute("BEGIN")
            row = dict(self._command(db, command_id))
            row["tasks"] = [dict(t) for t in db.execute("SELECT * FROM tasks WHERE command_id=? ORDER BY id", (command_id,))]
            row["events"] = [dict(t) for t in db.execute("SELECT * FROM events WHERE command_id=? ORDER BY seq", (command_id,))]
            return row
        finally:
            db.rollback()
            db.close()

    def pending_outbox(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            return [dict(r) for r in db.execute("SELECT * FROM outbox WHERE acknowledged=0 ORDER BY command_id")]

    def acknowledge_outbox(self, command_id: str, payload_sha256: str) -> None:
        """Consumer calls only after idempotent durable export/readback succeeds."""
        _digest(payload_sha256)
        with self._transaction() as db:
            row = db.execute("SELECT payload FROM outbox WHERE command_id=?", (_identifier(command_id),)).fetchone()
            if row is None or hashlib.sha256(row[0].encode()).hexdigest() != payload_sha256:
                raise LifecycleConflict("Outbox identity mismatch")
            db.execute("UPDATE outbox SET acknowledged=1 WHERE command_id=?", (command_id,))

    def set_control(self, command_id: str, control_type: str, details: dict[str, Any] | None = None) -> None:
        """Store pause/resume/cancel control intent on command."""
        _identifier(command_id)
        val = _json({"type": control_type, "details": details or {}, "set_at": time.time()})
        with self._transaction() as db:
            db.execute("UPDATE commands SET control=? WHERE id=?", (val, command_id))

    def get_control(self, command_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT control FROM commands WHERE id=?", (_identifier(command_id),)).fetchone()
            if row and row[0]:
                return json.loads(row[0])
            return None

    def reserve_attempt(
        self,
        attempt_id: str,
        command_id: str,
        task_id: str | None,
        purpose: str,
        profile_id: str,
        request_sha: str,
    ) -> None:
        """Reserve a unique attempt atomically before network dispatch."""
        _identifier(attempt_id)
        _identifier(command_id)
        if task_id:
            _identifier(task_id)
        _identifier(purpose)
        _identifier(profile_id)
        _digest(request_sha)
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._transaction() as db:
            db.execute(
                "INSERT INTO attempts(attempt_id, command_id, task_id, purpose, profile_id, request_sha, "
                "response_id, usage_json, finish_reason, quota_headers, latency_ms, evidence_ref, billed_cost, created_at) "
                "VALUES(?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,'UNKNOWN',?)",
                (attempt_id, command_id, task_id, purpose, profile_id, request_sha, created_at)
            )

    def record_attempt_outcome(
        self,
        attempt_id: str,
        *,
        response_id: str | None = None,
        usage_json: str | None = None,
        finish_reason: str | None = None,
        quota_headers: str | None = None,
        latency_ms: float | None = None,
        evidence_ref: str | None = None,
        billed_cost: str = "UNKNOWN",
    ) -> None:
        """Record the outcome of a reserved attempt without erasing history."""
        _identifier(attempt_id)
        with self._transaction() as db:
            db.execute(
                "UPDATE attempts SET response_id=coalesce(?,response_id), usage_json=coalesce(?,usage_json), "
                "finish_reason=coalesce(?,finish_reason), quota_headers=coalesce(?,quota_headers), "
                "latency_ms=coalesce(?,latency_ms), evidence_ref=coalesce(?,evidence_ref), "
                "billed_cost=coalesce(?,billed_cost) WHERE attempt_id=?",
                (response_id, usage_json, finish_reason, quota_headers, latency_ms, evidence_ref, billed_cost, attempt_id)
            )

    def record_attempt(
        self,
        attempt_id: str,
        command_id: str,
        purpose: str,
        profile_id: str,
        request_sha: str,
        *,
        task_id: str | None = None,
        response_id: str | None = None,
        usage_json: str | None = None,
        finish_reason: str | None = None,
        quota_headers: str | None = None,
        latency_ms: float | None = None,
        evidence_ref: str | None = None,
        billed_cost: str = "UNKNOWN",
    ) -> None:
        _identifier(attempt_id)
        _identifier(command_id)
        _identifier(purpose)
        _identifier(profile_id)
        _digest(request_sha)
        with self._transaction() as db:
            row = db.execute("SELECT 1 FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if not row:
                created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                db.execute(
                    "INSERT INTO attempts(attempt_id, command_id, task_id, purpose, profile_id, request_sha, "
                    "response_id, usage_json, finish_reason, quota_headers, latency_ms, evidence_ref, billed_cost, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (attempt_id, command_id, task_id, purpose, profile_id, request_sha, response_id, usage_json,
                     finish_reason, quota_headers, latency_ms, evidence_ref, billed_cost, created_at)
                )
            else:
                db.execute(
                    "UPDATE attempts SET response_id=coalesce(?,response_id), usage_json=coalesce(?,usage_json), "
                    "finish_reason=coalesce(?,finish_reason), quota_headers=coalesce(?,quota_headers), "
                    "latency_ms=coalesce(?,latency_ms), evidence_ref=coalesce(?,evidence_ref), "
                    "billed_cost=coalesce(?,billed_cost) WHERE attempt_id=?",
                    (response_id, usage_json, finish_reason, quota_headers, latency_ms, evidence_ref, billed_cost, attempt_id)
                )

    def record_verification(
        self,
        verification_id: str,
        command_id: str,
        kind: str,
        driver_id: str,
        oracle_sha: str,
        response_vector_sha: str,
        passed: bool,
        *,
        task_id: str | None = None,
        details_json: str | None = None,
    ) -> None:
        _identifier(verification_id)
        _identifier(command_id)
        _identifier(kind)
        _digest(oracle_sha)
        _digest(response_vector_sha)
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._transaction() as db:
            db.execute(
                "INSERT INTO verifications(id, command_id, task_id, kind, driver_id, oracle_sha, response_vector_sha, passed, details_json, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (verification_id, command_id, task_id, kind, driver_id, oracle_sha, response_vector_sha, 1 if passed else 0, details_json, created_at)
            )

    def set_meta(self, key: str, value: str) -> None:
        with self._transaction() as db:
            db.execute("INSERT OR REPLACE INTO engine_meta(key, value) VALUES(?,?)", (key, value))

    def get_meta(self, key: str) -> str | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT value FROM engine_meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def get_engine_meta(self) -> dict[str, str]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT key, value FROM engine_meta").fetchall()
            return {r[0]: r[1] for r in rows}

    def get_owner_epoch(self) -> int:
        val = self.get_meta("owner_epoch")
        return int(val) if val is not None else 0

    def increment_owner_epoch(self, identity: OwnerIdentity) -> int:
        with self._transaction() as db:
            row = db.execute("SELECT value FROM engine_meta WHERE key='owner_epoch'").fetchone()
            curr = int(row[0]) if row and row[0] else 0
            new_epoch = curr + 1
            db.execute("INSERT OR REPLACE INTO engine_meta(key, value) VALUES('owner_epoch', ?)", (str(new_epoch),))
            db.execute("INSERT OR REPLACE INTO engine_meta(key, value) VALUES('owner_identity', ?)", (_json(identity.to_dict()),))
            return new_epoch

    def get_command_by_idempotency_key(self, request_key: str, project_id: str | None = None) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            if project_id is not None:
                row = db.execute("SELECT * FROM commands WHERE request_key=? AND project=?", (request_key, project_id)).fetchone()
            else:
                row = db.execute("SELECT * FROM commands WHERE request_key=?", (request_key,)).fetchone()
            if not row:
                return None
            res = dict(row)
            # Add project_id alias for consistency
            if "project" in res and "project_id" not in res:
                res["project_id"] = res["project"]
            if "request" in res and res["request"]:
                try:
                    req_obj = json.loads(res["request"])
                    if "goal" in req_obj:
                        res["goal"] = req_obj["goal"]
                except Exception:
                    pass
            return res
