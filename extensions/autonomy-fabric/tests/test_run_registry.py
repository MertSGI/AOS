"""Unit tests for Agent Run Registry (R1)."""

import pytest
from extensions.autonomy_fabric.run_registry import (
    AgentRunRegistry,
    RunStatus,
    InvalidStateTransitionError,
    RunIdentity,
)


def test_create_run_and_journal():
    registry = AgentRunRegistry()
    run = registry.create_run(
        project_id="proj-1",
        run_type="TEST_RUN",
        authority_id="AUTH-123",
        controller_id="ctrl-1",
        agent_provider="antigravity",
    )
    assert run.status == RunStatus.QUEUED
    assert run.project_id == "proj-1"
    
    events = registry.journal.get_events_for_run(run.run_id)
    assert len(events) == 1
    assert events[0].event_type == "RUN_CREATED"


def test_valid_state_transitions():
    registry = AgentRunRegistry()
    run = registry.create_run(
        project_id="proj-1",
        run_type="TEST_RUN",
        authority_id="AUTH-123",
        controller_id="ctrl-1",
        agent_provider="antigravity",
    )

    registry.transition(run.run_id, RunStatus.STARTING, phase="BOOT")
    assert run.status == RunStatus.STARTING

    registry.transition(run.run_id, RunStatus.RUNNING, phase="EXEC")
    assert run.status == RunStatus.RUNNING
    assert run.started_at is not None

    registry.transition(run.run_id, RunStatus.WAITING_AGENT)
    assert run.status == RunStatus.WAITING_AGENT

    registry.transition(run.run_id, RunStatus.RUNNING)
    assert run.status == RunStatus.RUNNING

    registry.transition(run.run_id, RunStatus.COMPLETED, phase="DONE")
    assert run.status == RunStatus.COMPLETED
    assert run.completed_at is not None


def test_invalid_state_transition_fails_closed():
    registry = AgentRunRegistry()
    run = registry.create_run(
        project_id="proj-1",
        run_type="TEST_RUN",
        authority_id="AUTH-123",
        controller_id="ctrl-1",
        agent_provider="antigravity",
    )

    # QUEUED directly to COMPLETED is illegal
    with pytest.raises(InvalidStateTransitionError):
        registry.transition(run.run_id, RunStatus.COMPLETED)

    # Status remains unchanged
    assert run.status == RunStatus.QUEUED


def test_journal_replay_rebuilds_state():
    registry = AgentRunRegistry()
    run = registry.create_run(
        project_id="proj-1",
        run_type="TEST_RUN",
        authority_id="AUTH-123",
        controller_id="ctrl-1",
        agent_provider="antigravity",
    )

    registry.transition(run.run_id, RunStatus.STARTING)
    registry.transition(run.run_id, RunStatus.RUNNING, phase="PHASE_1")
    registry.update_run_metadata(run.run_id, {"agent_conversation_id": "conv-abc-123"})
    registry.transition(run.run_id, RunStatus.WAITING_HUMAN)

    rebuilt = registry.rebuild_run(run.run_id)
    assert rebuilt is not None
    assert rebuilt.status == RunStatus.WAITING_HUMAN
    assert rebuilt.current_phase == "PHASE_1"
    assert rebuilt.agent_conversation_id == "conv-abc-123"
    assert rebuilt.human_input_required is True
