"""Explicit provenance-preserving bootstrap from canonical sources."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable, List

from aos.knowledge.index import rebuild_index, seed_default_relationships
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.model import AuthorityClass, KnowledgeEventType


DEFAULT_CANONICAL_INPUTS = (
    "docs/project-control/STATE.json",
    "docs/project-control/DECISIONS.md",
    "docs/project-control/EVIDENCE.jsonl",
    "docs/project-control/UPDATE_PROTOCOL.md",
    "docs/project-control/AOS_CURRENT_TRUTH.json",
    "docs/project-control/CHARTER.md",
    "docs/runtime-v1/CONTRACT.md",
    "docs/architecture/AOS_END_TO_END_REFERENCE_ARCHITECTURE.md",
)


def bootstrap_canonical_sources(
    ledger: KnowledgeLedger,
    *,
    repo_root: Path,
    project_id: str,
    source_sha: str,
    paths: Iterable[str] = DEFAULT_CANONICAL_INPUTS,
) -> List[Dict[str, Any]]:
    root = Path(repo_root).expanduser().resolve()
    events = []
    for relative in paths:
        candidate = (root / relative).resolve()
        if root not in candidate.parents or not candidate.is_file():
            continue
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        events.append(ledger.append(
            KnowledgeEventType.BOOTSTRAP,
            project_id=project_id,
            idempotency_key=f"bootstrap:{relative}:{digest}",
            authority_class=AuthorityClass.CANONICAL_GOVERNANCE,
            agent_class="AOS_NATIVE",
            tool_name="aos.knowledge.bootstrap",
            base_sha=source_sha,
            read_paths=[relative.replace("\\", "/")],
            evidence_refs=[f"sha256:{digest}"],
            claims={
                "provenance": "BOOTSTRAP_FROM_CANONICAL_SOURCE",
                "source_path": relative.replace("\\", "/"),
                "source_content_hash": digest,
                "historical_chat_canonical": False,
            },
        ))
    seed_default_relationships(ledger, project_id=project_id)
    rebuild_index(ledger)
    return events
