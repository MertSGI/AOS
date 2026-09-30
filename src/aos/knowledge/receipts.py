"""Typed receipt helpers for agent, tool, verification, and runtime work."""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from aos.knowledge.index import rebuild_index
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.model import AgentClass, AuthorityClass, KnowledgeEventType


def record_receipt(
    ledger: KnowledgeLedger,
    event_type: KnowledgeEventType | str,
    *,
    project_id: str,
    idempotency_key: str,
    agent_class: AgentClass | str,
    tool_name: str,
    base_sha: Optional[str] = None,
    result_sha: Optional[str] = None,
    module_ids: Iterable[str] = (),
    changed_paths: Iterable[str] = (),
    read_paths: Iterable[str] = (),
    decisions: Iterable[str] = (),
    verification: Optional[Mapping[str, Any]] = None,
    evidence_refs: Iterable[str] = (),
    blockers: Iterable[str] = (),
    canonical_next_action: Optional[str] = None,
    claims: Optional[Mapping[str, Any]] = None,
    append_precondition: Optional[Callable[[Tuple[Dict[str, Any], ...]], None]] = None,
    **bindings: Any,
) -> Dict[str, Any]:
    """Append a receipt and synchronously refresh its derived index.

    Callers at high-impact acceptance boundaries intentionally do not catch
    local write errors: the boundary therefore fails closed.
    """
    event = ledger.append(
        event_type,
        project_id=project_id,
        idempotency_key=idempotency_key,
        authority_class=AuthorityClass.KNOWLEDGE_LEDGER,
        agent_class=AgentClass(agent_class).value,
        tool_name=tool_name,
        base_sha=base_sha,
        result_sha=result_sha,
        module_ids=list(module_ids),
        changed_paths=list(changed_paths),
        read_paths=list(read_paths),
        decision_ids=list(decisions),
        ci=dict(verification or {}),
        evidence_refs=list(evidence_refs),
        blockers=list(blockers),
        canonical_next_action=canonical_next_action,
        claims=dict(claims or {}),
        append_precondition=append_precondition,
        **bindings,
    )
    rebuild_index(ledger)
    return event


def record_implementation_receipt(ledger: KnowledgeLedger, **kwargs: Any) -> Dict[str, Any]:
    if not kwargs.get("base_sha") or not kwargs.get("result_sha"):
        raise ValueError("implementation receipts require exact base_sha and result_sha")
    return record_receipt(ledger, KnowledgeEventType.IMPLEMENTATION_RECEIPT, **kwargs)


def record_verification_receipt(ledger: KnowledgeLedger, **kwargs: Any) -> Dict[str, Any]:
    if not kwargs.get("result_sha"):
        raise ValueError("verification receipts require exact result_sha")
    return record_receipt(ledger, KnowledgeEventType.VERIFICATION_RECEIPT, **kwargs)


def record_candidate_materialization_receipt(ledger: KnowledgeLedger, **kwargs: Any) -> Dict[str, Any]:
    if not kwargs.get("result_sha"):
        raise ValueError("candidate materialization receipts require exact result_sha")
    return record_receipt(ledger, KnowledgeEventType.CANDIDATE_MATERIALIZATION_RECEIPT, **kwargs)


def record_live_promotion_receipt(ledger: KnowledgeLedger, **kwargs: Any) -> Dict[str, Any]:
    if kwargs.get("production") not in (None, "NO_GO"):
        raise ValueError("KCP promotion receipts cannot enable production")
    transition_id = str((kwargs.get("claims") or {}).get("transition_id") or "")
    if transition_id:
        from aos.knowledge.runtime_transitions import transition_completion_precondition
        kwargs["append_precondition"] = transition_completion_precondition(
            transition_id,
            event_type=KnowledgeEventType.LIVE_PROMOTION_RECEIPT,
            idempotency_key=str(kwargs.get("idempotency_key") or ""),
        )
    return record_receipt(ledger, KnowledgeEventType.LIVE_PROMOTION_RECEIPT, **kwargs)


def record_rollback_receipt(ledger: KnowledgeLedger, **kwargs: Any) -> Dict[str, Any]:
    transition_id = str((kwargs.get("claims") or {}).get("transition_id") or "")
    if transition_id:
        from aos.knowledge.runtime_transitions import transition_completion_precondition
        kwargs["append_precondition"] = transition_completion_precondition(
            transition_id,
            event_type=KnowledgeEventType.ROLLBACK_RECEIPT,
            idempotency_key=str(kwargs.get("idempotency_key") or ""),
        )
    return record_receipt(ledger, KnowledgeEventType.ROLLBACK_RECEIPT, **kwargs)


def record_runtime_transition_intent(ledger: KnowledgeLedger, **kwargs: Any) -> Dict[str, Any]:
    claims = kwargs.get("claims") or {}
    required = {"transition_id", "operation", "boundary", "target_state", "previous_state"}
    if required - set(claims):
        raise ValueError("runtime transition intent is missing required transition identity")
    if claims.get("operation") not in {"ACTIVATE", "PROMOTE", "ROLLBACK"}:
        raise ValueError("unsupported runtime transition operation")
    if not kwargs.get("result_sha"):
        raise ValueError("runtime transition intent requires an exact relevant source SHA")
    from aos.knowledge.runtime_transitions import transition_prepare_precondition
    kwargs["append_precondition"] = transition_prepare_precondition(str(claims["transition_id"]))
    return record_receipt(ledger, KnowledgeEventType.RUNTIME_TRANSITION_INTENT, **kwargs)


def record_runtime_transition_aborted(ledger: KnowledgeLedger, **kwargs: Any) -> Dict[str, Any]:
    claims = kwargs.get("claims") or {}
    if not claims.get("transition_id"):
        raise ValueError("runtime transition abort requires transition_id")
    from aos.knowledge.runtime_transitions import transition_abort_precondition
    kwargs["append_precondition"] = transition_abort_precondition(
        str(claims["transition_id"]),
        idempotency_key=str(kwargs.get("idempotency_key") or ""),
    )
    return record_receipt(ledger, KnowledgeEventType.RUNTIME_TRANSITION_ABORTED, **kwargs)
