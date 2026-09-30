"""Mandatory accepted-work receipt coverage queries."""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping

from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.model import KnowledgeEventType


ACCEPTED_VERIFICATION_STATUSES = frozenset({"PASS", "SUCCESS", "VERIFIED"})


class AcceptedWorkCoverageError(RuntimeError):
    """An exact source result lacks mandatory durable KCP coverage."""


def _normalized_paths(values: Iterable[str]) -> set[str]:
    return {str(value).replace("\\", "/") for value in values if str(value)}


def _covers_scope(
    event: Mapping[str, Any], *, module_ids: set[str], changed_paths: set[str]
) -> bool:
    if module_ids and not module_ids.issubset(set(event.get("module_ids") or [])):
        return False
    event_paths = _normalized_paths(event.get("changed_paths") or [])
    return not changed_paths or changed_paths.issubset(event_paths)


def accepted_work_coverage(
    ledger: KnowledgeLedger,
    *,
    project_id: str,
    result_sha: str,
    module_ids: Iterable[str] = (),
    changed_paths: Iterable[str] = (),
) -> Dict[str, Any]:
    """Return exact-SHA implementation and successful verification coverage."""
    required_modules = {str(value) for value in module_ids if str(value)}
    required_paths = _normalized_paths(changed_paths)
    implementations = []
    verifications = []
    for event in ledger.read_events():
        if event.get("project_id") != project_id or event.get("result_sha") != result_sha:
            continue
        if not _covers_scope(
            event, module_ids=required_modules, changed_paths=required_paths
        ):
            continue
        if event.get("event_type") == KnowledgeEventType.IMPLEMENTATION_RECEIPT.value:
            implementations.append(event)
        elif event.get("event_type") == KnowledgeEventType.VERIFICATION_RECEIPT.value:
            status = str((event.get("ci") or {}).get("status") or "").upper()
            if status in ACCEPTED_VERIFICATION_STATUSES:
                verifications.append(event)

    covered = bool(implementations and verifications)
    state = ledger.verify()
    return {
        "status": "COVERED" if covered else "MISSING_ACCEPTED_WORK_COVERAGE",
        "covered": covered,
        "project_id": project_id,
        "result_sha": result_sha,
        "implementation_receipt_ids": [item["event_id"] for item in implementations],
        "verification_receipt_ids": [item["event_id"] for item in verifications],
        "ledger_sequence": state["last_sequence"],
        "ledger_head_hash": state["head_hash"],
    }


def assert_accepted_work_receipted(
    ledger: KnowledgeLedger,
    *,
    project_id: str,
    result_sha: str,
    module_ids: Iterable[str] = (),
    changed_paths: Iterable[str] = (),
) -> Dict[str, Any]:
    coverage = accepted_work_coverage(
        ledger,
        project_id=project_id,
        result_sha=result_sha,
        module_ids=module_ids,
        changed_paths=changed_paths,
    )
    if not coverage["covered"]:
        raise AcceptedWorkCoverageError(
            f"KCP_ACCEPTED_WORK_COVERAGE_REQUIRED:{project_id}:{result_sha}"
        )
    return coverage
