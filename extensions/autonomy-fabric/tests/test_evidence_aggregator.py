"""Unit tests for Evidence Aggregator (R7)."""

import pytest
from extensions.autonomy_fabric.run_registry import AgentRunRegistry, RunStatus
from extensions.autonomy_fabric.evidence_aggregator import (
    EvidenceAggregator,
    EvidenceType,
    EvidenceRecord,
)


def test_evidence_indexing_and_claim_separation():
    registry = AgentRunRegistry()
    aggregator = EvidenceAggregator(registry)

    run = registry.create_run("p1", "DEV", "AUTH-1", "ctrl", "antigravity")

    # Record executor claim vs verifier verification
    claim = aggregator.record_evidence(
        run_id=run.run_id,
        phase="BUILD",
        project_id="p1",
        evidence_type=EvidenceType.CLAIM,
        producer="executor:agent-1",
        title="Agent claims feature works",
        payload={"claim": "100% tests pass"},
    )
    assert claim.evidence_type == EvidenceType.CLAIM

    verification = aggregator.record_evidence(
        run_id=run.run_id,
        phase="VERIFY",
        project_id="p1",
        evidence_type=EvidenceType.VERIFICATION,
        producer="verifier:pytest",
        title="Pytest verification pass",
        payload={"passed_count": 42},
    )
    assert verification.evidence_type == EvidenceType.VERIFICATION

    # Verify run updated with last evidence id
    updated_run = registry.get_run(run.run_id)
    assert updated_run.last_evidence_id == verification.evidence_id


def test_generate_project_summary():
    registry = AgentRunRegistry()
    aggregator = EvidenceAggregator(registry)

    run1 = registry.create_run("p1", "DEV", "AUTH-1", "ctrl", "antigravity")
    registry.transition(run1.run_id, RunStatus.STARTING)
    registry.transition(run1.run_id, RunStatus.RUNNING)
    registry.transition(run1.run_id, RunStatus.COMPLETED)

    run2 = registry.create_run("p1", "REVIEW", "AUTH-2", "ctrl", "antigravity")
    registry.transition(run2.run_id, RunStatus.STARTING)
    registry.transition(run2.run_id, RunStatus.RUNNING)
    registry.transition(run2.run_id, RunStatus.WAITING_HUMAN)

    summary = aggregator.generate_project_summary("p1")
    assert summary.total_runs == 2
    assert summary.completed_runs == [run1.run_id]
    assert summary.human_input_required_runs == [run2.run_id]
    assert summary.progress_percentage == 50.0
    assert "Review human-gated run(s)" in summary.next_safe_action
