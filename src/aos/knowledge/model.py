"""Versioned, bounded contracts for the AOS Knowledge Continuity Plane."""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, Mapping


CONTRACT_VERSION = "1.0.0"
MAX_EVENT_BYTES = 256 * 1024
MAX_COLLECTION_ITEMS = 512
MAX_TEXT_LENGTH = 16_384


class AuthorityClass(str, Enum):
    CANONICAL_GOVERNANCE = "CANONICAL_GOVERNANCE"
    OPERATIONAL_TRUTH = "OPERATIONAL_TRUTH"
    KNOWLEDGE_LEDGER = "KNOWLEDGE_LEDGER"
    SECOND_BRAIN_MIRROR = "SECOND_BRAIN_MIRROR"


class KnowledgeEventType(str, Enum):
    DECISION_ACCEPTED = "DECISION_ACCEPTED"
    DECISION_SUPERSEDED = "DECISION_SUPERSEDED"
    IMPLEMENTATION_RECEIPT = "IMPLEMENTATION_RECEIPT"
    VERIFICATION_RECEIPT = "VERIFICATION_RECEIPT"
    LIVE_PROMOTION_RECEIPT = "LIVE_PROMOTION_RECEIPT"
    ROLLBACK_RECEIPT = "ROLLBACK_RECEIPT"
    RUNTIME_TRANSITION_INTENT = "RUNTIME_TRANSITION_INTENT"
    RUNTIME_TRANSITION_ABORTED = "RUNTIME_TRANSITION_ABORTED"
    AUDIT_FINDING = "AUDIT_FINDING"
    AUDIT_FINDING_RESOLVED = "AUDIT_FINDING_RESOLVED"
    MODULE_RELATIONSHIP = "MODULE_RELATIONSHIP"
    HANDOFF = "HANDOFF"
    BLOCKER = "BLOCKER"
    RESOURCE_OBSERVATION = "RESOURCE_OBSERVATION"
    CONTEXT_PREFLIGHT_RECEIPT = "CONTEXT_PREFLIGHT_RECEIPT"
    SECOND_BRAIN_SYNC_RECEIPT = "SECOND_BRAIN_SYNC_RECEIPT"
    CANDIDATE_MATERIALIZATION_RECEIPT = "CANDIDATE_MATERIALIZATION_RECEIPT"
    CURRENT_TRUTH_OBSERVATION = "CURRENT_TRUTH_OBSERVATION"
    BOOTSTRAP = "BOOTSTRAP"


class AgentClass(str, Enum):
    CODEX = "CODEX"
    CLINE = "CLINE"
    ANTIGRAVITY = "ANTIGRAVITY"
    AOS_NATIVE = "AOS_NATIVE"
    CONTROLLER = "CONTROLLER"
    HUMAN_OPERATOR = "HUMAN_OPERATOR"
    OTHER_MODEL = "OTHER_MODEL"


class RelationshipType(str, Enum):
    DEPENDS_ON = "DEPENDS_ON"
    READS = "READS"
    WRITES = "WRITES"
    GOVERNS = "GOVERNS"
    VERIFY_BY = "VERIFY_BY"
    SUPERSEDES = "SUPERSEDES"
    MIRRORS_TO = "MIRRORS_TO"
    MUST_MATCH = "MUST_MATCH"
    MUST_NOT_USE = "MUST_NOT_USE"


EVENT_FIELDS = {
    "contract_version", "sequence", "event_id", "idempotency_key", "event_type",
    "created_at", "authority_class", "project_id", "objective_id", "task_id",
    "agent_id", "agent_class", "tool_name", "session_id", "base_sha", "result_sha",
    "repository", "branch", "module_ids", "changed_paths", "read_paths", "decision_ids",
    "supersedes", "superseded_by", "invariants", "claims", "evidence_refs", "ci",
    "runtime_evidence", "current_truth_observed_at", "current_truth_status", "blockers",
    "open_questions", "canonical_next_action", "risk_class", "production", "paid_fallback",
    "previous_hash", "content_hash",
}

