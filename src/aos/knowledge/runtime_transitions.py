"""Write-ahead KCP protocol helpers for crash-safe runtime transitions."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.model import KnowledgeEventType
from aos.knowledge.receipts import (
    record_runtime_transition_aborted,
    record_runtime_transition_intent,
)


COMPLETION_TYPES = {
    KnowledgeEventType.LIVE_PROMOTION_RECEIPT.value,
    KnowledgeEventType.ROLLBACK_RECEIPT.value,
}


class RuntimeTransitionTerminalStateError(RuntimeError):
    """A transition attempt cannot acquire two different terminal states."""


def transition_identity(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _transition_status_from_events(
    events: Iterable[Mapping[str, Any]], transition_id: str
) -> Dict[str, Optional[Dict[str, Any]]]:
    status: Dict[str, Optional[Dict[str, Any]]] = {
        "intent": None,
        "completion": None,
        "abort": None,
    }
    for event in events:
        if event.get("claims", {}).get("transition_id") != transition_id:
            continue
        if event["event_type"] == KnowledgeEventType.RUNTIME_TRANSITION_INTENT.value:
            status["intent"] = dict(event)
        elif event["event_type"] in COMPLETION_TYPES:
            status["completion"] = dict(event)
        elif event["event_type"] == KnowledgeEventType.RUNTIME_TRANSITION_ABORTED.value:
            status["abort"] = dict(event)
    if status["completion"] is not None and status["abort"] is not None:
        raise RuntimeTransitionTerminalStateError(
            f"RUNTIME_TRANSITION_TERMINAL_STATE_CONFLICT:{transition_id}"
        )
    return status


def transition_status(ledger: KnowledgeLedger, transition_id: str) -> Dict[str, Optional[Dict[str, Any]]]:
    return _transition_status_from_events(ledger.read_events(), transition_id)


def _assert_prepare_status(
    status: Mapping[str, Optional[Mapping[str, Any]]], transition_id: str
) -> None:
    if status["completion"] is not None or status["abort"] is not None:
        terminal = "COMPLETED" if status["completion"] is not None else "ABORTED"
        raise RuntimeTransitionTerminalStateError(
            f"RUNTIME_TRANSITION_ALREADY_{terminal}:{transition_id}"
        )


def assert_transition_prepare_allowed(ledger: KnowledgeLedger, transition_id: str) -> None:
    _assert_prepare_status(transition_status(ledger, transition_id), transition_id)


def transition_prepare_precondition(
    transition_id: str,
) -> Callable[[tuple[Dict[str, Any], ...]], None]:
    def check(events: tuple[Dict[str, Any], ...]) -> None:
        _assert_prepare_status(_transition_status_from_events(events, transition_id), transition_id)
    return check


def _assert_completion_status(
    status: Mapping[str, Optional[Mapping[str, Any]]],
    transition_id: str,
    *,
    event_type: KnowledgeEventType | str,
    idempotency_key: str,
) -> None:
    if status["abort"] is not None:
        raise RuntimeTransitionTerminalStateError(
            f"RUNTIME_TRANSITION_ALREADY_ABORTED:{transition_id}"
        )
    completion = status["completion"]
    if completion is not None and (
        completion["event_type"] != KnowledgeEventType(event_type).value
        or completion["idempotency_key"] != idempotency_key
    ):
        raise RuntimeTransitionTerminalStateError(
            f"RUNTIME_TRANSITION_ALREADY_COMPLETED:{transition_id}"
        )


def assert_transition_completion_allowed(
    ledger: KnowledgeLedger,
    transition_id: str,
    *,
    event_type: KnowledgeEventType | str,
    idempotency_key: str,
) -> None:
    _assert_completion_status(
        transition_status(ledger, transition_id),
        transition_id,
        event_type=event_type,
        idempotency_key=idempotency_key,
    )


def transition_completion_precondition(
    transition_id: str,
    *,
    event_type: KnowledgeEventType | str,
    idempotency_key: str,
) -> Callable[[tuple[Dict[str, Any], ...]], None]:
    def check(events: tuple[Dict[str, Any], ...]) -> None:
        _assert_completion_status(
            _transition_status_from_events(events, transition_id),
            transition_id,
            event_type=event_type,
            idempotency_key=idempotency_key,
        )
    return check


def _assert_abort_status(
    status: Mapping[str, Optional[Mapping[str, Any]]],
    transition_id: str,
    *,
    idempotency_key: str,
) -> None:
    if status["completion"] is not None:
        raise RuntimeTransitionTerminalStateError(
            f"RUNTIME_TRANSITION_ALREADY_COMPLETED:{transition_id}"
        )
    abort = status["abort"]
    if abort is not None and abort["idempotency_key"] != idempotency_key:
        raise RuntimeTransitionTerminalStateError(
            f"RUNTIME_TRANSITION_ALREADY_ABORTED:{transition_id}"
        )


def assert_transition_abort_allowed(
    ledger: KnowledgeLedger,
    transition_id: str,
    *,
    idempotency_key: str,
) -> None:
    _assert_abort_status(
        transition_status(ledger, transition_id),
        transition_id,
        idempotency_key=idempotency_key,
    )


def transition_abort_precondition(
    transition_id: str,
    *,
    idempotency_key: str,
) -> Callable[[tuple[Dict[str, Any], ...]], None]:
    def check(events: tuple[Dict[str, Any], ...]) -> None:
        _assert_abort_status(
            _transition_status_from_events(events, transition_id),
            transition_id,
            idempotency_key=idempotency_key,
        )
    return check


def unresolved_transition_intents(
    ledger: KnowledgeLedger,
    *,
    boundary: Optional[str] = None,
    resource: Optional[str] = None,
) -> tuple[Dict[str, Any], ...]:
    intents: Dict[str, Dict[str, Any]] = {}
    resolved: set[str] = set()
    for event in ledger.read_events():
        claims = event.get("claims", {})
        transition_id = str(claims.get("transition_id") or "")
        if not transition_id:
            continue
        if event["event_type"] == KnowledgeEventType.RUNTIME_TRANSITION_INTENT.value:
            intents[transition_id] = dict(event)
        elif event["event_type"] in COMPLETION_TYPES or event["event_type"] == KnowledgeEventType.RUNTIME_TRANSITION_ABORTED.value:
            resolved.add(transition_id)
    result = []
    for transition_id, event in intents.items():
        claims = event.get("claims", {})
        if transition_id in resolved:
            continue
        if boundary is not None and claims.get("boundary") != boundary:
            continue
        if resource is not None and claims.get("resource") != resource:
            continue
        result.append(event)
    return tuple(sorted(result, key=lambda item: (item["sequence"], item["event_id"])))


def prepare_transition(
    ledger: KnowledgeLedger,
    *,
    transition_id: str,
    operation: str,
    boundary: str,
    resource: str,
    result_sha: str,
    previous_state: Mapping[str, Any],
    target_state: Mapping[str, Any],
    module_ids: Iterable[str],
    identity: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    assert_transition_prepare_allowed(ledger, transition_id)
    return record_runtime_transition_intent(
        ledger,
        project_id="AOS",
        idempotency_key=f"runtime-transition-intent:{transition_id}",
        agent_class="AOS_NATIVE",
        tool_name=boundary,
        result_sha=result_sha,
        module_ids=list(module_ids),
        claims={
            "transition_id": transition_id,
            "operation": operation,
            "boundary": boundary,
            "resource": resource,
            "previous_state": dict(previous_state),
            "target_state": dict(target_state),
            "identity": dict(identity or {}),
            "status": "PREPARED",
        },
    )


def abort_transition(
    ledger: KnowledgeLedger,
    *,
    transition_id: str,
    boundary: str,
    result_sha: str,
    reason: str,
    recovered_state: Mapping[str, Any],
) -> Dict[str, Any]:
    return record_runtime_transition_aborted(
        ledger,
        project_id="AOS",
        idempotency_key=f"runtime-transition-aborted:{transition_id}",
        agent_class="AOS_NATIVE",
        tool_name=boundary,
        result_sha=result_sha,
        module_ids=["RuntimeDeploy", "RuntimeSupervisor"],
        claims={
            "transition_id": transition_id,
            "status": "ABORTED",
            "reason": str(reason)[:1000],
            "recovered_state": dict(recovered_state),
        },
    )


def transition_marker(transition_id: str, operation: str) -> Dict[str, str]:
    return {
        "transition_state": "INCOMPLETE_HOLD",
        "transition_id": transition_id,
        "transition_operation": operation,
    }


def clear_transition_marker(value: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(value)
    for key in ("transition_state", "transition_id", "transition_operation"):
        result.pop(key, None)
    return result
