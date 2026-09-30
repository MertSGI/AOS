"""Deterministic Markdown projections for local and external second brains."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from aos.knowledge.index import rebuild_index
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.model import CONTRACT_VERSION


DOCUMENT_NAMES = (
    "AOS_CURRENT_KNOWLEDGE.md",
    "AOS_ARCHITECTURE_AND_MODULE_MAP.md",
    "AOS_DECISIONS.md",
    "AOS_WORK_JOURNAL.md",
    "AOS_OPEN_GAPS_AND_AUDIT.md",
    "AOS_LIVE_AND_VERIFICATION_HISTORY.md",
)


def _header(index: Mapping[str, Any], title: str) -> list[str]:
    return [
        f"# {title}", "",
        f"- generated_at: `{index.get('generated_at') or 'EMPTY_LEDGER'}`",
        f"- ledger_sequence: `{index.get('last_sequence', 0)}`",
        f"- ledger_head_hash: `{index.get('head_hash')}`",
        f"- contract_version: `{CONTRACT_VERSION}`",
        "- authority: `DERIVED_FROM_APPEND_ONLY_KNOWLEDGE_LEDGER`",
        "- operational_truth: `FRESH_CURRENT_TRUTH_REQUIRED`",
        "- second_brain: `ADVISORY_ONLY_NOT_DECISION_AUTHORITY`",
        "- production: `NO_GO`",
        "- paid_fallback: `DISABLED`",
        "",
    ]


def _event_line(event: Mapping[str, Any]) -> str:
    summary = event.get("claims") or {}
    identities = []
    if event.get("decision_ids"):
        identities.append(f"decisions={','.join(event['decision_ids'])}")
    if event.get("module_ids"):
        identities.append(f"modules={','.join(event['module_ids'])}")
    if event.get("base_sha"):
        identities.append(f"base_sha={event['base_sha']}")
    if event.get("result_sha"):
        identities.append(f"result_sha={event['result_sha']}")
    suffix = f" ({'; '.join(identities)})" if identities else ""
    return f"- seq `{event.get('sequence')}` `{event.get('event_type')}` `{event.get('event_id')}`{suffix}: {json.dumps(summary, ensure_ascii=False, sort_keys=True)}"


def _section(lines: list[str], title: str, events: Iterable[Mapping[str, Any]]) -> None:
    lines.extend([f"## {title}", ""])
    populated = list(events)
    lines.extend(_event_line(event) for event in populated)
    if not populated:
        lines.append("- None recorded.")
    lines.append("")


def render_documents(ledger: KnowledgeLedger) -> Dict[str, str]:
    events = list(ledger.read_events())
    index = rebuild_index(ledger)
    current = _header(index, "AOS Current Knowledge")
    _section(current, "Current Decisions", index["current_accepted_decisions"].values())
    _section(current, "Active Blockers", index["active_blockers"].values())
    current.extend(["## Canonical Next Actions", ""])
    current.extend(
        f"- `{project}`: {action}" for project, action in index["project_canonical_next_action"].items()
    )
    if not index["project_canonical_next_action"]:
        current.append("- None recorded.")
    current.extend(["", "## Historical Operational Observations", "",
                    "Ledger observations are historical receipts and never replace a fresh CURRENT_TRUTH query.", ""])
    if index["latest_operational_observation_historical_only"]:
        current.append(_event_line(index["latest_operational_observation_historical_only"]))

    architecture = _header(index, "AOS Architecture and Module Map")
    _section(architecture, "Current Module Relationships", index["module_relationships"])

    decisions = _header(index, "AOS Decisions")
    _section(decisions, "Current Accepted Decisions", index["current_accepted_decisions"].values())
    _section(decisions, "Historical Superseded Decisions", index["superseded_decisions"].values())

    journal = _header(index, "AOS Work Journal")
    _section(journal, "Historical Work Receipts", [
        event for event in events if event["event_type"] in {
            "IMPLEMENTATION_RECEIPT", "CANDIDATE_MATERIALIZATION_RECEIPT", "HANDOFF", "BOOTSTRAP"
        }
    ])

    gaps = _header(index, "AOS Open Gaps and Audit")
    _section(gaps, "Current Unresolved Audit Findings", index["unresolved_audit_findings"].values())
    _section(gaps, "Current Active Blockers", index["active_blockers"].values())
    _section(gaps, "Historical Resolved Audit Findings", index["resolved_audit_findings"].values())

    live = _header(index, "AOS Live and Verification History")
    _section(live, "Historical Verification Receipts", [
        event for event in events if event["event_type"] == "VERIFICATION_RECEIPT"
    ])
    _section(live, "Historical Promotion and Rollback Receipts", [
        event for event in events if event["event_type"] in {"LIVE_PROMOTION_RECEIPT", "ROLLBACK_RECEIPT"}
    ])
    _section(live, "Prepared or Incomplete Runtime Transitions", [
        event for event in index["unresolved_runtime_transitions"].values()
    ])
    _section(live, "Historical Aborted Runtime Transitions", [
        event for event in events if event["event_type"] == "RUNTIME_TRANSITION_ABORTED"
    ])
    live.extend(["## Current Runtime State", "",
                 "Not cached here. Generate fresh CURRENT_TRUTH before operational decisions.", ""])

    documents = {
        DOCUMENT_NAMES[0]: "\n".join(current).rstrip() + "\n",
        DOCUMENT_NAMES[1]: "\n".join(architecture).rstrip() + "\n",
        DOCUMENT_NAMES[2]: "\n".join(decisions).rstrip() + "\n",
        DOCUMENT_NAMES[3]: "\n".join(journal).rstrip() + "\n",
        DOCUMENT_NAMES[4]: "\n".join(gaps).rstrip() + "\n",
        DOCUMENT_NAMES[5]: "\n".join(live).rstrip() + "\n",
    }
    return documents


def materialize_documents(ledger: KnowledgeLedger) -> Dict[str, Any]:
    documents = render_documents(ledger)
    output = ledger.root / "materialized"
    output.mkdir(parents=True, exist_ok=True)
    for name, content in documents.items():
        path = output / name
        temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    index = rebuild_index(ledger)
    return {
        "status": "READY",
        "document_count": len(documents),
        "documents": sorted(documents),
        "ledger_sequence": index["last_sequence"],
        "production": "NO_GO",
        "paid_fallback": "DISABLED",
    }
