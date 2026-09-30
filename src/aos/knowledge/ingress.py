"""Provider-independent ingress for externally completed accepted work."""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional

from aos.knowledge.accepted_work import ACCEPTED_VERIFICATION_STATUSES
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.model import AgentClass
from aos.knowledge.receipts import (
    record_implementation_receipt,
    record_verification_receipt,
)


_SHA = re.compile(r"^[0-9a-f]{40}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


def ingest_accepted_work(
    ledger: KnowledgeLedger,
    *,
    project_id: str,
    agent_class: AgentClass | str,
    tool_name: str,
    base_sha: str,
    result_sha: str,
    repository: str,
    branch: str,
    module_ids: Iterable[str],
    changed_paths: Iterable[str],
    evidence_refs: Iterable[str],
    verification_status: str,
    canonical_next_action: str,
    idempotency_key: str,
    task_id: Optional[str] = None,
    objective_id: Optional[str] = None,
    session_id: Optional[str] = None,
    decision_ids: Iterable[str] = (),
    blockers: Iterable[str] = (),
    open_questions: Iterable[str] = (),
    preflight_hash: Optional[str] = None,
    bootstrap_transition: bool = False,
) -> Dict[str, Any]:
    """Archive exact, verified external mutation evidence as two durable receipts."""
    producer = AgentClass(agent_class).value
    paths = [str(value).replace("\\", "/") for value in changed_paths if str(value)]
    modules = [str(value) for value in module_ids if str(value)]
    evidence = [str(value) for value in evidence_refs if str(value)]
    status = str(verification_status).upper()
    if not _SHA.fullmatch(str(base_sha)):
        raise ValueError("accepted work requires exact lowercase base SHA")
    if not _SHA.fullmatch(str(result_sha)):
        raise ValueError("accepted work requires exact lowercase result SHA")
    if not str(tool_name).strip():
        raise ValueError("accepted work requires producer tool identity")
    if not paths:
        raise ValueError("accepted mutated work requires changed-path scope")
    if status not in ACCEPTED_VERIFICATION_STATUSES:
        raise ValueError("verification status is not acceptable for accepted-work coverage")
    if not str(repository).strip() or not str(branch).strip():
        raise ValueError("accepted work requires repository and branch provenance")
    if not str(canonical_next_action).strip():
        raise ValueError("accepted work requires canonical next action")
    if preflight_hash is not None and not _HASH.fullmatch(str(preflight_hash)):
        raise ValueError("preflight hash must be an exact lowercase SHA-256")
    if bootstrap_transition and not evidence:
        raise ValueError("bootstrap transition ingress requires explicit evidence")

    provenance = "BOOTSTRAP_ACCEPTED_EXTERNAL_WORK" if bootstrap_transition else "EXTERNAL_ACCEPTED_WORK"
    bindings = {
        "project_id": project_id,
        "agent_class": producer,
        "tool_name": str(tool_name),
        "base_sha": str(base_sha),
        "result_sha": str(result_sha),
        "repository": str(repository),
        "branch": str(branch),
        "module_ids": modules,
        "changed_paths": paths,
        "evidence_refs": evidence,
        "canonical_next_action": str(canonical_next_action),
        "task_id": task_id,
        "objective_id": objective_id,
        "session_id": session_id,
        "decisions": list(decision_ids),
        "blockers": list(blockers),
        "open_questions": list(open_questions),
    }
    claims = {
        "ingress_contract": "EXTERNAL_AGENT_COMPLETION_V1",
        "provenance": provenance,
        "kcp_preflight_hash": preflight_hash,
        "does_not_grant_git_or_decision_authority": True,
    }
    implementation = record_implementation_receipt(
        ledger,
        idempotency_key=f"{idempotency_key}:implementation",
        claims=claims,
        **bindings,
    )
    verification = record_verification_receipt(
        ledger,
        idempotency_key=f"{idempotency_key}:verification",
        verification={"status": status},
        claims={**claims, "accepted_work_coverage": True},
        **bindings,
    )
    state = ledger.verify()
    result = {
        "status": "ACCEPTED_WORK_RECEIPTED",
        "implementation_receipt_id": implementation["event_id"],
        "verification_receipt_id": verification["event_id"],
        "ledger_sequence": state["last_sequence"],
        "ledger_head_hash": state["head_hash"],
        "result_sha": result_sha,
        "archival_status": "KCP_ARCHIVED",
        "bootstrap_transition": bootstrap_transition,
    }
    result.update({
        "KCP_ARCHIVAL_STATUS": result["archival_status"],
        "KCP_IMPLEMENTATION_RECEIPT_ID": result["implementation_receipt_id"],
        "KCP_VERIFICATION_RECEIPT_ID": result["verification_receipt_id"],
        "KCP_LEDGER_SEQUENCE": result["ledger_sequence"],
        "KCP_RESULT_SHA": result_sha,
        "KCP_PREFLIGHT_HASH": preflight_hash,
    })
    return result
