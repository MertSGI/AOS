"""Planner provider interface and OpenAI Responses API implementation."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Protocol, Tuple

class PlannerError(Exception):
    """Base internal planner exception."""
    pass

class PlannerTransientError(PlannerError):
    """Transient network/API error eligible for retry."""
    pass

class PlannerCredentialError(PlannerError):
    """Missing or invalid credential error."""
    pass

class PlannerContractError(PlannerError):
    """Schema, model refusal, incomplete response, or semantic contract failure."""
    pass

class PlannerProvider(Protocol):
    def generate_plan(self, prompt: str, schema: Dict[str, Any]) -> Tuple[Dict[str, Any], str | None, Dict[str, Any] | None]:
        ...

class OpenAIPlannerProvider:
    def __init__(self, model: str = "gpt-5.6-sol"):
        self.model = model

    def generate_plan(self, prompt: str, schema: Dict[str, Any]) -> Tuple[Dict[str, Any], str | None, Dict[str, Any] | None]:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise PlannerCredentialError("OPENAI_API_KEY environment variable is missing")

        import openai
        client = openai.OpenAI(api_key=api_key)

        if not hasattr(client, "responses") or not callable(getattr(client.responses, "create", None)):
            raise PlannerContractError("OpenAI Responses API (client.responses.create) is unavailable in current SDK")

        instructions = (
            "You are the AOS Shadow Planner. Your task is to evaluate canonical project control "
            "context and output a bounded planner decision JSON matching the provided schema. "
            "You MUST select the canonical milestone and canonical next_action EXACTLY as provided "
            "in the bounded input. In shadow mode, mutation_intent MUST be 'NONE' and risk_class MUST be 'R0'."
        )

        try:
            response = client.responses.create(
                model=self.model,
                instructions=instructions,
                input=prompt,
                store=False,
                reasoning={"effort": "medium"},
                max_output_tokens=1000,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "planner_decision",
                        "strict": True,
                        "schema": schema
                    }
                }
            )
        except Exception as e:
            err_name = e.__class__.__name__
            if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError)):
                raise PlannerTransientError(f"OpenAI transient error ({err_name}): {e}") from e
            elif isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
                raise PlannerCredentialError(f"OpenAI auth/permission failure ({err_name}): {e}") from e
            elif isinstance(e, openai.BadRequestError):
                raise PlannerContractError(f"OpenAI invalid request/schema ({err_name}): {e}") from e
            else:
                raise PlannerContractError(f"OpenAI provider contract failure ({err_name}): {e}") from e

        # Inspect Responses API completion status
        status = getattr(response, "status", None)
        if status and status not in ("completed", "complete"):
            inc_details = getattr(response, "incomplete_details", None)
            raise PlannerContractError(f"OpenAI response status '{status}' incomplete: {inc_details}")

        # Extract output text content and check refusal
        content_str = None
        if hasattr(response, "output_text") and response.output_text:
            content_str = response.output_text
        elif hasattr(response, "output") and response.output:
            for item in response.output:
                if getattr(item, "type", None) == "message" and hasattr(item, "content"):
                    for part in item.content:
                        if getattr(part, "type", None) == "refusal":
                            raise PlannerContractError(f"OpenAI model refused response: {getattr(part, 'refusal', '')}")
                        if getattr(part, "type", None) == "text":
                            content_str = getattr(part, "text", None)
                            if content_str:
                                break

        if not content_str:
            raise PlannerContractError("OpenAI Responses API returned empty content")

        try:
            parsed_decision = json.loads(content_str)
        except Exception as e:
            raise PlannerContractError(f"OpenAI model output is not valid JSON: {e}") from e

        response_id = getattr(response, "id", None)

        usage_data = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            cached_tokens = 0
            if hasattr(u, "input_tokens_details") and u.input_tokens_details:
                cached_tokens = getattr(u.input_tokens_details, "cached_tokens", 0)
            elif hasattr(u, "prompt_tokens_details") and u.prompt_tokens_details:
                cached_tokens = getattr(u.prompt_tokens_details, "cached_tokens", 0)

            usage_data = {
                "input_tokens": getattr(u, "input_tokens", getattr(u, "prompt_tokens", 0)),
                "cached_input_tokens": cached_tokens,
                "output_tokens": getattr(u, "output_tokens", getattr(u, "completion_tokens", 0)),
                "total_tokens": getattr(u, "total_tokens", 0)
            }

        return parsed_decision, response_id, usage_data

class FakePlannerProvider:
    def __init__(
        self,
        decision_override: Dict[str, Any] | None = None,
        transient_failures_count: int = 0,
        exception_to_raise: Exception | None = None
    ):
        self.decision_override = decision_override
        self.transient_failures_count = transient_failures_count
        self.exception_to_raise = exception_to_raise
        self.attempts = 0

    def generate_plan(self, prompt: str, schema: Dict[str, Any]) -> Tuple[Dict[str, Any], str | None, Dict[str, Any] | None]:
        self.attempts += 1
        if self.exception_to_raise:
            raise self.exception_to_raise

        if self.attempts <= self.transient_failures_count:
            raise PlannerTransientError(f"Transient network error on attempt {self.attempts}")

        if self.decision_override:
            return self.decision_override, "fake-response-id-001", {
                "input_tokens": 150,
                "cached_input_tokens": 0,
                "output_tokens": 45,
                "total_tokens": 195
            }

        source_sha = "4c55eecdbe064c74b34af31a1daf9851689e4fe8"
        milestone = "LARİ Clinic"
        next_act = "Controller-authorized LARİ Clinic foundation materialization and read-only scope/contract gap audit from frozen Package baseline 65a53427f52c21e60aa8f92e02a17d693a201601."
        target_base = "65a53427f52c21e60aa8f92e02a17d693a201601"

        for line in prompt.splitlines():
            if line.startswith("RESOLVED SOURCE SHA:"):
                source_sha = line.split(":", 1)[1].strip()
            elif line.startswith("CANONICAL MILESTONE:"):
                milestone = line.split(":", 1)[1].strip()
            elif line.startswith("CANONICAL NEXT ACTION:"):
                next_act = line.split(":", 1)[1].strip()
            elif line.startswith("TARGET BASE SHA:"):
                raw_tb = line.split(":", 1)[1].strip()
                target_base = None if raw_tb == "NONE" else raw_tb

        decision = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": source_sha,
            "selected_milestone": milestone,
            "selected_next_action": next_act,
            "target_base_sha": target_base,
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Shadow read of canonical state matches active workstream and next action.",
            "disposition": "SHADOW_ACCEPT"
        }
        return decision, "fake-response-id-001", {
            "input_tokens": 150,
            "cached_input_tokens": 0,
            "output_tokens": 45,
            "total_tokens": 195
        }
