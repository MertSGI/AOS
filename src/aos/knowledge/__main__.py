"""Bounded command line interface for the Knowledge Continuity Plane."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional

from aos.knowledge.audit import list_unresolved_audit_findings, record_audit_finding, resolve_audit_finding
from aos.knowledge.bootstrap import DEFAULT_CANONICAL_INPUTS, bootstrap_canonical_sources
from aos.knowledge.context import build_context_pack
from aos.knowledge.index import rebuild_index, record_module_relationship
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.materialize import materialize_documents
from aos.knowledge.mirror import LocalOutboxMirror, read_sync_status, sync_materialized_documents
from aos.knowledge.model import AgentClass, AuthorityClass, KnowledgeEventType


def _json(value: str) -> Any:
    parsed = json.loads(value)
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AOS Knowledge Continuity Plane")
    parser.add_argument("--runtime-home", required=True)
    parser.add_argument("--project-id", default="AOS")
    sub = parser.add_subparsers(dest="command", required=True)

    append = sub.add_parser("append")
    append.add_argument("--event-type", required=True, choices=[item.value for item in KnowledgeEventType])
    append.add_argument("--idempotency-key", required=True)
    append.add_argument("--authority-class", default=AuthorityClass.KNOWLEDGE_LEDGER.value,
                        choices=[item.value for item in AuthorityClass])
    append.add_argument("--agent-class", default=AgentClass.AOS_NATIVE.value,
                        choices=[item.value for item in AgentClass])
    append.add_argument("--tool-name", default="aos.knowledge.cli")
    append.add_argument("--fields-json", default="{}", type=_json)

    sub.add_parser("rebuild")
    sub.add_parser("materialize")
    sub.add_parser("status")
    sub.add_parser("audit-list")
    sub.add_parser("sync-status")

    context = sub.add_parser("context")
    context.add_argument("--task-class", required=True)
    context.add_argument("--module", action="append", default=[])
    context.add_argument("--path", action="append", default=[])
    context.add_argument("--base-sha", required=True)
    context.add_argument("--record-receipt", action="store_true")

    materialize = sub.choices["materialize"]
    materialize.add_argument("--sync-mode", choices=["LOCAL_ONLY", "DRIVE_MIRROR", "NOTEBOOK_ENTERPRISE"])

    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--repo-root", required=True)
    bootstrap.add_argument("--source-sha", required=True)
    bootstrap.add_argument("--path", action="append", default=[])

    relationship = sub.add_parser("relationship")
    relationship.add_argument("--source", required=True)
    relationship.add_argument("--type", required=True)
    relationship.add_argument("--target", required=True)

    audit = sub.add_parser("audit-record")
    audit.add_argument("--finding-json", required=True, type=_json)
    resolve = sub.add_parser("audit-resolve")
    resolve.add_argument("--finding-id", required=True)
    resolve.add_argument("--resolution", required=True)
    resolve.add_argument("--source-sha", required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    ledger = KnowledgeLedger(Path(args.runtime_home) / "knowledge")
    if args.command == "append":
        result = ledger.append(
            args.event_type,
            project_id=args.project_id,
            idempotency_key=args.idempotency_key,
            authority_class=args.authority_class,
            agent_class=args.agent_class,
            tool_name=args.tool_name,
            **args.fields_json,
        )
        rebuild_index(ledger)
    elif args.command == "rebuild":
        result = rebuild_index(ledger)
    elif args.command == "context":
        result = build_context_pack(
            ledger, project_id=args.project_id, task_class=args.task_class,
            module_ids=args.module, paths=args.path, base_sha=args.base_sha,
            record_receipt=args.record_receipt,
        )
    elif args.command == "materialize":
        result = materialize_documents(ledger)
        if args.sync_mode:
            mirror = LocalOutboxMirror(ledger.root / "sync", mode=args.sync_mode,
                                       provider="GOOGLE_DRIVE" if args.sync_mode == "DRIVE_MIRROR" else args.sync_mode)
            result["sync"] = sync_materialized_documents(ledger, mirror, project_id=args.project_id)
    elif args.command == "status":
        result = {**ledger.verify(), "index": rebuild_index(ledger)}
    elif args.command == "audit-list":
        result = {"findings": list_unresolved_audit_findings(ledger)}
    elif args.command == "sync-status":
        result = read_sync_status(ledger)
    elif args.command == "bootstrap":
        result = {"events": bootstrap_canonical_sources(
            ledger, repo_root=Path(args.repo_root), project_id=args.project_id,
            source_sha=args.source_sha, paths=args.path or DEFAULT_CANONICAL_INPUTS,
        )}
    elif args.command == "relationship":
        result = record_module_relationship(
            ledger, project_id=args.project_id, source=args.source,
            relationship=args.type, target=args.target,
        )
    elif args.command == "audit-record":
        result = record_audit_finding(ledger, project_id=args.project_id, **args.finding_json)
    elif args.command == "audit-resolve":
        result = resolve_audit_finding(
            ledger, project_id=args.project_id, finding_id=args.finding_id,
            resolution=args.resolution, source_sha=args.source_sha,
        )
    else:
        raise AssertionError(args.command)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
