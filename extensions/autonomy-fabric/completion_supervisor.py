"""AOS Completion Supervisor (R6).

Enforces pre-presentation controller review, bounded revision loops,
and escalation to HUMAN_REVIEW_REQUIRED_WITH_BLOCKERS when max cycles are exceeded.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any
from extensions.autonomy_fabric.run_registry import AgentRunRegistry, RunStatus, RunIdentity
from extensions.autonomy_fabric.authority_router import AuthorityRouter, DecisionContext, DecisionCategory


class ControllerReviewDisposition(str, Enum):
    FINAL_PRESENTATION_READY = "FINAL_PRESENTATION_READY"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    HUMAN_REVIEW_REQUIRED_WITH_BLOCKERS = "HUMAN_REVIEW_REQUIRED_WITH_BLOCKERS"


@dataclass
class ReviewResult:
    disposition: ControllerReviewDisposition
    objective_defects: List[str] = field(default_factory=list)
    revision_cycle_count: int = 0
    max_revision_cycles: int = 3
    final_presentation_ready: bool = False
    human_decision_context: Optional[DecisionContext] = None


class CompletionSupervisor:
    """Pre-presentation controller review engine."""

    def __init__(
        self,
        registry: AgentRunRegistry,
        authority_router: Optional[AuthorityRouter] = None,
        max_autonomous_revision_cycles: int = 3,
    ):
        self.registry = registry
        self.authority_router = authority_router or AuthorityRouter()
        self.max_autonomous_revision_cycles = max_autonomous_revision_cycles
        self.revision_counts: Dict[str, int] = {}  # run_id -> cycle_count

    def review_run(
        self,
        run_id: str,
        objective_defects: Optional[List[str]] = None,
        human_decision_context: Optional[DecisionContext] = None,
    ) -> ReviewResult:
        run = self.registry.get_run(run_id)
        if not run:
            raise KeyError(f"Run {run_id} not found")

        current_cycles = self.revision_counts.get(run_id, 0)
        defects = objective_defects or []

        if defects:
            if current_cycles >= self.max_autonomous_revision_cycles:
                # Max revision cycles exceeded with remaining objective defects -> escalate with blockers
                self.registry.transition(
                    run_id,
                    RunStatus.WAITING_HUMAN,
                    phase="ESCALATED_WITH_BLOCKERS",
                    payload={"blockers": defects, "cycles_exceeded": True},
                )
                return ReviewResult(
                    disposition=ControllerReviewDisposition.HUMAN_REVIEW_REQUIRED_WITH_BLOCKERS,
                    objective_defects=defects,
                    revision_cycle_count=current_cycles,
                    max_revision_cycles=self.max_autonomous_revision_cycles,
                    final_presentation_ready=False,
                    human_decision_context=human_decision_context,
                )
            else:
                # Drive another revision cycle
                self.revision_counts[run_id] = current_cycles + 1
                self.registry.transition(
                    run_id,
                    RunStatus.RUNNING,
                    phase=f"REVISION_CYCLE_{current_cycles + 1}",
                    payload={"defects_to_fix": defects},
                )
                return ReviewResult(
                    disposition=ControllerReviewDisposition.REVISION_REQUIRED,
                    objective_defects=defects,
                    revision_cycle_count=self.revision_counts[run_id],
                    max_revision_cycles=self.max_autonomous_revision_cycles,
                    final_presentation_ready=False,
                )

        # No objective defects
        if human_decision_context:
            self.registry.transition(
                run_id,
                RunStatus.WAITING_HUMAN,
                phase="READY_FOR_HUMAN_DECISION",
                payload={"decision_id": human_decision_context.decision_id},
            )
        else:
            self.registry.transition(run_id, RunStatus.COMPLETED, phase="CONTROLLER_REVIEW_PASSED")

        return ReviewResult(
            disposition=ControllerReviewDisposition.FINAL_PRESENTATION_READY,
            objective_defects=[],
            revision_cycle_count=current_cycles,
            max_revision_cycles=self.max_autonomous_revision_cycles,
            final_presentation_ready=True,
            human_decision_context=human_decision_context,
        )
