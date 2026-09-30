"""Atomic, lock-safe, append-only knowledge ledger."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from aos.knowledge.model import (
    CONTRACT_VERSION,
    MAX_EVENT_BYTES,
    AuthorityClass,
    KnowledgeEventType,
    normalize_event_fields,
    validate_event,
)
from aos.runtime_store import exclusive_file_lock


ZERO_HASH = "0" * 64
_THREAD_LOCKS: Dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


@contextmanager
def _append_lock(path: Path, *, timeout_seconds: float = 30.0):
    """Serialize threads and retry the cross-process OS lock on Windows."""
    identity = str(path.resolve()).casefold()
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(identity, threading.RLock())
    with thread_lock:
        deadline = time.monotonic() + timeout_seconds
        lock_context = None
        handle = None
        while True:
            candidate = exclusive_file_lock(path, blocking=False)
            try:
                handle = candidate.__enter__()
                lock_context = candidate
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise KnowledgeLedgerError(f"timed out acquiring knowledge ledger lock: {path}")
                time.sleep(0.01)
        try:
            yield handle
        finally:
            assert lock_context is not None
            lock_context.__exit__(None, None, None)


class KnowledgeLedgerError(RuntimeError):
    pass


class KnowledgeLedgerCorruptionError(KnowledgeLedgerError):
    pass


def canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class KnowledgeLedger:
    """Hash-chained JSONL ledger. Historical bytes are never rewritten."""

    def __init__(self, knowledge_root: Path, *, clock: Callable[[], str] = utc_now) -> None:
        self.root = Path(knowledge_root).expanduser().resolve()
        self.log_path = self.root / "ledger.jsonl"
        self.index_path = self.root / "index.json"
        self.lock_path = self.root / "ledger.lock"
        self.clock = clock
        for directory in ("snapshots", "materialized", "sync", "receipts"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)

    def _read_events_unlocked(self) -> Tuple[Dict[str, Any], ...]:
        if not self.log_path.exists():
            return ()
        events = []
        previous_hash = ZERO_HASH
        seen_ids: set[str] = set()
        seen_keys: set[str] = set()
        with self.log_path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.endswith(b"\n"):
                    raise KnowledgeLedgerCorruptionError(f"truncated ledger line {line_number}")
                if len(raw_line) > MAX_EVENT_BYTES:
                    raise KnowledgeLedgerCorruptionError(f"oversized ledger line {line_number}")
                try:
                    event = json.loads(raw_line.decode("utf-8"))
                    validate_event(event)
                except Exception as exc:
                    raise KnowledgeLedgerCorruptionError(f"invalid ledger line {line_number}") from exc
                if event["sequence"] != line_number or event["previous_hash"] != previous_hash:
                    raise KnowledgeLedgerCorruptionError(f"broken ledger chain at line {line_number}")
                unsigned = dict(event)
                content_hash = str(unsigned.pop("content_hash"))
                expected = hashlib.sha256(canonical_json(unsigned)).hexdigest()
                if content_hash != expected:
                    raise KnowledgeLedgerCorruptionError(f"content hash mismatch at line {line_number}")
                if event["event_id"] in seen_ids or event["idempotency_key"] in seen_keys:
                    raise KnowledgeLedgerCorruptionError(f"duplicate immutable identity at line {line_number}")
                events.append(event)
                previous_hash = content_hash
                seen_ids.add(str(event["event_id"]))
                seen_keys.add(str(event["idempotency_key"]))
        return tuple(events)

    def read_events(self) -> Tuple[Dict[str, Any], ...]:
        with _append_lock(self.lock_path):
            return self._read_events_unlocked()

    def append(
        self,
        event_type: KnowledgeEventType | str,
        *,
        project_id: str,
        idempotency_key: str,
        authority_class: AuthorityClass | str = AuthorityClass.KNOWLEDGE_LEDGER,
        created_at: Optional[str] = None,
        append_precondition: Optional[Callable[[Tuple[Dict[str, Any], ...]], None]] = None,
        **fields: Any,
    ) -> Dict[str, Any]:
        event_type_value = KnowledgeEventType(event_type).value
        authority_value = AuthorityClass(authority_class).value
        if not project_id or len(project_id) > 256:
            raise ValueError("project_id must be a non-empty bounded identifier")
        if not idempotency_key or len(idempotency_key) > 512:
            raise ValueError("idempotency_key must be a non-empty bounded identifier")
        if "production" in fields and fields["production"] != "NO_GO":
            raise ValueError("KCP cannot enable production")
        if "paid_fallback" in fields and fields["paid_fallback"] != "DISABLED":
            raise ValueError("KCP cannot enable paid fallback")

        supplied = normalize_event_fields(fields)
        semantic = {
            "event_type": event_type_value,
            "authority_class": authority_value,
            "project_id": str(project_id),
            **supplied,
            "production": "NO_GO",
            "paid_fallback": "DISABLED",
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with _append_lock(self.lock_path):
            events = self._read_events_unlocked()
            if append_precondition is not None:
                append_precondition(events)
            for existing in events:
                if existing["idempotency_key"] != idempotency_key:
                    continue
                comparable = {
                    key: value for key, value in existing.items()
                    if key not in {"contract_version", "sequence", "event_id", "idempotency_key", "created_at", "previous_hash", "content_hash"}
                }
                if comparable != semantic:
                    raise KnowledgeLedgerError("idempotency key conflicts with an existing event")
                return dict(existing)

            timestamp = str(created_at or self.clock())
            try:
                datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("created_at must be an ISO-8601 timestamp") from exc
            sequence = len(events) + 1
            event_id = hashlib.sha256(
                f"{CONTRACT_VERSION}|{project_id}|{event_type_value}|{idempotency_key}".encode("utf-8")
            ).hexdigest()
            unsigned: Dict[str, Any] = {
                "contract_version": CONTRACT_VERSION,
                "sequence": sequence,
                "event_id": event_id,
                "idempotency_key": idempotency_key,
                "event_type": event_type_value,
                "created_at": timestamp,
                "authority_class": authority_value,
                "project_id": str(project_id),
                **supplied,
                "production": "NO_GO",
                "paid_fallback": "DISABLED",
                "previous_hash": events[-1]["content_hash"] if events else ZERO_HASH,
            }
            event = {**unsigned, "content_hash": hashlib.sha256(canonical_json(unsigned)).hexdigest()}
            validate_event(event)
            encoded = canonical_json(event) + b"\n"
            if len(encoded) > MAX_EVENT_BYTES:
                raise ValueError("knowledge event exceeds bounded size")
            with self.log_path.open("ab") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            receipt_path = self.root / "receipts" / f"{event_id}.json"
            if not receipt_path.exists():
                from aos.runtime_store import atomic_json
                atomic_json(receipt_path, event)
            return event

    def verify(self) -> Dict[str, Any]:
        events = self.read_events()
        return {
            "contract_version": CONTRACT_VERSION,
            "status": "READY",
            "event_count": len(events),
            "last_sequence": events[-1]["sequence"] if events else 0,
            "head_hash": events[-1]["content_hash"] if events else ZERO_HASH,
            "production": "NO_GO",
            "paid_fallback": "DISABLED",
        }

    def events(self, event_types: Optional[Iterable[str]] = None) -> Tuple[Dict[str, Any], ...]:
        allowed = set(event_types) if event_types is not None else None
        return tuple(event for event in self.read_events() if allowed is None or event["event_type"] in allowed)
