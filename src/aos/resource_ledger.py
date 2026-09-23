"""Append-only operational resource accounting for Resource OS."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from aos.provider_observation import RateLimitObservation, canonical_task_class
from aos.runtime_store import atomic_json, exclusive_file_lock


class ResourceLedgerCorruptionError(ValueError):
    pass


class ResourceEventType(str, Enum):
    ATTEMPT_STARTED = "ATTEMPT_STARTED"
    ATTEMPT_FINISHED = "ATTEMPT_FINISHED"
    RATE_OBSERVED = "RATE_OBSERVED"
    QUOTA_DECISION = "QUOTA_DECISION"
    HEALTH_OBSERVED = "HEALTH_OBSERVED"
    RESOURCE_USAGE = "RESOURCE_USAGE"
    RESERVATION_CREATED = "RESERVATION_CREATED"
    RESERVATION_RELEASED = "RESERVATION_RELEASED"
    RECOVERY_DISPOSITION = "RECOVERY_DISPOSITION"


_SAFE_KEYS = {
    "provider_id", "model_id", "resource_id", "backend_id", "task_class",
    "billing_class", "attempt_id", "work_id", "status", "disposition",
    "failure_family", "request_count", "input_tokens", "cached_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
    "cost_estimate_usd", "cost_actual_usd", "artifact_count",
    "rate_limit_observation", "quota_decision", "recovery_fingerprint",
    "retry_at_epoch", "eligible", "state", "reason", "evidence_source",
    "key", "same_fingerprint_respawns", "strategy_generation",
    "batch_number", "completed_batch_count_baseline", "objective_id",
    "workspace_source_generation", "fingerprint_sha256",
}
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:/|*-]{0,256}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:|-]{1,256}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _safe_number(value: Any, *, integer: bool = False) -> Optional[int | float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    if integer:
        return int(number) if number.is_integer() else None
    return number


def _sanitize_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    integer_keys = {
        "request_count", "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_output_tokens", "total_tokens", "artifact_count",
        "same_fingerprint_respawns", "strategy_generation",
        "batch_number", "completed_batch_count_baseline",
    }
    numeric_keys = integer_keys | {"cost_estimate_usd", "cost_actual_usd", "retry_at_epoch"}
    for key, value in payload.items():
        if key not in _SAFE_KEYS or value is None:
            continue
        if key == "rate_limit_observation" and isinstance(value, Mapping):
            safe[key] = RateLimitObservation.from_dict(value).to_dict()
        elif key in {"quota_decision", "recovery_fingerprint"} and isinstance(value, Mapping):
            safe[key] = _sanitize_payload(value)
        elif key in numeric_keys:
            number = _safe_number(value, integer=key in integer_keys)
            if number is not None:
                safe[key] = number
        elif key == "eligible" and isinstance(value, bool):
            safe[key] = value
        elif key == "task_class":
            safe[key] = canonical_task_class(value)
        else:
            text = str(value)
            if _SAFE_TEXT.fullmatch(text):
                safe[key] = text
    return safe


@dataclass(frozen=True)
class ResourceLedgerEvent:
    sequence: int
    event_id: str
    idempotency_key: str
    event_type: str
    occurred_at_epoch: float
    payload: Dict[str, Any]
    previous_hash: str
    event_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "sequence": self.sequence,
            "event_id": self.event_id,
            "idempotency_key": self.idempotency_key,
            "event_type": self.event_type,
            "occurred_at_epoch": self.occurred_at_epoch,
            "payload": dict(self.payload),
            "previous_hash": self.previous_hash,
            "event_hash": self.event_hash,
        }


class ResourceLedger:
    schema_version = "1.0.0"

    def __init__(
        self,
        log_path: Path,
        snapshot_path: Optional[Path] = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.log_path = log_path
        self.snapshot_path = snapshot_path or log_path.with_name("resource-ledger-snapshot.json")
        self.clock = clock
        self._events: List[ResourceLedgerEvent] = []
        self._corrupt = False
        self._truncated_tail_ignored = False
        self.reload()

    @property
    def corrupt(self) -> bool:
        return self._corrupt

    @property
    def head_hash(self) -> str:
        return self._events[-1].event_hash if self._events else "0" * 64

    def reload(self) -> None:
        try:
            events, truncated = self._replay_file()
            self._events = events
            self._truncated_tail_ignored = truncated
            self._corrupt = False
            self._write_snapshot()
        except ResourceLedgerCorruptionError:
            self._events = []
            self._corrupt = True

    def _replay_file(self) -> Tuple[List[ResourceLedgerEvent], bool]:
        if not self.log_path.exists():
            return [], False
        raw = self.log_path.read_bytes()
        lines = raw.splitlines(keepends=True)
        events: List[ResourceLedgerEvent] = []
        previous_hash = "0" * 64
        ids: set[str] = set()
        truncated = False
        for index, raw_line in enumerate(lines):
            is_final = index == len(lines) - 1
            complete = raw_line.endswith((b"\n", b"\r"))
            try:
                data = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                if is_final and not complete:
                    truncated = True
                    break
                raise ResourceLedgerCorruptionError("invalid non-tail ledger event")
            try:
                event = self._event_from_dict(data)
            except (TypeError, ValueError, KeyError) as exc:
                raise ResourceLedgerCorruptionError("invalid ledger event") from exc
            if event.sequence != len(events) + 1 or event.previous_hash != previous_hash:
                raise ResourceLedgerCorruptionError("ledger sequence/hash link mismatch")
            unsigned = event.to_dict()
            unsigned.pop("event_hash")
            expected_hash = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
            if event.event_hash != expected_hash or event.event_id in ids:
                raise ResourceLedgerCorruptionError("ledger integrity mismatch")
            events.append(event)
            previous_hash = event.event_hash
            ids.add(event.event_id)
        return events, truncated

    @staticmethod
    def _event_from_dict(data: Mapping[str, Any]) -> ResourceLedgerEvent:
        if data.get("schema_version") != "1.0.0":
            raise ValueError("unsupported event schema")
        event_type = ResourceEventType(str(data["event_type"])).value
        payload = _sanitize_payload(data.get("payload", {}))
        if payload != data.get("payload"):
            raise ValueError("event contains noncanonical payload")
        sequence = int(data["sequence"])
        event_id = str(data["event_id"])
        idempotency_key = str(data["idempotency_key"])
        occurred_at_epoch = float(data["occurred_at_epoch"])
        previous_hash = str(data["previous_hash"])
        event_hash = str(data["event_hash"])
        if sequence < 1 or not _SHA256.fullmatch(event_id):
            raise ValueError("invalid ledger event identity")
        if not _SAFE_ID.fullmatch(idempotency_key):
            raise ValueError("invalid ledger idempotency key")
        if not math.isfinite(occurred_at_epoch) or occurred_at_epoch < 0:
            raise ValueError("invalid ledger timestamp")
        if not _SHA256.fullmatch(previous_hash) or not _SHA256.fullmatch(event_hash):
            raise ValueError("invalid ledger hash")
        return ResourceLedgerEvent(
            sequence=sequence,
            event_id=event_id,
            idempotency_key=idempotency_key,
            event_type=event_type,
            occurred_at_epoch=occurred_at_epoch,
            payload=payload,
            previous_hash=previous_hash,
            event_hash=event_hash,
        )

    def append(
        self,
        event_type: ResourceEventType | str,
        *,
        idempotency_key: str,
        payload: Mapping[str, Any],
        occurred_at_epoch: Optional[float] = None,
    ) -> ResourceLedgerEvent:
        event_type_value = ResourceEventType(event_type).value
        if not _SAFE_ID.fullmatch(idempotency_key):
            raise ValueError("idempotency_key must be a safe bounded identifier")
        safe_payload = _sanitize_payload(payload)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.log_path.with_suffix(self.log_path.suffix + ".lock")):
            events, truncated = self._replay_file()
            if truncated:
                valid = b"".join(
                    _canonical_json(event.to_dict()) + b"\n" for event in events
                )
                temp = self.log_path.with_name(self.log_path.name + ".repair.tmp")
                temp.write_bytes(valid)
                os.replace(temp, self.log_path)
            for existing in events:
                if existing.idempotency_key == idempotency_key:
                    if (
                        existing.event_type != event_type_value
                        or existing.payload != safe_payload
                    ):
                        raise ValueError("idempotency_key conflicts with an existing event")
                    self._events = events
                    return existing
            sequence = len(events) + 1
            timestamp = float(self.clock() if occurred_at_epoch is None else occurred_at_epoch)
            if not math.isfinite(timestamp) or timestamp < 0:
                raise ValueError("occurred_at_epoch must be a non-negative finite number")
            event_id = hashlib.sha256(
                f"{event_type_value}|{idempotency_key}".encode("utf-8")
            ).hexdigest()
            unsigned = {
                "schema_version": self.schema_version,
                "sequence": sequence,
                "event_id": event_id,
                "idempotency_key": idempotency_key,
                "event_type": event_type_value,
                "occurred_at_epoch": timestamp,
                "payload": safe_payload,
                "previous_hash": events[-1].event_hash if events else "0" * 64,
            }
            event = ResourceLedgerEvent(
                **{key: value for key, value in unsigned.items() if key != "schema_version"},
                event_hash=hashlib.sha256(_canonical_json(unsigned)).hexdigest(),
            )
            with self.log_path.open("ab") as handle:
                handle.write(_canonical_json(event.to_dict()) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._events = events + [event]
            self._corrupt = False
            self._truncated_tail_ignored = truncated
            self._write_snapshot()
            return event

    def events(self, event_types: Optional[Iterable[str]] = None) -> Tuple[ResourceLedgerEvent, ...]:
        allowed = set(event_types) if event_types is not None else None
        return tuple(
            event for event in self._events
            if allowed is None or event.event_type in allowed
        )

    def _derived_snapshot(self) -> Dict[str, Any]:
        summary = {
            "schema_version": self.schema_version,
            "fail_closed": self._corrupt,
            "truncated_tail_ignored": self._truncated_tail_ignored,
            "event_count": len(self._events),
            "last_sequence": self._events[-1].sequence if self._events else 0,
            "head_hash": self.head_hash,
            "attempts_started": 0,
            "attempts_finished": 0,
            "request_count": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 0,
            "cost_estimate_usd": 0.0,
            "cost_actual_usd": 0.0,
        }
        for event in self._events:
            if event.event_type == ResourceEventType.ATTEMPT_STARTED.value:
                summary["attempts_started"] += 1
            if event.event_type == ResourceEventType.ATTEMPT_FINISHED.value:
                summary["attempts_finished"] += 1
            if event.event_type == ResourceEventType.RESOURCE_USAGE.value:
                for key in (
                    "request_count", "input_tokens", "cached_input_tokens",
                    "output_tokens", "reasoning_output_tokens", "total_tokens",
                ):
                    summary[key] += int(event.payload.get(key, 0) or 0)
                for key in ("cost_estimate_usd", "cost_actual_usd"):
                    summary[key] = round(summary[key] + float(event.payload.get(key, 0.0) or 0.0), 12)
        return summary

    def _write_snapshot(self) -> None:
        if self._corrupt:
            return
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.snapshot_path, self._derived_snapshot())

    def summary(self) -> Dict[str, Any]:
        return self._derived_snapshot()
