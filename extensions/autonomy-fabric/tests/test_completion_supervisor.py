"""Unit tests for Completion Supervisor (R6)."""

import pytest
from extensions.autonomy_fabric.run_registry import AgentRunRegistry, RunStatus
from extensions.autonomy_fabric.authority_router import AuthorityRouter
from extensions.autonomy_fabric.completion_supervisor import (
    CompletionSupervisor,
    ControllerReviewDisposition,
)


def test_completion_supervisor_revision_loop_and_max_escalation():
    registry = AgentRunRegistry()
    supervisor = CompletionSupervisor(registry, max_autonomous_revision_cycles=2)

    run = registry.create_run("p1", "DEV", "AUTH-1", "ctrl", "antigravity")
    registry.transition(run.run_id, RunStatus.STARTING)
    registry.transition(run.run_id, RunStatus.RUNNING)

    # Cycle 1: Revision requested for broken test
    res1 = supervisor.review_run(run.run_id, objective_defects=["test_failure: test_auth"])
    assert res1.disposition == ControllerReviewDisposition.REVISION_REQUIRED
    assert res1.revision_cycle_count == 1
    assert run.status == RunStatus.RUNNING

    # Cycle 2: Revision requested again
    res2 = supervisor.review_run(run.run_id, objective_defects=["test_failure: test_auth"])
    assert res2.disposition == ControllerReviewDisposition.REVISION_REQUIRED
    assert res2.revision_cycle_count == 2

    # Cycle 3: Exceeds max 2 cycles -> Escalates to HUMAN_REVIEW_REQUIRED_WITH_BLOCKERS
    res3 = supervisor.review_run(run.run_id, objective_defects=["test_failure: test_auth"])
    assert res3.disposition == ControllerReviewDisposition.HUMAN_REVIEW_REQUIRED_WITH_BLOCKERS
    assert res3.final_presentation_ready is False
    assert run.status == RunStatus.WAITING_HUMAN
