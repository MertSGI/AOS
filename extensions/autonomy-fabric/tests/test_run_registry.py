"""Unit tests for Agent Run Registry (R1)."""

import pytest
from extensions.autonomy_fabric.run_registry import (
    AgentRunRegistry,
    RunStatus,
    InvalidStateTransitionError,
    RunIdentity,
    FileRunJournal,
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


def test_agentic_checkpoint_fields_round_trip_through_file_journal(tmp_path):
    journal_path = tmp_path / "runs.jsonl"
    registry = AgentRunRegistry(FileRunJournal(str(journal_path)))
    run = registry.create_run(
        project_id="proj-1",
        run_type="AGENTIC",
        authority_id="AUTH-123",
        controller_id="ctrl-1",
        agent_provider="codex_cli",
        resource_id="local-codex",
        backend_id="codex_cli",
        source_sha="a" * 40,
        checkpoint_id="checkpoint-1",
        objective_id="objective-1",
    )
    registry.record_agentic_checkpoint(
        run.run_id,
        session_or_thread_id="11111111-1111-4111-8111-111111111111",
        workspace_fingerprint="b" * 64,
        source_sha="a" * 40,
        checkpoint_id="checkpoint-2",
        last_successful_turn=1,
        completed_work_unit_ids=["work-1"],
        completed_work_unit_signatures={"work-1": "c" * 64},
        artifact_hashes={"artifact.json": "d" * 64},
        last_successful_artifact={"path": "artifact.json", "sha256": "d" * 64},
    )

    restarted = AgentRunRegistry(FileRunJournal(str(journal_path)))
    replayed = restarted.get_run(run.run_id)

    assert replayed is not None
    assert replayed.session_or_thread_id == "11111111-1111-4111-8111-111111111111"
    assert replayed.workspace_fingerprint == "b" * 64
    assert replayed.last_successful_turn == 1
    assert replayed.completed_work_unit_ids == ["work-1"]
    assert replayed.artifact_hashes == {"artifact.json": "d" * 64}
    with pytest.raises(InvalidStateTransitionError, match="completed work unit"):
        restarted.assert_work_unit_not_completed(run.run_id, "work-1")
    with pytest.raises(InvalidStateTransitionError, match="signature"):
        restarted.assert_work_unit_not_completed(run.run_id, "new-work", "c" * 64)


def test_stale_agentic_session_is_superseded_and_never_selected_implicitly():
    registry = AgentRunRegistry()
    run = registry.create_run(
        project_id="proj-1",
        run_type="AGENTIC",
        authority_id="AUTH-123",
        controller_id="ctrl-1",
        agent_provider="antigravity",
    )
    registry.update_run_metadata(run.run_id, {
        "session_or_thread_id": "old-session",
        "workspace_fingerprint": "a" * 64,
    })

    registry.supersede_agentic_session(
        run.run_id,
        session_id="old-session",
        reason="SUPERSEDED_STALE_WORKSPACE",
    )

    assert run.session_or_thread_id is None
    assert run.agent_conversation_id is None
    assert run.superseded_session_ids == ["old-session"]
    assert run.last_terminal_event == "SUPERSEDED_STALE_WORKSPACE"
