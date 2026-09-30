"""Deterministic, relevant, size-bounded KCP context preflight."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from aos.knowledge.index import ModuleGraph, rebuild_index
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.model import CONTRACT_VERSION, KnowledgeEventType


MAX_CONTEXT_BYTES = 32_768


def _overlaps(ref: Mapping[str, Any], modules: set[str], paths: set[str]) -> bool:
    ref_modules = set(ref.get("module_ids", []))
    ref_paths = set(ref.get("changed_paths", [])) | set(ref.get("read_paths", []))
    return bool(not ref_modules and not ref_paths or ref_modules & modules or ref_paths & paths)


def _bounded(pack: Dict[str, Any], byte_limit: int) -> Dict[str, Any]:
    trim_order = [
        "latest_verifications", "latest_implementations", "module_relationships",
        "unresolved_audit_findings", "active_blockers", "current_decisions",
    ]
    while len(json.dumps(pack, ensure_ascii=False, sort_keys=True).encode("utf-8")) > byte_limit:
        trimmed = False
        for key in trim_order:
            value = pack.get(key)
            if isinstance(value, list) and value:
                value.pop()
                trimmed = True
                break
        if not trimmed:
            raise ValueError("mandatory KCP context fields exceed byte limit")
    return pack


def build_context_pack(
    ledger: KnowledgeLedger,
    *,
    project_id: str,
    task_class: str,
    module_ids: Iterable[str],
    paths: Iterable[str],
    base_sha: str,
    current_truth_provider: Optional[Callable[[], Mapping[str, Any]]] = None,
    byte_limit: int = MAX_CONTEXT_BYTES,
    record_receipt: bool = False,
) -> Dict[str, Any]:
    modules = {str(item) for item in module_ids}
    path_set = {str(item).replace("\\", "/") for item in paths}
    index = rebuild_index(ledger)
    graph = ModuleGraph(index)
    relationships = []
    for module in sorted(modules):
        relationships.extend(graph.query(module))
    relationships = list({item["event_id"]: item for item in relationships}.values())

    operational: Dict[str, Any] = {
        "status": "NOT_QUERIED",
        "authority": "FRESH_CURRENT_TRUTH_REQUIRED",
        "historical_ledger_observation_is_not_current": True,
    }
    if current_truth_provider is not None:
        fresh = dict(current_truth_provider())
        operational = {
            "status": fresh.get("overall_status", "UNKNOWN"),
            "observed_at": fresh.get("observed_at"),
            "refresh_status": fresh.get("refresh_status"),
            "contradictions": fresh.get("contradictions", [])[:32],
            "authority": "FRESH_CURRENT_TRUTH",
        }

    relevant = lambda values: [item for item in values if _overlaps(item, modules, path_set)]
    pack: Dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "project_id": project_id,
        "task_class": task_class,
        "base_sha": base_sha,
        "module_ids": sorted(modules),
        "paths": sorted(path_set),
        "ledger_sequence": index["last_sequence"],
        "ledger_head_hash": index["head_hash"],
        "authority_statement": {
            "governance": "Git docs/project-control",
            "operations": "fresh CURRENT_TRUTH",
            "continuity": "append-only Knowledge Ledger",
            "second_brain": "advisory mirror only",
        },
        "current_decisions": relevant(list(index["current_accepted_decisions"].values())),
        "relevant_invariants": sorted({
            invariant
            for event in ledger.read_events()
            if _overlaps(event, modules, path_set)
            for invariant in event.get("invariants", [])
        }),
        "module_relationships": sorted(relationships, key=lambda item: item["sequence"]),
        "active_blockers": relevant(list(index["active_blockers"].values())),
        "unresolved_audit_findings": relevant(list(index["unresolved_audit_findings"].values())),
        "latest_implementations": relevant(list(index["latest_accepted_implementation_by_module"].values())),
        "latest_verifications": relevant(list(index["latest_accepted_verification_by_module"].values())),
        "operational_truth": operational,
        "supersession_warnings": sorted(index["superseded_decisions"]),
        "canonical_next_action": index["project_canonical_next_action"].get(project_id),
        "production": "NO_GO",
        "paid_fallback": "DISABLED",
    }
    pack = _bounded(pack, max(4096, min(int(byte_limit), MAX_CONTEXT_BYTES)))
    fingerprint_body = json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    pack["context_hash"] = hashlib.sha256(fingerprint_body).hexdigest()
    if record_receipt:
        ledger.append(
            KnowledgeEventType.CONTEXT_PREFLIGHT_RECEIPT,
            project_id=project_id,
            idempotency_key=f"context:{pack['context_hash']}",
            agent_class="AOS_NATIVE",
            tool_name="aos.knowledge.context",
            base_sha=base_sha,
            module_ids=sorted(modules),
            read_paths=sorted(path_set),
            claims={"task_class": task_class, "context_hash": pack["context_hash"]},
        )
        rebuild_index(ledger)
    return pack
