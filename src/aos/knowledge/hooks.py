"""Small integration hooks for existing AOS execution and runtime boundaries."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from aos.knowledge.context import build_context_pack
from aos.knowledge.index import rebuild_index
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.model import AuthorityClass, KnowledgeEventType
from aos.knowledge.receipts import record_receipt


def knowledge_root_for_runtime(runtime_home: Path) -> Path:
    return Path(runtime_home).expanduser().resolve() / "knowledge"


def ledger_for_runtime(runtime_home: Path) -> KnowledgeLedger:
    return KnowledgeLedger(knowledge_root_for_runtime(runtime_home))


def configured_ledger() -> Optional[KnowledgeLedger]:
    """Resolve opt-in integration for boundaries that do not own runtime_home."""
    configured = os.environ.get("AOS_KNOWLEDGE_HOME")
    return KnowledgeLedger(Path(configured)) if configured else None


def ledger_from_runtime_config(config: Mapping[str, Any]) -> Optional[KnowledgeLedger]:
    runtime_root = config.get("runtime_root")
    if not runtime_root:
        return None
    return ledger_for_runtime(Path(str(runtime_root)).expanduser().resolve().parent)


def execution_context_preflight(
    ledger: KnowledgeLedger,
    *,
    project_id: str,
    task_class: str,
    module_ids: Iterable[str],
    paths: Iterable[str],
    base_sha: str,
) -> Dict[str, Any]:
    return build_context_pack(
        ledger,
        project_id=project_id,
        task_class=task_class,
        module_ids=module_ids,
        paths=paths,
        base_sha=base_sha,
        record_receipt=True,
    )


def record_current_truth_transition(
    runtime_home: Path,
    projection: Mapping[str, Any],
    *,
    project_id: str = "AOS",
) -> Optional[Dict[str, Any]]:
    """Record only meaningful degraded/contradictory observations as historical."""
    status = str(projection.get("overall_status") or "UNKNOWN")
    contradictions = projection.get("contradictions") or []
    if status == "KNOWN" and not contradictions:
        return None
    observed_at = str(projection.get("observed_at") or "")
    digest = hashlib.sha256(
        json.dumps({"status": status, "contradictions": contradictions}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    ledger = ledger_for_runtime(runtime_home)
    for previous in reversed(ledger.read_events()):
        if previous.get("event_type") != KnowledgeEventType.CURRENT_TRUTH_OBSERVATION.value:
            continue
        if previous.get("claims", {}).get("observation_fingerprint") == digest:
            return None
        break
    event = ledger.append(
        KnowledgeEventType.CURRENT_TRUTH_OBSERVATION,
        project_id=project_id,
        idempotency_key=f"current-truth:{observed_at}:{digest}",
        authority_class=AuthorityClass.OPERATIONAL_TRUTH,
        agent_class="AOS_NATIVE",
        tool_name="aos.current_truth",
        current_truth_observed_at=observed_at,
        current_truth_status=status,
        runtime_evidence={"contradictions": contradictions, "refresh_status": projection.get("refresh_status")},
        claims={
            "historical_after_observation": True,
            "does_not_define_current_state": True,
            "observation_fingerprint": digest,
        },
    )
    rebuild_index(ledger)
    return event


def record_configured_receipt(event_type: KnowledgeEventType | str, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Write through the configured ledger; local failures intentionally propagate."""
    ledger = configured_ledger()
    if ledger is None:
        return None
    return record_receipt(ledger, event_type, **kwargs)
