"""AOS Task DAG / Project Work Graph (R4).

Implements dependency graph resolution, node execution gating,
and dynamic project completion percentage calculation.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Any
from extensions.autonomy_fabric.run_registry import RunStatus, AgentRunRegistry, RunIdentity


class NodeGateType(str, Enum):
    NONE = "NONE"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"
    AUTHORITY_APPROVAL = "AUTHORITY_APPROVAL"


@dataclass
class DAGNode:
    node_id: str
    project_id: str
    run_type: str
    authority_id: str
    dependencies: List[str] = field(default_factory=list)  # list of node_ids
    gate_type: NodeGateType = NodeGateType.NONE
    associated_run_id: Optional[str] = None
    gate_passed: bool = False


class TaskDAG:
    """Dependency-aware Task DAG orchestration."""

    def __init__(self, project_id: str, registry: AgentRunRegistry):
        self.project_id = project_id
        self.registry = registry
        self.nodes: Dict[str, DAGNode] = {}

    def add_node(
        self,
        node_id: str,
        run_type: str,
        authority_id: str,
        dependencies: Optional[List[str]] = None,
        gate_type: NodeGateType = NodeGateType.NONE,
    ) -> DAGNode:
        if node_id in self.nodes:
            raise ValueError(f"Node {node_id} already exists in DAG {self.project_id}")

        deps = dependencies or []
        for dep in deps:
            if dep not in self.nodes:
                raise ValueError(f"Dependency node {dep} does not exist in DAG")

        node = DAGNode(
            node_id=node_id,
            project_id=self.project_id,
            run_type=run_type,
            authority_id=authority_id,
            dependencies=deps,
            gate_type=gate_type,
        )
        self.nodes[node_id] = node
        return node

    def associate_run(self, node_id: str, run_id: str):
        if node_id not in self.nodes:
            raise KeyError(f"Node {node_id} not found")
        self.nodes[node_id].associated_run_id = run_id

    def pass_gate(self, node_id: str):
        if node_id not in self.nodes:
            raise KeyError(f"Node {node_id} not found")
        self.nodes[node_id].gate_passed = True

    def is_eligible_to_run(self, node_id: str) -> bool:
        if node_id not in self.nodes:
            return False

        node = self.nodes[node_id]

        # Check if already started or completed
        if node.associated_run_id:
            run = self.registry.get_run(node.associated_run_id)
            if run and run.status in (RunStatus.RUNNING, RunStatus.STARTING, RunStatus.COMPLETED):
                return False

        # Check dependencies
        for dep_id in node.dependencies:
            dep_node = self.nodes[dep_id]
            if not dep_node.associated_run_id:
                return False
            dep_run = self.registry.get_run(dep_node.associated_run_id)
            if not dep_run or dep_run.status != RunStatus.COMPLETED:
                return False
            # Check dependency gate if required
            if dep_node.gate_type != NodeGateType.NONE and not dep_node.gate_passed:
                return False

        return True

    def get_eligible_nodes(self) -> List[DAGNode]:
        return [node for node in self.nodes.values() if self.is_eligible_to_run(node.node_id)]

    def compute_progress(self) -> float:
        """Computes dynamic project progress percentage (0.0 to 100.0) from actual run state."""
        if not self.nodes:
            return 100.0

        total_nodes = len(self.nodes)
        completed_nodes = 0

        for node in self.nodes.values():
            if node.associated_run_id:
                run = self.registry.get_run(node.associated_run_id)
                if run and run.status == RunStatus.COMPLETED:
                    if node.gate_type == NodeGateType.NONE or node.gate_passed:
                        completed_nodes += 1

        return round((completed_nodes / total_nodes) * 100.0, 2)
