"""Resumable audit finding ingestion and context APIs."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

from aos.knowledge.context import build_context_pack
from aos.knowledge.index import rebuild_index
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.model import KnowledgeEventType


def record_audit_finding(
    ledger: KnowledgeLedger,
    *,
    project_id: str,
    finding_id: str,
    layer: str,
    severity: str,
    claim: str,
    evidence: Iterable[str],
    affected_modules: Iterable[str],
    affected_paths: Iterable[str],
    source_sha: str,
    runtime_sha: Optional[str] = None,
    status: str = "OPEN",
    supersedes: Iterable[str] = (),
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    event = ledger.append(
        KnowledgeEventType.AUDIT_FINDING,
        project_id=project_id,
        idempotency_key=f"audit-finding:{finding_id}",
        created_at=created_at,
        agent_class="AOS_NATIVE",
        tool_name="aos.knowledge.audit",
        base_sha=source_sha,
        result_sha=runtime_sha,
        module_ids=list(affected_modules),
        changed_paths=list(affected_paths),
        evidence_refs=list(evidence),
        supersedes=list(supersedes),
        claims={
            "finding_id": finding_id,
            "layer": layer,
            "severity": severity,
            "claim": claim,
            "status": status,
            "source_sha": source_sha,
            "runtime_sha": runtime_sha,
            "resolution": None,
            "resolved_at": None,
        },
    )
    rebuild_index(ledger)
    return event


def resolve_audit_finding(
    ledger: KnowledgeLedger,
    *,
    project_id: str,
    finding_id: str,
    resolution: str,
    source_sha: str,
    resolved_at: Optional[str] = None,
    evidence: Iterable[str] = (),
) -> Dict[str, Any]:
    resolution_key = f"audit-resolution:{finding_id}:{source_sha}"
    for existing in ledger.read_events():
        if existing.get("idempotency_key") == resolution_key:
            return dict(existing)
    unresolved = list_unresolved_audit_findings(ledger)
    if not any(item.get("claims", {}).get("finding_id") == finding_id for item in unresolved):
        raise ValueError(f"unresolved audit finding not found: {finding_id}")
    event = ledger.append(
        KnowledgeEventType.AUDIT_FINDING_RESOLVED,
        project_id=project_id,
        idempotency_key=resolution_key,
        created_at=resolved_at,
        agent_class="AOS_NATIVE",
        tool_name="aos.knowledge.audit",
        result_sha=source_sha,
        evidence_refs=list(evidence),
        supersedes=[finding_id],
        claims={
            "finding_id": finding_id,
            "status": "RESOLVED",
            "resolution": resolution,
            "resolved_at": resolved_at,
        },
    )
    rebuild_index(ledger)
    return event


def list_unresolved_audit_findings(
    ledger: KnowledgeLedger,
    *,
    layers: Optional[Iterable[str]] = None,
    severities: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    index = rebuild_index(ledger)
    allowed_layers = set(layers or ())
    allowed_severities = set(severities or ())
    findings = list(index["unresolved_audit_findings"].values())
    return [
        finding for finding in findings
        if (not allowed_layers or finding.get("claims", {}).get("layer") in allowed_layers)
        and (not allowed_severities or finding.get("claims", {}).get("severity") in allowed_severities)
    ]


def build_audit_context(
    ledger: KnowledgeLedger,
    *,
    project_id: str,
    layer: str,
    module_ids: Iterable[str],
    paths: Iterable[str],
    base_sha: str,
) -> Dict[str, Any]:
    pack = build_context_pack(
        ledger,
        project_id=project_id,
        task_class=f"AUDIT:{layer}",
        module_ids=module_ids,
        paths=paths,
        base_sha=base_sha,
    )
    pack["audit_layer"] = layer
    return pack
