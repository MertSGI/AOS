from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aos.knowledge.audit import (
    list_unresolved_audit_findings,
    record_audit_finding,
    resolve_audit_finding,
)
from aos.knowledge.bootstrap import bootstrap_canonical_sources
from aos.knowledge.context import build_context_pack
from aos.knowledge.index import ModuleGraph, derive_index, rebuild_index, record_module_relationship
from aos.knowledge.ledger import KnowledgeLedger, KnowledgeLedgerCorruptionError
from aos.knowledge.materialize import materialize_documents, render_documents
from aos.knowledge.mirror import (
    LocalOutboxMirror,
    NotebookAdvisoryResponse,
    SecondBrainMirror,
    record_advisory_candidate,
    sync_materialized_documents,
)
from aos.knowledge.model import AuthorityClass, KnowledgeEventType
from aos.knowledge.receipts import record_implementation_receipt
from aos.validate import validate_document


SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def ledger(tmp_path: Path) -> KnowledgeLedger:
    return KnowledgeLedger(tmp_path / "runtime" / "knowledge", clock=lambda: "2026-09-30T10:00:00Z")


def append(ledger: KnowledgeLedger, key: str, event_type: str = "HANDOFF", **fields):
    return ledger.append(
        event_type,
        project_id="AOS",
        idempotency_key=key,
        agent_class="CODEX",
        tool_name="pytest",
        **fields,
    )


def test_ledger_append_is_durable_and_append_only(ledger: KnowledgeLedger):
    first = append(ledger, "one", claims={"claim": "one"})
    before = ledger.log_path.read_bytes()
    second = append(ledger, "two", claims={"claim": "two"})
    after = ledger.log_path.read_bytes()
    assert after.startswith(before)
    assert KnowledgeLedger(ledger.root).read_events() == (first, second)
    assert first["content_hash"] == second["previous_hash"]


def test_ledger_detects_historical_rewrite(ledger: KnowledgeLedger):
    append(ledger, "one", claims={"claim": "one"})
    ledger.log_path.write_text(ledger.log_path.read_text("utf-8").replace('"one"', '"two"', 1), "utf-8")
    with pytest.raises(KnowledgeLedgerCorruptionError):
        ledger.read_events()


def test_index_rebuild_is_reproducible(ledger: KnowledgeLedger):
    append(ledger, "decision", "DECISION_ACCEPTED", decision_ids=["D-1"], claims={"claim": "accepted"})
    first = rebuild_index(ledger)
    ledger.index_path.unlink()
    second = rebuild_index(ledger)
    assert first == second


def test_decision_supersession_and_old_decision_cannot_override_new(ledger: KnowledgeLedger):
    append(ledger, "old", "DECISION_ACCEPTED", decision_ids=["D-OLD"], claims={"value": "old"})
    append(
        ledger, "new", "DECISION_ACCEPTED", decision_ids=["D-NEW"], supersedes=["D-OLD"],
        claims={"value": "new"},
    )
    index = rebuild_index(ledger)
    assert "D-OLD" not in index["current_accepted_decisions"]
    assert index["current_accepted_decisions"]["D-NEW"]["claims"]["value"] == "new"
    assert index["superseded_decisions"]["D-OLD"]["claims"]["value"] == "old"


def test_historical_current_truth_never_becomes_current_truth(ledger: KnowledgeLedger):
    append(
        ledger, "truth", "CURRENT_TRUTH_OBSERVATION",
        authority_class=AuthorityClass.OPERATIONAL_TRUTH,
        current_truth_observed_at="2026-09-30T09:00:00Z",
        current_truth_status="KNOWN",
        claims={"runtime_sha": SHA_A, "historical_after_observation": True},
    )
    index = rebuild_index(ledger)
    assert index["operational_truth_must_be_refreshed"] is True
    assert index["latest_operational_observation_historical_only"]["claims"]["runtime_sha"] == SHA_A
    pack = build_context_pack(
        ledger, project_id="AOS", task_class="READ", module_ids=["RuntimeEngine"], paths=[], base_sha=SHA_A,
    )
    assert pack["operational_truth"]["status"] == "NOT_QUERIED"


