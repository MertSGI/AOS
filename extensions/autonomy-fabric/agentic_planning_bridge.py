"""Bounded Structured Planning Bridge for Eligible Agentic Backends.

Enables eligible agentic execution backends (e.g. Antigravity, Cline, Codex) to
service structured planning requests (MODEL_REASONING) without granting write
authority or faking raw capability sets on the underlying agentic harnesses.

INVARIANTS:
1. Strict schema validation against Draft202012Validator. Fails closed on any schema violation.
2. Zero write authority: write_scope is strictly empty (). Any workspace mutation attempt fails closed.
3. Preserves backend_id, session identity, and scarcity/cost tiers of the underlying resource.
4. Transient structured proposal contract: proposal is returned in transient_structured_output /
   evidence_payload['proposal'], preserving privacy and provenance.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Set

from jsonschema import Draft202012Validator

from extensions.autonomy_fabric.execution_backend import (
    AgenticExecutionBackend,
    BackendClass,
    EvidenceClass,
    ExecutionAvailabilitySnapshot,
    ExecutionAvailabilityState,
    ExecutionBackend,
    ExecutionCapability,
    ExecutionCost,
    ExecutionHealth,
    ExecutionRequest,
    ExecutionResult,
    ExecutionTrustZone,
)


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Extract the first valid top-level JSON object from text output."""
    text = text.strip()
    if not text:
        return None
    # 1. Direct parse attempt
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    # 2. Markdown fenced json block
    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced_match:
        try:
            data = json.loads(fenced_match.group(1))
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    # 3. Outer brace search
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    return None


