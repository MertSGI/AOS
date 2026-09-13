"""AOS Nemotron Specialist Model Fabric.

Provides specialist roles, classification-aware security boundaries, deep reasoning execution,
and privacy-preserving output redaction (raw reasoning content never persisted).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class SpecialistRole(str, Enum):
    PLAN_CRITIC = "PLAN_CRITIC"
    ARCHITECTURE_REVIEW = "ARCHITECTURE_REVIEW"
    SECURITY_REVIEW = "SECURITY_REVIEW"
    CODE_REVIEW = "CODE_REVIEW"
    SQL_SCHEMA_REVIEW = "SQL_SCHEMA_REVIEW"
    LONG_CONTEXT_ANALYSIS = "LONG_CONTEXT_ANALYSIS"
    EVIDENCE_CONTRADICTION_REVIEW = "EVIDENCE_CONTRADICTION_REVIEW"
    DESIGN_TEXT_CRITIC = "DESIGN_TEXT_CRITIC"
    SECOND_OPINION = "SECOND_OPINION"


# Strictly permitted data classifications for hosted NVIDIA endpoint
HOSTED_NVIDIA_ALLOWED_DATA_CLASSIFICATIONS = {"PUBLIC", "INTERNAL_NON_SENSITIVE"}

# Explicitly denied classifications
DENIED_DATA_CLASSIFICATIONS = {
    "SECRET",
    "CREDENTIAL",
    "PATIENT_PII",
    "HEALTH_PII",
    "PAYMENT_DATA",
    "SENSITIVE_CUSTOMER_DATA",
    "CONFIDENTIAL",
    "RESTRICTED",
}


@dataclass
class SpecialistRequest:
    role: SpecialistRole
    prompt: str
    data_classification: str = "PUBLIC"
    context: Optional[Dict[str, Any]] = None
    structured_schema: Optional[Dict[str, Any]] = None
    thinking_budget: int = 0
    temperature: float = 0.0


@dataclass
class SpecialistResponse:
    role: SpecialistRole
    status: str  # "SUCCESS", "DENIED", "FAILED", "ADVISORY"
    answer: str
    structured_data: Optional[Dict[str, Any]] = None
    model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    provider: str = "nemotron"
    response_id: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    rejection_reason: Optional[str] = None
    is_advisory_only: bool = True  # Invariant: Nemotron judgments are strictly advisory; verifier remains authoritative


class NemotronSpecialistFabric:
    """Specialist model router and advisor implementing strict security and privacy boundaries."""

    BASE_URL = "https://integrate.api.nvidia.com/v1"
    DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        client_factory: Optional[Callable[[], Any]] = None,
    ):
        self.model = model
        self.client_factory = client_factory

    def evaluate(self, request: SpecialistRequest) -> SpecialistResponse:
        # 1. Data Classification Enforcement (Fail Closed)
        if request.data_classification in DENIED_DATA_CLASSIFICATIONS or \
           request.data_classification not in HOSTED_NVIDIA_ALLOWED_DATA_CLASSIFICATIONS:
            return SpecialistResponse(
                role=request.role,
                status="DENIED",
                answer="",
                rejection_reason=f"Data classification '{request.data_classification}' is prohibited on hosted NVIDIA fabric",
            )

        # 2. Invariant: Nemotron is a text specialist and CANNOT perform pixel visual inspection
        if "screenshot" in request.prompt.lower() or "pixel" in request.prompt.lower():
            if request.role == SpecialistRole.DESIGN_TEXT_CRITIC:
                pass  # Text-based design critique allowed
            else:
                return SpecialistResponse(
                    role=request.role,
                    status="DENIED",
                    answer="",
                    rejection_reason="Nemotron is a text specialist; pixel/visual screenshot inspection is strictly denied",
                )

        # 3. Client Acquisition
        api_key = os.environ.get("NVIDIA_API_KEY")
        if not api_key and self.client_factory is None:
            return SpecialistResponse(
                role=request.role,
                status="FAILED",
                answer="",
                rejection_reason="NVIDIA_API_KEY environment variable is missing",
            )

        if self.client_factory:
            client = self.client_factory()
        else:
            import openai
            client = openai.OpenAI(api_key=api_key, base_url=self.BASE_URL)

        # 4. System prompt tailored to role
        system_instruction = (
            f"You are the AOS Specialist Advisor for {request.role.value}. "
            "Your feedback is strictly advisory and evaluated by the AOS verifier. "
            "Provide rigorous, concise, grounded analysis without hallucinating authority."
        )

        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": request.prompt},
        ]

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": request.temperature,
            "store": False,
        }

        # Extra body for thinking budget / deep reasoning
        if request.thinking_budget > 0:
            kwargs["extra_body"] = {"reasoning": {"effort": "high"}}

        if request.structured_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": f"nemotron_{request.role.value.lower()}",
                    "strict": True,
                    "schema": request.structured_schema,
                },
            }

        # 5. Execution with error mapping
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:
            return SpecialistResponse(
                role=request.role,
                status="FAILED",
                answer="",
                rejection_reason=f"Provider call failed ({e.__class__.__name__}): {e}",
            )

        if not getattr(resp, "choices", None):
            return SpecialistResponse(
                role=request.role,
                status="FAILED",
                answer="",
                rejection_reason="Nemotron returned empty choices",
            )

        choice = resp.choices[0]
        content_str = getattr(choice.message, "content", "") or ""

        # INVARIANT: Raw reasoning / thoughts are stripped and never returned or persisted
        structured_parsed = None
        if request.structured_schema and content_str:
            try:
                structured_parsed = json.loads(content_str)
            except Exception as e:
                return SpecialistResponse(
                    role=request.role,
                    status="FAILED",
                    answer="",
                    rejection_reason=f"Malformed JSON output from structured specialist mode: {e}",
                )

        usage = {}
        if hasattr(resp, "usage") and resp.usage:
            usage = {
                "input_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(resp.usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(resp.usage, "total_tokens", 0) or 0,
            }

        return SpecialistResponse(
            role=request.role,
            status="SUCCESS",
            answer=content_str,
            structured_data=structured_parsed,
            model=self.model,
            response_id=getattr(resp, "id", None),
            usage=usage,
            is_advisory_only=True,
        )
