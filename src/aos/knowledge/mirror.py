"""Provider-independent second-brain mirror and advisory boundaries."""
from __future__ import annotations

import hashlib
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from aos.knowledge.index import rebuild_index
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.materialize import materialize_documents
from aos.knowledge.model import AuthorityClass, KnowledgeEventType
from aos.runtime_store import atomic_json, read_json


class SecondBrainMirror(ABC):
    @abstractmethod
    def publish_document(self, *, logical_name: str, content: bytes, ledger_sequence: int) -> Mapping[str, Any]: ...

    @abstractmethod
    def publish_manifest(self, manifest: Mapping[str, Any]) -> Mapping[str, Any]: ...

    @abstractmethod
    def health(self) -> Mapping[str, Any]: ...


class LocalOutboxMirror(SecondBrainMirror):
    """Always-available local outbox; DRIVE_MIRROR transport remains optional."""

    def __init__(self, sync_root: Path, *, mode: str = "LOCAL_ONLY", provider: str = "LOCAL") -> None:
        if mode not in {"LOCAL_ONLY", "DRIVE_MIRROR", "NOTEBOOK_ENTERPRISE"}:
            raise ValueError("unsupported second-brain mirror mode")
        self.root = Path(sync_root).expanduser().resolve()
        self.mode = mode
        self.provider = provider
        (self.root / "outbox").mkdir(parents=True, exist_ok=True)

    def publish_document(self, *, logical_name: str, content: bytes, ledger_sequence: int) -> Mapping[str, Any]:
        digest = hashlib.sha256(content).hexdigest()
        target = self.root / "outbox" / logical_name
        temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return {
            "document_id": hashlib.sha256(logical_name.encode("utf-8")).hexdigest(),
            "logical_name": logical_name,
            "content_hash": digest,
            "ledger_sequence": ledger_sequence,
            "sync_state": "LOCAL_READY" if self.mode == "LOCAL_ONLY" else "PENDING_TRANSPORT",
            "remote_provider": self.provider,
            "remote_reference": None,
            "last_sync_attempt": None,
            "last_sync_success": None,
            "last_error_class": None if self.mode == "LOCAL_ONLY" else "TRANSPORT_NOT_CONFIGURED",
        }

    def publish_manifest(self, manifest: Mapping[str, Any]) -> Mapping[str, Any]:
        atomic_json(self.root / "manifest.json", dict(manifest))
        return dict(manifest)

    def health(self) -> Mapping[str, Any]:
        return {
            "mode": self.mode,
            "status": "READY" if self.mode == "LOCAL_ONLY" else "DEGRADED",
            "local_outbox": "READY",
            "external_transport": "NOT_REQUIRED" if self.mode == "LOCAL_ONLY" else "UNAVAILABLE",
            "advisory_only": True,
        }


@dataclass(frozen=True)
class NotebookAdvisoryResponse:
    status: str
    answer: str
    citations: tuple[str, ...]
    source_ids: tuple[str, ...]
    observed_at: str
    provider: str
    advisory_only: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class NotebookAdvisoryAdapter(ABC):
    """Read-only boundary. Implementations cannot receive authority stores."""

    @abstractmethod
    def ask(self, *, question: str, source_ids: tuple[str, ...]) -> NotebookAdvisoryResponse: ...


def record_advisory_candidate(
    ledger: KnowledgeLedger,
    *,
    project_id: str,
    response: NotebookAdvisoryResponse,
    idempotency_key: str,
) -> Dict[str, Any]:
    if not response.advisory_only:
        raise ValueError("notebook responses must remain advisory-only")
    event = ledger.append(
        KnowledgeEventType.HANDOFF,
        project_id=project_id,
        idempotency_key=idempotency_key,
        authority_class=AuthorityClass.SECOND_BRAIN_MIRROR,
        agent_class="OTHER_MODEL",
        tool_name=response.provider,
        claims={
            "candidate_only": True,
            "answer": response.answer,
            "citations": list(response.citations),
            "source_ids": list(response.source_ids),
            "status": response.status,
            "advisory_only": True,
        },
    )
    rebuild_index(ledger)
    return event


def sync_materialized_documents(
    ledger: KnowledgeLedger,
    mirror: SecondBrainMirror,
    *,
    project_id: str,
) -> Dict[str, Any]:
    """Publish a local outbox. Mirror failures never remove local knowledge."""
    materialize_documents(ledger)
    index = rebuild_index(ledger)
    records = []
    state = "READY"
    error_class: Optional[str] = None
    try:
        for path in sorted((ledger.root / "materialized").glob("*.md")):
            record = dict(mirror.publish_document(
                logical_name=path.name,
                content=path.read_bytes(),
                ledger_sequence=index["last_sequence"],
            ))
            record.setdefault("generated_at", index["generated_at"])
            record.setdefault("last_sync_attempt", None)
            record.setdefault("last_sync_success", None)
            record.setdefault("last_error_class", None)
            records.append(record)
        manifest = {
            "contract_version": "1.0.0",
            "generated_at": index["generated_at"],
            "ledger_sequence": index["last_sequence"],
            "documents": records,
            "sync_state": "READY" if all(row["sync_state"] == "LOCAL_READY" for row in records) else "PENDING",
        }
        mirror.publish_manifest(manifest)
        if manifest["sync_state"] == "PENDING":
            state = "SECOND_BRAIN_SYNC_PENDING"
    except Exception as exc:
        state = "SECOND_BRAIN_SYNC_PENDING"
        error_class = exc.__class__.__name__
        manifest = {
            "contract_version": "1.0.0", "generated_at": index["generated_at"],
            "ledger_sequence": index["last_sequence"], "documents": records,
            "sync_state": "DEGRADED", "last_error_class": error_class,
        }
        atomic_json(ledger.root / "sync" / "manifest.json", manifest)
    ledger.append(
        KnowledgeEventType.SECOND_BRAIN_SYNC_RECEIPT,
        project_id=project_id,
        idempotency_key=f"mirror:{index['head_hash']}:{state}",
        authority_class=AuthorityClass.SECOND_BRAIN_MIRROR,
        agent_class="AOS_NATIVE",
        tool_name="aos.knowledge.mirror",
        claims={"sync_state": state, "last_error_class": error_class, "document_count": len(records)},
    )
    rebuild_index(ledger)
    return {**manifest, "status": state, "local_ledger_preserved": True}


def read_sync_status(ledger: KnowledgeLedger) -> Dict[str, Any]:
    manifest = read_json(ledger.root / "sync" / "manifest.json", {})
    return manifest or {"sync_state": "NOT_RUN", "advisory_only": True}