def test_context_preflight_is_relevant_and_bounded(ledger: KnowledgeLedger):
    for index in range(80):
        append(
            ledger, f"impl-{index}", "IMPLEMENTATION_RECEIPT", base_sha=SHA_A, result_sha=SHA_B,
            module_ids=["Relevant" if index % 2 == 0 else "Other"],
            claims={"summary": "x" * 1000, "index": index},
        )
    pack = build_context_pack(
        ledger, project_id="AOS", task_class="CHANGE", module_ids=["Relevant"], paths=[],
        base_sha=SHA_A, byte_limit=8192,
    )
    encoded = json.dumps(pack, sort_keys=True).encode()
    assert len(encoded) <= 8192 + 100
    assert all("Relevant" in item.get("module_ids", []) for item in pack["latest_implementations"])


def test_module_graph_relationships_are_queryable(ledger: KnowledgeLedger):
    record_module_relationship(
        ledger, project_id="AOS", source="RuntimeDeploy", relationship="MUST_MATCH", target="CandidateManifest",
    )
    graph = ModuleGraph(rebuild_index(ledger))
    assert graph.query("RuntimeDeploy", direction="out")[0]["claims"]["target"] == "CandidateManifest"
    assert graph.query("CandidateManifest", direction="in")[0]["claims"]["source"] == "RuntimeDeploy"


def test_receipt_includes_exact_sha_bindings(ledger: KnowledgeLedger):
    event = record_implementation_receipt(
        ledger, project_id="AOS", idempotency_key="receipt", agent_class="CODEX", tool_name="pytest",
        base_sha=SHA_A, result_sha=SHA_B, module_ids=["KCP"], changed_paths=["src/aos/knowledge/ledger.py"],
    )
    assert event["base_sha"] == SHA_A and event["result_sha"] == SHA_B
    with pytest.raises(ValueError):
        record_implementation_receipt(
            ledger, project_id="AOS", idempotency_key="bad", agent_class="CODEX", tool_name="pytest",
            base_sha="short", result_sha=SHA_B,
        )


def test_audit_finding_survives_rebuild_and_resolution_is_historical(ledger: KnowledgeLedger):
    record_audit_finding(
        ledger, project_id="AOS", finding_id="F-1", layer="L1", severity="HIGH",
        claim="gap", evidence=["proof"], affected_modules=["KCP"], affected_paths=["src/aos/knowledge"],
        source_sha=SHA_A,
    )
    assert list_unresolved_audit_findings(ledger)[0]["claims"]["finding_id"] == "F-1"
    resolve_audit_finding(
        ledger, project_id="AOS", finding_id="F-1", resolution="fixed", source_sha=SHA_B,
        resolved_at="2026-09-30T11:00:00Z",
    )
    index = rebuild_index(ledger)
    assert list_unresolved_audit_findings(ledger) == []
    assert "F-1" in index["resolved_audit_findings"]


def test_materialized_docs_are_deterministic_and_distinguish_history(ledger: KnowledgeLedger):
    append(ledger, "old", "DECISION_ACCEPTED", decision_ids=["D-OLD"], claims={"value": "old"})
    append(ledger, "new", "DECISION_ACCEPTED", decision_ids=["D-NEW"], supersedes=["D-OLD"], claims={"value": "new"})
    first = render_documents(ledger)
    materialize_documents(ledger)
    second = render_documents(ledger)
    assert first == second
    decisions = first["AOS_DECISIONS.md"]
    assert "Current Accepted Decisions" in decisions
    assert "Historical Superseded Decisions" in decisions
    assert "D-NEW" in decisions and "D-OLD" in decisions


def test_secrets_are_redacted_from_ledger_and_materialized_docs(ledger: KnowledgeLedger):
    append(
        ledger, "secret", claims={"api_key": "sk-super-secret-material-123456", "note": "Bearer abcdefghijklmnop"},
    )
    docs = render_documents(ledger)
    combined = "\n".join(docs.values())
    assert "super-secret" not in ledger.log_path.read_text("utf-8")
    assert "abcdefghijklmnop" not in combined
    assert "[REDACTED]" in combined


