"""AOS Execution Router (V2).

Selects eligible execution backends based on capabilities, authority boundaries,
trust zones, health status, quota, latency, and costs.
Enforces deterministic preferred order:
NATIVE > CI/GITHUB > MODEL+NATIVE > AG SPECIALIST
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from dataclasses import replace
import logging

from extensions.autonomy_fabric.execution_backend import (
    ExecutionBackend,
    ExecutionRequest,
    ExecutionResult,
    ExecutionCapability,
    ExecutionHealth,
    ExecutionCost,
    EvidenceClass,
    ExecutionAvailabilitySnapshot,
    ExecutionAvailabilityState,
    ExecutionFailureDisposition,
    BackendClass,
    classify_execution_failure,
    execution_failure_class,
)
from extensions.autonomy_fabric.authority_router import AuthorityRouter, DecisionCategory
from extensions.autonomy_fabric.resource_orchestrator import ResourceOrchestrator


logger = logging.getLogger("aos.execution_router")


def summarize_backend_attempts(
    records: Iterable[Dict[str, Any]],
) -> Dict[str, Dict[str, int]]:
    """Aggregate sanitized attempt facts without collapsing failover history."""
    summary: Dict[str, Dict[str, int]] = {}
    for record in records:
        backend_id = str(record.get("backend_id") or "UNKNOWN")
        metrics = summary.setdefault(backend_id, {
            "attempt_count": 0,
            "success_count": 0,
            "degraded_count": 0,
            "failure_count": 0,
            "final_selection_count": 0,
            "direct_agentic_attempt_count": 0,
            "planning_bridge_attempt_count": 0,
        })
        metrics["attempt_count"] += 1
        status = str(record.get("status") or "UNKNOWN").upper()
        if status == "SUCCESS":
            metrics["success_count"] += 1
        elif status == "DEGRADED":
            metrics["degraded_count"] += 1
        else:
            metrics["failure_count"] += 1
        if bool(record.get("final_selection")):
            metrics["final_selection_count"] += 1
        mode = str(record.get("invocation_mode") or "")
        if mode == "DIRECT_AGENTIC_INVOCATION":
            metrics["direct_agentic_attempt_count"] += 1
        elif mode == "PLANNING_BRIDGE_INVOCATION":
            metrics["planning_bridge_attempt_count"] += 1
    return summary


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
        self.last_attempt_telemetry: List[Dict[str, Any]] = []

        if backends:
            for b in backends:
                self.register_backend(b)

    def register_backend(self, backend: ExecutionBackend) -> None:
        self._backends[backend.backend_id] = backend

    def get_backend(self, backend_id: str) -> Optional[ExecutionBackend]:
        return self._backends.get(backend_id)

    def list_backends(self) -> List[ExecutionBackend]:
        return list(self._backends.values())

    def close(self) -> None:
        """Release only resources explicitly owned by registered backends."""
        for backend in self._backends.values():
            close = getattr(backend, "close", None)
            if callable(close):
                close()

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
        """Try each finite eligible route once, preserving failure semantics."""
        attempts = 0
        tried_backend_ids: Set[str] = set()
        failed_results: List[Tuple[ExecutionResult, ExecutionFailureDisposition]] = []
        self.last_attempt_telemetry = []

        while len(tried_backend_ids) < len(self._backends):
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

            attempts += 1
            tried_backend_ids.add(selected.backend_id)
            dispatch_request = request
            if (
                request.agentic_identity is not None
                and request.agentic_identity.backend_id != selected.backend_id
            ):
                prior = request.agentic_identity
                pack = dict(request.context_pack or {})
                pack.update({
                    "completed_work_unit_ids": list(prior.completed_work_unit_ids),
                    "completed_work_unit_signatures": dict(prior.completed_work_unit_signatures),
                    "artifact_hashes": dict(prior.artifact_hashes),
                    "superseded_session_ids": list(dict.fromkeys([
                        *prior.superseded_session_ids,
                        *([prior.session_or_thread_id] if prior.session_or_thread_id else []),
                    ])),
                    "handoff_from_backend": prior.backend_id,
                })
                dispatch_request = replace(request, agentic_identity=None, context_pack=pack)
            result = selected.execute(dispatch_request)
            invocation_mode = (
                "PLANNING_BRIDGE_INVOCATION"
                if hasattr(selected, "underlying_backend")
                else (
                    "DIRECT_AGENTIC_INVOCATION"
                    if getattr(selected, "backend_class", None)
                    == BackendClass.AGENTIC_EXECUTION_BACKEND
                    else "BACKEND_INVOCATION"
                )
            )
            call_records: List[Dict[str, Any]] = [{
                "attempt_id": f"{request.request_id}:{attempts}:{selected.backend_id}",
                "request_id": request.request_id,
                "task_id": request.task_id,
                "backend_id": selected.backend_id,
                "status": str(result.status),
                "invocation_mode": invocation_mode,
                "final_selection": False,
            }]
            consume = getattr(selected, "consume_attempt_telemetry", None)
            if callable(consume):
                for index, underlying in enumerate(consume(), start=1):
                    if not isinstance(underlying, dict):
                        continue
                    call_records.append({
                        "attempt_id": (
                            f"{request.request_id}:{attempts}:{selected.backend_id}:"
                            f"underlying:{index}"
                        ),
                        "request_id": request.request_id,
                        "task_id": request.task_id,
                        "backend_id": str(underlying.get("backend_id") or "UNKNOWN"),
                        "status": str(underlying.get("status") or "UNKNOWN"),
                        "invocation_mode": str(
                            underlying.get("invocation_mode")
                            or "PLANNING_BRIDGE_INVOCATION"
                        ),
                        "bridge_backend_id": selected.backend_id,
                        "final_selection": False,
                    })

            if result.status != "SUCCESS":
                disposition = classify_execution_failure(result)
                for record in call_records:
                    record["failure_disposition"] = disposition.value
                    record["failure_class"] = execution_failure_class(result)
                if disposition in {
                    ExecutionFailureDisposition.RESOURCE_UNAVAILABLE,
                    ExecutionFailureDisposition.BACKEND_LOCAL,
                }:
                    # Both classes are safe to route around.  Only a route made
                    # entirely of resource failures may become provider wait.
                    failed_results.append((result, disposition))
                    self.last_attempt_telemetry.extend(call_records)
                    continue

                # Authority/security/global request-contract failures stop the
                # route immediately and remain the final selection.
                for record in call_records:
                    record["final_selection"] = True
                self.last_attempt_telemetry.extend(call_records)
                return result

            for record in call_records:
                record["final_selection"] = True
            self.last_attempt_telemetry.extend(call_records)
            return result

        if failed_results:
            if all(
                disposition == ExecutionFailureDisposition.RESOURCE_UNAVAILABLE
                for _result, disposition in failed_results
            ):
                remaining_backends = [
                    backend for backend in self._backends.values()
                    if backend.backend_id not in tried_backend_ids
                ]
                remaining_exhaustion = "NO_ELIGIBLE_BACKEND"
                classify_exhaustion = getattr(self.orchestrator, "classify_exhaustion", None)
                if remaining_backends and callable(classify_exhaustion):
                    remaining_exhaustion = str(
                        classify_exhaustion(remaining_backends, request)
                    )
                if remaining_exhaustion == "BACKEND_LOCAL":
                    return ExecutionResult(
                        backend_id="router",
                        worker_id="router",
                        task_id=request.task_id,
                        request_id=request.request_id,
                        status="DEGRADED",
                        exit_code=1,
                        workspace=request.workspace,
                        sanitized_errors=["BACKEND_LOCAL_CONTRACT_ROUTE_UNAVAILABLE"],
                        evidence_payload={
                            "failure_class": "BACKEND_LOCAL_CONTRACT_ROUTE_UNAVAILABLE",
                            "attempted_backend_ids": sorted(tried_backend_ids),
                            "failure_disposition": ExecutionFailureDisposition.BACKEND_LOCAL.value,
                        },
                        evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
                        availability=ExecutionAvailabilitySnapshot(
                            state=ExecutionAvailabilityState.CONTRACT_FAILURE,
                            observed_at="",
                            source="ROUTER_ELIGIBILITY_EXHAUSTION",
                        ),
                    )
                # Exhaustion is global only when every attempted compatible
                # backend failed for an actual availability reason.
                base = failed_results[-1][0]
                evidence_payload = dict(base.evidence_payload or {})
                evidence_payload.update({
                    "failure_class": "ALL_ELIGIBLE_REASONING_RESOURCES_UNAVAILABLE",
                    "attempted_backend_ids": sorted(tried_backend_ids),
                    "backend_failure_classes": [
                        execution_failure_class(item) for item, _ in failed_results
                    ],
                    "failure_disposition": ExecutionFailureDisposition.RESOURCE_UNAVAILABLE.value,
                })
                return ExecutionResult(
                    backend_id=base.backend_id,
                    worker_id=base.worker_id,
                    task_id=base.task_id,
                    request_id=base.request_id,
                    status="WAITING_FOR_REASONING_PROVIDER",
                    exit_code=base.exit_code,
                    workspace=base.workspace,
                    sanitized_errors=["ALL_ELIGIBLE_REASONING_RESOURCES_UNAVAILABLE"],
                    resource_usage=dict(base.resource_usage or {}),
                    evidence_class=base.evidence_class,
                    evidence_payload=evidence_payload,
                    availability=base.availability,
                )

            # A deterministic backend-local failure disproves a global resource
            # outage.  Preserve its exact class for diagnosis and bounded retry.
            return next(
                result for result, disposition in reversed(failed_results)
                if disposition == ExecutionFailureDisposition.BACKEND_LOCAL
            )

        exhaustion = "NO_ELIGIBLE_BACKEND"
        classify_exhaustion = getattr(self.orchestrator, "classify_exhaustion", None)
        if callable(classify_exhaustion):
            exhaustion = str(classify_exhaustion(self._backends.values(), request))
        if exhaustion == "RESOURCE_UNAVAILABLE":
            return ExecutionResult(
                backend_id="router",
                worker_id="router",
                task_id=request.task_id,
                request_id=request.request_id,
                status="WAITING_FOR_REASONING_PROVIDER",
                exit_code=1,
                workspace=request.workspace,
                sanitized_errors=["ALL_ELIGIBLE_REASONING_RESOURCES_UNAVAILABLE"],
                evidence_payload={
                    "failure_class": "ALL_ELIGIBLE_REASONING_RESOURCES_UNAVAILABLE",
                    "failure_disposition": ExecutionFailureDisposition.RESOURCE_UNAVAILABLE.value,
                },
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
                availability=ExecutionAvailabilitySnapshot(
                    state=ExecutionAvailabilityState.TEMPORARILY_UNAVAILABLE,
                    observed_at="",
                    source="ROUTER_ELIGIBILITY_EXHAUSTION",
                ),
            )

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
            evidence_payload={
                "failure_class": exhaustion,
                "failure_disposition": ExecutionFailureDisposition.FAIL_CLOSED.value,
            },
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
        )
