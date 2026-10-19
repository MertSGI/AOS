"""Transactional command/task journal, isolated from protected Runtime V1.

This is an internal controller API, not an authorization endpoint or executor.
Grants and verification records must be resolved by trusted server components.
SQLite transactions cannot make external Git/filesystem effects atomic: an
interrupted RUNNING task must be reconciled, never automatically replayed.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
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


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid identifier")
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


@dataclass(frozen=True)
class TrustedGrant:
    """Construct only from a server-owned registry, never a request payload.

    This object is a binding, not proof of provider entitlement or OS sandboxing.
    Registry ownership, expiration, revocation and resource policy are enforced
    by the future engine at admission and immediately before every side effect.
    """
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
    """One opt-in local SQLite journal; no automatic migrations or scheduling."""
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
        with closing(self._connect()) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unknown journal schema version; migration required")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS commands(
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, request_key TEXT NOT NULL,
                    binding TEXT NOT NULL, request TEXT NOT NULL, state TEXT NOT NULL,
                    admission TEXT NOT NULL, revision INTEGER NOT NULL, result TEXT,
                    UNIQUE(project, request_key));
                CREATE TABLE IF NOT EXISTS tasks(
                    command_id TEXT NOT NULL REFERENCES commands(id), id TEXT NOT NULL,
                    identity TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT,
                    PRIMARY KEY(command_id,id));
                CREATE TABLE IF NOT EXISTS events(
                    command_id TEXT NOT NULL REFERENCES commands(id), seq INTEGER NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(command_id,seq));
                CREATE TABLE IF NOT EXISTS outbox(
                    command_id TEXT PRIMARY KEY REFERENCES commands(id),
                    payload TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0);
                PRAGMA user_version=1;
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

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
        db.execute("INSERT INTO events VALUES(?,?,?,?)", (cid, seq, kind, _json(value)))

    def admit(self, command_id: str, request_key: str, goal: str,
              criteria: tuple[str, ...], grant: TrustedGrant) -> str:
        _identifier(command_id)
        _identifier(request_key)
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
            db.execute("INSERT INTO commands VALUES(?,?,?,?,?,?,?,?,NULL)",
                       (command_id, grant.project_id, request_key, encoded, req, "QUEUED", "ACTIVE", 0))
            self._event(db, command_id, "command.accepted", {"binding_sha256": _hash(binding)})
        return command_id

    def transition(self, command_id: str, expected_revision: int, target: str, *, reason: str) -> None:
        with self._transaction() as db:
            row = self._command(db, command_id)
            if row["revision"] != expected_revision or target not in TRANSITIONS.get(row["state"], set()):
                raise LifecycleConflict("Stale revision or forbidden transition")
            if target == "QUEUED" and db.execute("SELECT 1 FROM tasks WHERE command_id=? AND state='RUNNING'", (command_id,)).fetchone():
                raise LifecycleConflict("Uncertain effects require reconciliation before resume")
            admission = "HOLD" if target in HELD else "ACTIVE"
            db.execute("UPDATE commands SET state=?,admission=?,revision=revision+1 WHERE id=?", (target, admission, command_id))
            self._event(db, command_id, "command.transition", {"state": target, "reason": reason})

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
            db.execute("INSERT INTO tasks VALUES(?,?,?,'RUNNING',NULL)", (command_id, task_id, identity))
            self._event(db, command_id, "task.intent", {"task_id": task_id, "identity_sha256": _hash(json.loads(identity))})
        return "EXECUTE_ONCE"

    def accept_task(self, command_id: str, task_id: str, *, result_sha: str,
                    artifacts: dict[str, str], verification_ref: str) -> None:
        """Trusted verifier callback, including reconciliation after external commit.

        The engine must independently validate actual Git/test/artifact evidence;
        accepting a backend's success boolean here would violate this contract.
        """
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
            db.execute("UPDATE tasks SET state='ACCEPTED',receipt=? WHERE command_id=? AND id=?", (receipt, command_id, task_id))
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
            result = _json({"schema_version": 1, "command_id": command_id, "state": state,
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