class FailingMirror(SecondBrainMirror):
    def publish_document(self, **kwargs):
        raise ConnectionError("offline")

    def publish_manifest(self, manifest):
        raise ConnectionError("offline")

    def health(self):
        return {"status": "UNAVAILABLE"}


def test_second_brain_failure_preserves_local_ledger(ledger: KnowledgeLedger):
    append(ledger, "local")
    before = ledger.read_events()
    result = sync_materialized_documents(ledger, FailingMirror(), project_id="AOS")
    after = ledger.read_events()
    assert result["status"] == "SECOND_BRAIN_SYNC_PENDING"
    assert after[: len(before)] == before
    assert after[-1]["event_type"] == "SECOND_BRAIN_SYNC_RECEIPT"


def test_local_ledger_failure_prevents_high_impact_acceptance(ledger: KnowledgeLedger, monkeypatch):
    monkeypatch.setattr(ledger, "append", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        record_implementation_receipt(
            ledger, project_id="AOS", idempotency_key="fail", agent_class="CODEX", tool_name="pytest",
            base_sha=SHA_A, result_sha=SHA_B,
        )


def test_optional_drive_mode_is_fail_safe_and_local_outbox_remains(ledger: KnowledgeLedger):
    append(ledger, "local")
    mirror = LocalOutboxMirror(ledger.root / "sync", mode="DRIVE_MIRROR", provider="GOOGLE_DRIVE")
    result = sync_materialized_documents(ledger, mirror, project_id="AOS")
    assert result["status"] == "SECOND_BRAIN_SYNC_PENDING"
    assert mirror.health()["external_transport"] == "UNAVAILABLE"
    assert list((ledger.root / "sync" / "outbox").glob("*.md"))


def test_notebook_advisory_cannot_directly_mutate_authority(ledger: KnowledgeLedger):
    response = NotebookAdvisoryResponse(
        status="ANSWERED", answer="Consider D-2", citations=("doc-1",), source_ids=("source-1",),
        observed_at="2026-09-30T10:00:00Z", provider="NOTEBOOK_TEST",
    )
    event = record_advisory_candidate(ledger, project_id="AOS", response=response, idempotency_key="advisory")
    assert event["authority_class"] == "SECOND_BRAIN_MIRROR"
    assert event["event_type"] == "HANDOFF"
    assert rebuild_index(ledger)["current_accepted_decisions"] == {}


def test_kcp_does_not_mutate_product_command_state(tmp_path: Path):
    command_state = tmp_path / "runtime" / "state" / "commands" / "cmd-1" / "state.json"
    command_state.parent.mkdir(parents=True)
    command_state.write_text('{"state":"RUNNING"}\n', "utf-8")
    before = command_state.read_bytes()
    local = KnowledgeLedger(tmp_path / "runtime" / "knowledge")
    append(local, "read-write")
    rebuild_index(local)
    materialize_documents(local)
    assert command_state.read_bytes() == before


def test_production_and_paid_fallback_are_immutable_safety_posture(ledger: KnowledgeLedger):
    event = append(ledger, "safe")
    assert event["production"] == "NO_GO" and event["paid_fallback"] == "DISABLED"
    with pytest.raises(ValueError):
        append(ledger, "unsafe-prod", production="GO")
    with pytest.raises(ValueError):
        append(ledger, "unsafe-paid", paid_fallback="ENABLED")


def test_windows_paths_are_preserved_and_queryable(ledger: KnowledgeLedger):
    path = r"C:\Projects\AOS\src\aos\knowledge\ledger.py"
    append(ledger, "windows", "IMPLEMENTATION_RECEIPT", base_sha=SHA_A, result_sha=SHA_B,
           module_ids=["KCP"], changed_paths=[path])
    pack = build_context_pack(
        ledger, project_id="AOS", task_class="CHANGE", module_ids=["KCP"], paths=[path], base_sha=SHA_A,
    )
    assert pack["latest_implementations"]
    assert "C:/Projects/AOS" in pack["paths"][0]


def test_concurrent_append_locking_is_safe(ledger: KnowledgeLedger):
    def work(index: int):
        return append(ledger, f"concurrent-{index}", claims={"index": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(40)))
    events = ledger.read_events()
    assert [event["sequence"] for event in events] == list(range(1, 41))
    assert len({event["event_id"] for event in events}) == 40


def test_duplicate_receipt_is_idempotent_and_conflict_is_rejected(ledger: KnowledgeLedger):
    first = append(ledger, "duplicate", claims={"value": 1})
    second = append(ledger, "duplicate", claims={"value": 1})
    assert first == second and len(ledger.read_events()) == 1
    with pytest.raises(Exception, match="idempotency key conflicts"):
        append(ledger, "duplicate", claims={"value": 2})


def test_bootstrap_marks_provenance_without_manufacturing_history(ledger: KnowledgeLedger, tmp_path: Path):
    repo = tmp_path / "repo"
    source = repo / "docs" / "project-control" / "STATE.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"accepted":true}\n', "utf-8")
    events = bootstrap_canonical_sources(
        ledger, repo_root=repo, project_id="AOS", source_sha=SHA_A,
        paths=["docs/project-control/STATE.json", "missing.md"],
    )
    bootstrap = [event for event in events if event["event_type"] == "BOOTSTRAP"]
    assert len(bootstrap) == 1
    assert bootstrap[0]["claims"]["provenance"] == "BOOTSTRAP_FROM_CANONICAL_SOURCE"
    assert bootstrap[0]["claims"]["historical_chat_canonical"] is False


def test_fresh_current_truth_provider_is_used_only_at_preflight(ledger: KnowledgeLedger):
    pack = build_context_pack(
        ledger, project_id="AOS", task_class="PROMOTION", module_ids=["RuntimeDeploy"], paths=[], base_sha=SHA_A,
        current_truth_provider=lambda: {
            "overall_status": "KNOWN", "refresh_status": "READY",
            "observed_at": "2026-09-30T12:00:00Z", "contradictions": [],
        },
    )
    assert pack["operational_truth"]["authority"] == "FRESH_CURRENT_TRUTH"
    assert ledger.read_events() == ()


def test_derived_index_can_be_recreated_from_ledger_alone(ledger: KnowledgeLedger):
    append(ledger, "blocker", "BLOCKER", claims={"blocker_id": "B-1", "status": "ACTIVE"})
    expected = derive_index(ledger.read_events())
    ledger.index_path.write_text("{}", "utf-8")
    assert rebuild_index(ledger) == expected


def test_strict_versioned_event_schema_accepts_event_and_rejects_unknown_field(ledger: KnowledgeLedger):
    event = append(ledger, "schema")
    assert validate_document("knowledge_event", event).is_valid
    assert not validate_document("knowledge_event", {**event, "unknown": True}).is_valid


def test_required_event_types_and_authority_classes_are_explicit():
    required_types = {
        "DECISION_ACCEPTED", "DECISION_SUPERSEDED", "IMPLEMENTATION_RECEIPT",
        "VERIFICATION_RECEIPT", "LIVE_PROMOTION_RECEIPT", "ROLLBACK_RECEIPT",
        "AUDIT_FINDING", "AUDIT_FINDING_RESOLVED", "MODULE_RELATIONSHIP", "HANDOFF",
        "BLOCKER", "RESOURCE_OBSERVATION", "CONTEXT_PREFLIGHT_RECEIPT",
        "SECOND_BRAIN_SYNC_RECEIPT",
    }
    assert required_types <= {item.value for item in KnowledgeEventType}
    assert {item.value for item in AuthorityClass} == {
        "CANONICAL_GOVERNANCE", "OPERATIONAL_TRUTH", "KNOWLEDGE_LEDGER", "SECOND_BRAIN_MIRROR",
    }
