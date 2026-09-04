"""Live Supervision Proof & Boundary Check (R9)."""

import pytest
from extensions.autonomy_fabric.run_registry import AgentRunRegistry, RunStatus
from extensions.autonomy_fabric.antigravity_adapter import FakeAntigravityAdapter, AntigravityStatus
from extensions.autonomy_fabric.supervisor import ParallelSupervisor


def test_r9_deterministic_two_conversation_supervision_proof():
    """Proves two concurrent conversations receive distinct IDs, are observed, and resumed."""
    registry = AgentRunRegistry()
    adapter = FakeAntigravityAdapter()
    adapter.default_status = AntigravityStatus.WAITING  # Set status to WAITING to test resumption flow
    supervisor = ParallelSupervisor(registry, adapter, max_concurrent_active_runs=4)

    # Launch Run A (Conversation 1)
    run_a = supervisor.launch_run(
        project_id="proof-proj",
        run_type="PROOF_A",
        authority_id="AUTH-PROOF-1",
        controller_id="ctrl-proof",
        agent_provider="antigravity",
        workspace_path="/tmp/proof_ws_a",
        branch="proof/a",
        initial_prompt="Harmless read query A",
    )

    # Launch Run B (Conversation 2)
    run_b = supervisor.launch_run(
        project_id="proof-proj",
        run_type="PROOF_B",
        authority_id="AUTH-PROOF-2",
        controller_id="ctrl-proof",
        agent_provider="antigravity",
        workspace_path="/tmp/proof_ws_b",
        branch="proof/b",
        initial_prompt="Harmless read query B",
    )

    # Prove distinct conversation IDs
    assert run_a.agent_conversation_id != run_b.agent_conversation_id
    assert run_a.agent_conversation_id is not None
    assert run_b.agent_conversation_id is not None
    assert run_a.status == RunStatus.WAITING_AGENT
    assert run_b.status == RunStatus.WAITING_AGENT

    # Resume exact conversation A using its conversation ID
    resumed_a = supervisor.resume_run(run_a.run_id, "Follow-up read query A")
    assert resumed_a.agent_conversation_id == run_a.agent_conversation_id

    # Verify both runs are indexed in registry
    runs = registry.list_runs(project_id="proof-proj")
    assert len(runs) == 2