class AgenticStructuredPlanningBridge(ExecutionBackend):
    """Adapts an AgenticExecutionBackend into an ExecutionBackend for MODEL_REASONING."""

    backend_class = BackendClass.REASONING_BACKEND
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    supported_capabilities: Set[ExecutionCapability] = {ExecutionCapability.MODEL_REASONING}

    def __init__(self, underlying_backend: AgenticExecutionBackend, *, bridge_id: Optional[str] = None):
        self.underlying_backend = underlying_backend
        self.backend_id = bridge_id or f"{underlying_backend.backend_id}_planning_bridge"

    @property
    def cost(self) -> ExecutionCost:
        return self.underlying_backend.cost

    def get_health(self) -> ExecutionHealth:
        return self.underlying_backend.get_health()

    def get_availability(self) -> ExecutionAvailabilitySnapshot:
        if hasattr(self.underlying_backend, "get_availability"):
            return self.underlying_backend.get_availability()
        health = self.get_health()
        state = ExecutionAvailabilityState.AVAILABLE if health == ExecutionHealth.HEALTHY else ExecutionAvailabilityState.CONTRACT_FAILURE
        return ExecutionAvailabilitySnapshot(
            state=state,
            observed_at="",
            source=f"{self.backend_id}_health",
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        # Enforce read-only planning: write_scope must be strictly empty
        if request.write_scope:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="agentic_planning_bridge",
                task_id=request.task_id,
                request_id=request.request_id,
                status="DENIED",
                exit_code=1,
                workspace=request.workspace,
                sanitized_errors=["PLANNING_WRITE_AUTHORITY_DENIED"],
                evidence_payload={
                    "failure_class": "PLANNING_WRITE_AUTHORITY_DENIED",
                    "reason": "Planning bridge strictly prohibits write_scope.",
                },
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        prompt = str(request.payload.get("prompt", ""))
        schema = request.payload.get("schema")
        if not isinstance(schema, dict) or not schema:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="agentic_planning_bridge",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=request.workspace,
                sanitized_errors=["INVALID_PLANNING_SCHEMA"],
                evidence_payload={"failure_class": "INVALID_PLANNING_SCHEMA"},
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        # Build bounded planning prompt instructing agentic harness to output JSON only
        schema_json = json.dumps(schema, indent=2, sort_keys=True)
        structured_prompt = (
            f"{prompt}\n\n"
            "================================================================================\n"
            "CRITICAL STRUCTURED OUTPUT REQUIREMENT:\n"
            "You are acting in READ-ONLY PLANNING MODE. Do NOT modify any files on disk.\n"
            "Do NOT run any mutating commands or tests.\n"
            "You MUST output ONLY a valid JSON object matching the JSON schema below.\n"
            "No preamble, no explanation, no markdown text outside the JSON object.\n"
            "EXACT JSON SCHEMA:\n"
            f"{schema_json}\n"
            "================================================================================\n"
        )

        agentic_req = ExecutionRequest(
            task_id=request.task_id,
            project_id=request.project_id,
            workspace=request.workspace,
            operation_class="AGENTIC",
            required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
            authority_id=request.authority_id,
            read_scope=request.read_scope,
            write_scope=[],  # Strict no-write contract
            allowed_commands=[],
            network_policy=request.network_policy,
            data_classification=request.data_classification,
            credential_refs=request.credential_refs,
            timeout_seconds=request.timeout_seconds,
            expected_artifacts=[],
            expected_changed_paths=[],
            payload={
                "prompt": structured_prompt,
                "planning_mode": True,
                "schema": schema,
                "source_sha": request.payload.get("source_sha", "0" * 40),
            },
            agentic_identity=request.agentic_identity,
            context_pack=request.context_pack,
        )

        agentic_result = self.underlying_backend.execute(agentic_req)

        # If underlying execution did not succeed, fail closed
        if agentic_result.status != "SUCCESS":
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="agentic_planning_bridge",
                task_id=request.task_id,
                request_id=request.request_id,
                status=agentic_result.status,
                exit_code=agentic_result.exit_code,
                workspace=request.workspace,
                sanitized_errors=agentic_result.sanitized_errors or ["AGENTIC_BACKEND_EXECUTION_FAILED"],
                evidence_payload={
                    "failure_class": agentic_result.evidence_payload.get("failure_class", "AGENTIC_EXECUTION_FAILED"),
                    "underlying_backend": self.underlying_backend.backend_id,
                    "underlying_status": agentic_result.status,
                },
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
                availability=agentic_result.availability,
            )

        # Enforce zero mutating changes
        if agentic_result.changed_paths:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="agentic_planning_bridge",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=request.workspace,
                sanitized_errors=["PLANNING_MUTATION_DETECTED"],
                evidence_payload={
                    "failure_class": "PLANNING_MUTATION_DETECTED",
                    "changed_paths": agentic_result.changed_paths,
                },
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        # Extract structured proposal from output
        output_text = agentic_result.stdout_digest or ""
        if hasattr(agentic_result, "evidence_payload"):
            # If underlying backend provided raw output in evidence payload or stdout
            raw = agentic_result.evidence_payload.get("raw_output") or agentic_result.evidence_payload.get("proposal")
            if isinstance(raw, dict):
                proposal = raw
            else:
                proposal = extract_json_object(str(raw or output_text))
        else:
            proposal = extract_json_object(output_text)

        if not proposal:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="agentic_planning_bridge",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=request.workspace,
                sanitized_errors=["MALFORMED_STRUCTURED_OUTPUT"],
                evidence_payload={
                    "failure_class": "MALFORMED_STRUCTURED_OUTPUT",
                    "reason": "Failed to extract valid JSON proposal matching schema.",
                },
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        # Validate against Draft202012 schema
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(proposal))
        if errors:
            err_msgs = [e.message for e in errors[:5]]
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="agentic_planning_bridge",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=request.workspace,
                sanitized_errors=["SCHEMA_VALIDATION_FAILED"],
                evidence_payload={
                    "failure_class": "SCHEMA_VALIDATION_FAILED",
                    "validation_errors": err_msgs,
                },
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        # Return success with transient structured proposal
        res = ExecutionResult(
            backend_id=self.backend_id,
            worker_id="agentic_planning_bridge",
            task_id=request.task_id,
            request_id=request.request_id,
            status="SUCCESS",
            exit_code=0,
            workspace=request.workspace,
            stdout_digest=f"Structured planning succeeded via bridge to {self.underlying_backend.backend_id}",
            evidence_payload={
                "proposal": proposal,
                "bridged_backend_id": self.underlying_backend.backend_id,
                "cost_class": self.cost.value,
            },
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            agentic_identity=agentic_result.agentic_identity,
            availability=agentic_result.availability,
        )
        setattr(res, "transient_structured_output", proposal)
        return res