LIST_FIELDS = {
    "module_ids", "changed_paths", "read_paths", "decision_ids", "supersedes",
    "superseded_by", "invariants", "evidence_refs", "blockers", "open_questions",
}

MAPPING_FIELDS = {"claims", "ci", "runtime_evidence"}
_SENSITIVE_KEY = re.compile(r"(secret|password|passwd|token|credential|cookie|authorization|api[_-]?key)", re.I)
_SECRET_VALUE = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~+/=-]{8,}|(?:sk|ghp|github_pat|AIza)[-_a-z0-9]{12,})"
)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


def sanitize_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Return a bounded JSON-compatible value with secret-bearing material removed."""
    if depth > 8:
        return "[TRUNCATED]"
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[REDACTED]", value[:MAX_TEXT_LENGTH])
    if isinstance(value, Mapping):
        return {
            str(child_key)[:256]: sanitize_value(child_value, key=str(child_key), depth=depth + 1)
            for child_key, child_value in list(value.items())[:MAX_COLLECTION_ITEMS]
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize_value(item, depth=depth + 1) for item in list(value)[:MAX_COLLECTION_ITEMS]]
    return sanitize_value(str(value), key=key, depth=depth + 1)


def normalize_event_fields(fields: Mapping[str, Any]) -> Dict[str, Any]:
    unknown = set(fields) - EVENT_FIELDS
    if unknown:
        raise ValueError(f"unknown knowledge-event fields: {sorted(unknown)}")
    normalized: Dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if key in LIST_FIELDS:
            if not isinstance(value, (list, tuple, set)):
                raise ValueError(f"{key} must be a list")
            normalized[key] = [str(item)[:2048] for item in list(value)[:MAX_COLLECTION_ITEMS]]
        elif key in MAPPING_FIELDS:
            if not isinstance(value, Mapping):
                raise ValueError(f"{key} must be an object")
            normalized[key] = sanitize_value(value, key=key)
        elif key in {"sequence"}:
            normalized[key] = int(value)
        else:
            normalized[key] = sanitize_value(value, key=key)
    return normalized


def validate_event(event: Mapping[str, Any]) -> None:
    required = {
        "contract_version", "sequence", "event_id", "idempotency_key", "event_type",
        "created_at", "authority_class", "project_id", "production", "paid_fallback",
        "previous_hash", "content_hash",
    }
    missing = required - set(event)
    if missing:
        raise ValueError(f"knowledge event missing fields: {sorted(missing)}")
    if set(event) - EVENT_FIELDS:
        raise ValueError("knowledge event contains unknown fields")
    if event["contract_version"] != CONTRACT_VERSION:
        raise ValueError("unsupported knowledge-event contract version")
    KnowledgeEventType(str(event["event_type"]))
    AuthorityClass(str(event["authority_class"]))
    if "agent_class" in event:
        AgentClass(str(event["agent_class"]))
    if int(event["sequence"]) < 1:
        raise ValueError("knowledge-event sequence must be positive")
    if not _HASH.fullmatch(str(event["event_id"])):
        raise ValueError("invalid knowledge-event id")
    if not _HASH.fullmatch(str(event["previous_hash"])) or not _HASH.fullmatch(str(event["content_hash"])):
        raise ValueError("invalid knowledge-event hash chain")
    if event["production"] != "NO_GO":
        raise ValueError("KCP cannot enable production")
    if event["paid_fallback"] != "DISABLED":
        raise ValueError("KCP cannot enable paid fallback")
    for sha_field in ("base_sha", "result_sha"):
        if sha_field in event and not _SHA.fullmatch(str(event[sha_field])):
            raise ValueError(f"{sha_field} must be an exact lowercase Git SHA")
    if event["authority_class"] == AuthorityClass.OPERATIONAL_TRUTH.value:
        if not event.get("current_truth_observed_at"):
            raise ValueError("operational observations require current_truth_observed_at")
