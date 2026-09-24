"""AOS Execution Router (V2).

Selects eligible execution backends based on capabilities, authority boundaries,
trust zones, health status, quota, latency, and costs.
Enforces deterministic preferred order:
NATIVE > CI/GITHUB > MODEL+NATIVE > AG SPECIALIST
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple
import logging

from extensions.autonomy_fabric.execution_backend import (
    ExecutionBackend,
    ExecutionRequest,
    ExecutionResult,
    ExecutionCapability,
    ExecutionHealth,
    ExecutionCost,
    EvidenceClass,
    ExecutionAvailabilityState,
)
from extensions.autonomy_fabric.authority_router import AuthorityRouter, DecisionCategory
from extensions.autonomy_fabric.resource_orchestrator import ResourceOrchestrator


logger = logging.getLogger("aos.execution_router")


class ExecutionRouter:
    """Intelligent, quota-aware router for dispatching tasks across backends."""

    def __init__(
        self,
        backends: Optional[List[ExecutionBackend]] = None,
        authority_router: Optional[AuthorityRouter] = None,
        ag_required: bool = False,
        orchestrator: Optional[ResourceOrchestrator] = None,
    ):
        self._backends: Dict[str, ExecutionBackend] = {}
        self.authority_router = authority_router or AuthorityRouter()
        self.ag_required = ag_required
        self.orchestrator = orchestrator or ResourceOrchestrator()

        if backends:
            for b in backends:
                self.register_backend(b)

    def register_backend(self, backend: ExecutionBackend) -> None:
        self._backends[backend.backend_id] = backend

    def get_backend(self, backend_id: str) -> Optional[ExecutionBackend]:
        return self._backends.get(backend_id)

    def list_backends(self) -> List[ExecutionBackend]:
        return list(self._backends.values())

    def select_backend(self, request: ExecutionRequest) -> Optional[ExecutionBackend]:
        """Selects the best eligible backend satisfying capabilities and authority.

        Priority order:
        1. Local Native Workers (FREE_LOCAL)
        2. Remote CI/GitHub (REMOTE_CI)
        3. Model Reasoning / Hybrid (FREE_TIER_CLOUD)
        4. Antigravity Specialist Fallback (QUOTA_LIMITED)
        """
        required_caps = set(request.required_capabilities)

        # Check human authority boundaries
        op_lower = request.operation_class.lower()
        if "production" in op_lower or "destructive" in op_lower or "payment" in op_lower:
            issue_code = "production_go" if "production" in op_lower else "irreversible_destructive_decision"
            decision = self.authority_router.classify_issue(
                issue_code,
                f"Gate for {request.task_id}",
                "Operation requires explicit human authorization",
            )
            if decision in (
                DecisionCategory.PRODUCTION_DECISION,
                DecisionCategory.AUTHORITY_REQUIRED,
                DecisionCategory.HUMAN_PRODUCT_DECISION,
            ):
                return None

        selected_id = self.orchestrator.select(self._backends.values(), request)
        return self._backends.get(selected_id) if selected_id else None

    def execute_with_failover(self, request: ExecutionRequest) -> ExecutionResult:
        """Dispatches request to optimal backend, automatically falling over if quota/degraded."""
        attempts = 0
        tried_backend_ids: Set[str] = set()
        last_degraded: Optional[ExecutionResult] = None

        while attempts < 3:
            attempts += 1
            # Temporarily filter out already tried backends
            available_backends = [
                b for b in self._backends.values() if b.backend_id not in tried_backend_ids
            ]
            temp_router = ExecutionRouter(
                backends=available_backends,
                authority_router=self.authority_router,
                ag_required=self.ag_required,
                orchestrator=self.orchestrator,
            )
            selected = temp_router.select_backend(request)

            if not selected:
                break

            tried_backend_ids.add(selected.backend_id)
            result = selected.execute(request)

            # If result is degraded or quota exhausted, fail over to next eligible backend
            if result.status == "DEGRADED":
                last_degraded = result
                continue

            return result

        # Preserve a structured non-terminal availability result when all
        # compatible resources degraded. Project state must not become failure.
        if last_degraded is not None:
            return last_degraded

        # No backend succeeded or all eligible backends were ineligible.
        return ExecutionResult(
            backend_id="router",
            worker_id="router",
            task_id=request.task_id,
            request_id=request.request_id,
            status="DENIED" if not self._backends else "FAILED",
            exit_code=1,
            workspace=request.workspace,
            sanitized_errors=[f"No healthy eligible execution backend available for capabilities: {[c.value for c in request.required_capabilities]}"],
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
        )
