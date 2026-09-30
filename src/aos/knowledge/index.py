"""Deterministic knowledge-index and module-relationship projections."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from aos.knowledge.ledger import KnowledgeLedger, ZERO_HASH
from aos.knowledge.model import CONTRACT_VERSION, KnowledgeEventType, RelationshipType
from aos.runtime_store import atomic_json, read_json


DEFAULT_MODULE_RELATIONSHIPS = (
    ("CURRENT_TRUTH", "READS", "RuntimeEngine"),
    ("RuntimeSupervisor", "GOVERNS", "ControlPanel"),
    ("PlatformRecovery", "DEPENDS_ON", "SourceRepairFactory"),
    ("PlatformRecovery", "DEPENDS_ON", "OperationsRepositoryAuthority"),
    ("RuntimeDeploy", "MUST_MATCH", "CandidateManifest"),
    ("LARI", "WRITES", "ProductWorkspace"),
    ("LARI_UI_V2", "WRITES", "ProductWorkspace"),
    ("KCP", "GOVERNS", "KnowledgeLedger"),
    ("KCP", "MIRRORS_TO", "DriveMirror"),
    ("KCP", "MIRRORS_TO", "SecondBrainAdapter"),
)


def _event_ref(event: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: event[key]
        for key in (
            "event_id", "sequence", "event_type", "created_at", "project_id", "module_ids",
            "decision_ids", "claims", "base_sha", "result_sha", "changed_paths", "evidence_refs",
            "current_truth_observed_at", "current_truth_status", "canonical_next_action",
        )
        if key in event
    }


def derive_index(events: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    ordered = list(events)
    current_decisions: Dict[str, Dict[str, Any]] = {}
    superseded_decisions: Dict[str, Dict[str, Any]] = {}
    blockers: Dict[str, Dict[str, Any]] = {}
    findings: Dict[str, Dict[str, Any]] = {}
    resolved_findings: Dict[str, Dict[str, Any]] = {}
    relationships: Dict[str, Dict[str, Any]] = {}
    latest_impl: Dict[str, Dict[str, Any]] = {}
    latest_verification: Dict[str, Dict[str, Any]] = {}
    bindings: Dict[str, Dict[str, Any]] = {}
    questions: Dict[str, Dict[str, Any]] = {}
    next_actions: Dict[str, str] = {}
    latest_promotion: Optional[Dict[str, Any]] = None
    unresolved_transitions: Dict[str, Dict[str, Any]] = {}
    completed_transitions: Dict[str, Dict[str, Any]] = {}
    aborted_transitions: Dict[str, Dict[str, Any]] = {}
    latest_operational_observation: Optional[Dict[str, Any]] = None

    for event in ordered:
        ref = _event_ref(event)
        event_type = event["event_type"]
        for superseded in event.get("supersedes", []):
            if superseded in current_decisions:
                superseded_decisions[superseded] = current_decisions.pop(superseded)
            blockers.pop(superseded, None)
            questions.pop(superseded, None)
            if superseded in findings:
                resolved_findings[superseded] = findings.pop(superseded)
        if event_type == KnowledgeEventType.DECISION_ACCEPTED.value:
            for decision_id in event.get("decision_ids", []):
                previous = current_decisions.pop(decision_id, None)
                if previous:
                    superseded_decisions[f"{decision_id}@{previous['sequence']}"] = previous
                current_decisions[decision_id] = ref
        elif event_type == KnowledgeEventType.DECISION_SUPERSEDED.value:
            for decision_id in event.get("supersedes", []) + event.get("decision_ids", []):
                previous = current_decisions.pop(decision_id, None)
                if previous:
                    superseded_decisions[decision_id] = previous
        elif event_type == KnowledgeEventType.BLOCKER.value:
            blocker_id = str(event.get("claims", {}).get("blocker_id") or event["event_id"])
            if str(event.get("claims", {}).get("status", "ACTIVE")).upper() == "RESOLVED":
                blockers.pop(blocker_id, None)
            else:
                blockers[blocker_id] = ref
        elif event_type == KnowledgeEventType.AUDIT_FINDING.value:
            finding_id = str(event.get("claims", {}).get("finding_id") or event["event_id"])
            findings[finding_id] = ref
        elif event_type == KnowledgeEventType.AUDIT_FINDING_RESOLVED.value:
            finding_id = str(event.get("claims", {}).get("finding_id") or "")
            previous = findings.pop(finding_id, None) or resolved_findings.get(finding_id)
            resolved_findings[finding_id] = {"finding": previous, "resolution": ref}
        elif event_type == KnowledgeEventType.MODULE_RELATIONSHIP.value:
            claims = event.get("claims", {})
            edge_key = f"{claims.get('source')}|{claims.get('relationship')}|{claims.get('target')}"
            relationships[edge_key] = ref
        elif event_type == KnowledgeEventType.IMPLEMENTATION_RECEIPT.value:
            for module_id in event.get("module_ids", []):
                latest_impl[module_id] = ref
        elif event_type in {
            KnowledgeEventType.VERIFICATION_RECEIPT.value,
            KnowledgeEventType.CANDIDATE_MATERIALIZATION_RECEIPT.value,
        }:
            for module_id in event.get("module_ids", []):
                latest_verification[module_id] = ref
        elif event_type == KnowledgeEventType.LIVE_PROMOTION_RECEIPT.value:
            latest_promotion = ref
        if event_type == KnowledgeEventType.RUNTIME_TRANSITION_INTENT.value:
            transition_id = str(event.get("claims", {}).get("transition_id") or "")
            if transition_id:
                unresolved_transitions[transition_id] = ref
        elif event_type in {
            KnowledgeEventType.LIVE_PROMOTION_RECEIPT.value,
            KnowledgeEventType.ROLLBACK_RECEIPT.value,
        }:
            transition_id = str(event.get("claims", {}).get("transition_id") or "")
            if transition_id:
                unresolved_transitions.pop(transition_id, None)
                completed_transitions[transition_id] = ref
        elif event_type == KnowledgeEventType.RUNTIME_TRANSITION_ABORTED.value:
            transition_id = str(event.get("claims", {}).get("transition_id") or "")
            if transition_id:
                unresolved_transitions.pop(transition_id, None)
                aborted_transitions[transition_id] = ref
        if event.get("base_sha") or event.get("result_sha"):
            binding_key = "|".join(event.get("module_ids") or [event["project_id"]])
            bindings[binding_key] = ref
        for question in event.get("open_questions", []):
            questions[question] = ref
        if event.get("canonical_next_action"):
            next_actions[event["project_id"]] = str(event["canonical_next_action"])
        if event.get("authority_class") == "OPERATIONAL_TRUTH":
            latest_operational_observation = ref

    high_water = ordered[-1] if ordered else None
    return {
        "contract_version": CONTRACT_VERSION,
        "derived_from": "knowledge/ledger.jsonl",
        "last_sequence": high_water["sequence"] if high_water else 0,
        "head_hash": high_water["content_hash"] if high_water else ZERO_HASH,
        "generated_at": high_water["created_at"] if high_water else None,
        "current_accepted_decisions": dict(sorted(current_decisions.items())),
        "superseded_decisions": dict(sorted(superseded_decisions.items())),
        "active_blockers": dict(sorted(blockers.items())),
        "unresolved_audit_findings": dict(sorted(findings.items())),
        "resolved_audit_findings": dict(sorted(resolved_findings.items())),
        "module_relationships": [relationships[key] for key in sorted(relationships)],
        "latest_accepted_implementation_by_module": dict(sorted(latest_impl.items())),
        "latest_accepted_verification_by_module": dict(sorted(latest_verification.items())),
        "latest_live_promotion": latest_promotion,
        "unresolved_runtime_transitions": dict(sorted(unresolved_transitions.items())),
        "completed_runtime_transitions": dict(sorted(completed_transitions.items())),
        "aborted_runtime_transitions": dict(sorted(aborted_transitions.items())),
        "project_canonical_next_action": dict(sorted(next_actions.items())),
        "source_runtime_sha_bindings": dict(sorted(bindings.items())),
        "current_unresolved_questions": dict(sorted(questions.items())),
        "latest_operational_observation_historical_only": latest_operational_observation,
        "operational_truth_must_be_refreshed": True,
        "production": "NO_GO",
        "paid_fallback": "DISABLED",
    }


def rebuild_index(ledger: KnowledgeLedger) -> Dict[str, Any]:
    index = derive_index(ledger.read_events())
    atomic_json(ledger.index_path, index)
    return index


class ModuleGraph:
    def __init__(self, index: Mapping[str, Any]) -> None:
        self.index = index

    @classmethod
    def from_path(cls, path: Path) -> "ModuleGraph":
        return cls(read_json(path, {}))

    def query(
        self,
        module_id: str,
        *,
        relationship_types: Optional[Iterable[str]] = None,
        direction: str = "both",
    ) -> List[Dict[str, Any]]:
        allowed = {RelationshipType(item).value for item in relationship_types} if relationship_types else None
        result = []
        for ref in self.index.get("module_relationships", []):
            claims = ref.get("claims", {})
            relationship = claims.get("relationship")
            source_match = claims.get("source") == module_id and direction in {"both", "out"}
            target_match = claims.get("target") == module_id and direction in {"both", "in"}
            if (source_match or target_match) and (allowed is None or relationship in allowed):
                result.append(ref)
        return sorted(result, key=lambda item: (item.get("sequence", 0), item.get("event_id", "")))


def record_module_relationship(
    ledger: KnowledgeLedger,
    *,
    project_id: str,
    source: str,
    relationship: RelationshipType | str,
    target: str,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    relation = RelationshipType(relationship).value
    event = ledger.append(
        KnowledgeEventType.MODULE_RELATIONSHIP,
        project_id=project_id,
        idempotency_key=idempotency_key or f"module:{source}:{relation}:{target}",
        module_ids=[source, target],
        claims={"source": source, "relationship": relation, "target": target},
        agent_class="AOS_NATIVE",
        tool_name="aos.knowledge",
    )
    rebuild_index(ledger)
    return event


def seed_default_relationships(ledger: KnowledgeLedger, *, project_id: str = "AOS") -> List[Dict[str, Any]]:
    return [
        record_module_relationship(
            ledger,
            project_id=project_id,
            source=source,
            relationship=relationship,
            target=target,
        )
        for source, relationship, target in DEFAULT_MODULE_RELATIONSHIPS
    ]
