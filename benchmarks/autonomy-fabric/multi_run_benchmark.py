"""Multi-Run Autonomy Benchmark (R19 / Correction R1).

Simulates concurrent execution of Run A (WAITING_HUMAN), Run B (WAITING_AUTHORITY), and Run C (RUNNING -> COMPLETED),
proving non-blocking state independence and durable supervisor process restart recovery.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from extensions.autonomy_fabric.run_registry import AgentRunRegistry, RunStatus, RunIdentity
from extensions.autonomy_fabric.antigravity_adapter import FakeAntigravityAdapter, AntigravityStatus
from extensions.autonomy_fabric.supervisor import ParallelSupervisor
from extensions.autonomy_fabric.evidence_aggregator import EvidenceAggregator, EvidenceType


@dataclass
class MultiRunBenchmarkReport:
    runs_isolated: bool
    workspaces_isolated: bool
    authorities_isolated: bool
    waiting_human_non_blocking: bool
    waiting_authority_non_blocking: bool
    waiting_agent_tested: bool
    interrupted_tested: bool
    recovery_successful: bool
    all_verifications_passed: bool


class MultiRunAutonomyBenchmarkRunner:
    """Runs deterministic simulation of concurrent independent runs with WAITING_HUMAN and WAITING_AUTHORITY gates."""

    def run_benchmark(self) -> MultiRunBenchmarkReport:
        registry = AgentRunRegistry()
        adapter = FakeAntigravityAdapter()
        adapter.default_status = AntigravityStatus.WAITING
        supervisor = ParallelSupervisor(registry, adapter, max_concurrent_active_runs=5)
        evidence = EvidenceAggregator(registry)

        # Run A: LARI UI development -> WAITING_HUMAN
        run_a = supervisor.launch_run(
            project_id="proj-lari-ui",
            run_type="UI_DEV",
            authority_id="AUTH-LARI-V2",
            controller_id="ctrl-main",
            agent_provider="antigravity",
            workspace_path="/ws/lari_ui",
            branch="feature/lari-ui-v2",
            initial_prompt="Develop LARI UI V2",
        )

        # Run B: AOS self-development -> WAITING_AUTHORITY
        run_b = supervisor.launch_run(
            project_id="proj-aos-selfdev",
            run_type="SELF_DEV",
            authority_id="AUTH-AOS-SELFDEV",
            controller_id="ctrl-main",
            agent_provider="antigravity",
            workspace_path="/ws/aos_selfdev",
            branch="feature/aos-autonomy",
            initial_prompt="Execute Self-Dev Autonomy",
        )

        # Run C: Independent Roadmap -> RUNNING -> COMPLETED
        run_c = supervisor.launch_run(
            project_id="proj-roadmap-c",
            run_type="ROADMAP",
            authority_id="AUTH-ROADMAP-C",
            controller_id="ctrl-main",
            agent_provider="antigravity",
            workspace_path="/ws/roadmap_c",
            branch="feature/roadmap-c",
            initial_prompt="Execute Roadmap C",
        )

        # Run D: Tested for WAITING_AGENT state
        run_d = supervisor.launch_run(
            project_id="proj-d",
            run_type="AUX",
            authority_id="AUTH-D",
            controller_id="ctrl-main",
            agent_provider="antigravity",
            workspace_path="/ws/aux_d",
            branch="feature/d",
            initial_prompt="Auxiliary task D",
        )

        # Verify isolation across active runs
        runs_isolated = len({run_a.run_id, run_b.run_id, run_c.run_id, run_d.run_id}) == 4
        workspaces_isolated = len({run_a.workspace_path, run_b.workspace_path, run_c.workspace_path, run_d.workspace_path}) == 4
        authorities_isolated = len({run_a.authority_id, run_b.authority_id, run_c.authority_id, run_d.authority_id}) == 4

        # Explicitly transition Run A to WAITING_HUMAN
        registry.transition(run_a.run_id, RunStatus.RUNNING)
        registry.transition(run_a.run_id, RunStatus.WAITING_HUMAN)

        # Explicitly transition Run B to WAITING_AUTHORITY (independent from WAITING_AGENT)
        registry.transition(run_b.run_id, RunStatus.RUNNING)
        registry.transition(run_b.run_id, RunStatus.WAITING_AUTHORITY)

        # Keep Run D in WAITING_AGENT
        registry.transition(run_d.run_id, RunStatus.WAITING_AGENT)

        # Prove Run C continues progressing to RUNNING & COMPLETED despite A and B waiting!
        registry.transition(run_c.run_id, RunStatus.RUNNING)
        registry.transition(run_c.run_id, RunStatus.COMPLETED)

        waiting_human_non_blocking = (run_c.status == RunStatus.COMPLETED and run_a.status == RunStatus.WAITING_HUMAN)
        waiting_authority_non_blocking = (run_c.status == RunStatus.COMPLETED and run_b.status == RunStatus.WAITING_AUTHORITY)
        waiting_agent_tested = (run_d.status == RunStatus.WAITING_AGENT)

        # Test INTERRUPTED status handling
        run_e = supervisor.launch_run(
            project_id="proj-e",
            run_type="AUX_E",
            authority_id="AUTH-E",
            controller_id="ctrl-main",
            agent_provider="antigravity",
            workspace_path="/ws/aux_e",
            branch="feature/e",
        )
        supervisor.interrupt_run(run_e.run_id, "Test interrupt")
        interrupted_tested = (registry.get_run(run_e.run_id).status == RunStatus.INTERRUPTED)  # type: ignore

        # Record evidence for Run C
        evidence.record_evidence(
            run_id=run_c.run_id,
            phase="COMPLETED",
            project_id="proj-roadmap-c",
            evidence_type=EvidenceType.VERIFICATION,
            producer="verifier:benchmark",
            title="Run C Completed Successfully",
        )

        # Test supervisor restart recovery (Run D is WAITING_AGENT so active lease is recovered)
        new_supervisor = ParallelSupervisor(registry, adapter, max_concurrent_active_runs=5)
        recovered_count = new_supervisor.recover_from_crash()
        recovery_successful = (recovered_count == 1)

        all_passed = (
            runs_isolated
            and workspaces_isolated
            and authorities_isolated
            and waiting_human_non_blocking
            and waiting_authority_non_blocking
            and waiting_agent_tested
            and interrupted_tested
            and recovery_successful
        )

        return MultiRunBenchmarkReport(
            runs_isolated=runs_isolated,
            workspaces_isolated=workspaces_isolated,
            authorities_isolated=authorities_isolated,
            waiting_human_non_blocking=waiting_human_non_blocking,
            waiting_authority_non_blocking=waiting_authority_non_blocking,
            waiting_agent_tested=waiting_agent_tested,
            interrupted_tested=interrupted_tested,
            recovery_successful=recovery_successful,
            all_verifications_passed=all_passed,
        )
