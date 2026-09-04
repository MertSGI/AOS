"""Unit tests for Task DAG (R4)."""

import pytest
from extensions.autonomy_fabric.run_registry import AgentRunRegistry, RunStatus
from extensions.autonomy_fabric.task_dag import TaskDAG, NodeGateType


def test_dag_dependency_and_eligibility():
    registry = AgentRunRegistry()
    dag = TaskDAG("proj-lari", registry)

    node_v2 = dag.add_node("LARI_UI_V2", "UI_DEV", "AUTH-1")
    node_r9 = dag.add_node("LARI_FINAL_R9", "BACKEND_DEV", "AUTH-2")
    node_rev = dag.add_node(
        "FINAL_LARI_REVIEW",
        "REVIEW",
        "AUTH-3",
        dependencies=["LARI_UI_V2", "LARI_FINAL_R9"],
        gate_type=NodeGateType.HUMAN_APPROVAL,
    )

    # Initially, LARI_UI_V2 and LARI_FINAL_R9 are eligible
    eligible = [n.node_id for n in dag.get_eligible_nodes()]
    assert set(eligible) == {"LARI_UI_V2", "LARI_FINAL_R9"}
    assert dag.compute_progress() == 0.0

    # Start and complete UI V2
    run_v2 = registry.create_run("proj-lari", "UI_DEV", "AUTH-1", "ctrl", "antigravity")
    registry.transition(run_v2.run_id, RunStatus.STARTING)
    registry.transition(run_v2.run_id, RunStatus.RUNNING)
    registry.transition(run_v2.run_id, RunStatus.COMPLETED)
    dag.associate_run("LARI_UI_V2", run_v2.run_id)

    # FINAL_LARI_REVIEW is STILL NOT eligible because R9 is not completed
    assert not dag.is_eligible_to_run("FINAL_LARI_REVIEW")
    assert dag.compute_progress() == 33.33

    # Complete R9
    run_r9 = registry.create_run("proj-lari", "BACKEND_DEV", "AUTH-2", "ctrl", "antigravity")
    registry.transition(run_r9.run_id, RunStatus.STARTING)
    registry.transition(run_r9.run_id, RunStatus.RUNNING)
    registry.transition(run_r9.run_id, RunStatus.COMPLETED)
    dag.associate_run("LARI_FINAL_R9", run_r9.run_id)

    # Now FINAL_LARI_REVIEW is eligible!
    assert dag.is_eligible_to_run("FINAL_LARI_REVIEW")
    assert dag.compute_progress() == 66.67
