"""NVIDIA Nemotron PlannerProvider implementation reusing the OpenAI Python SDK."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Tuple

from aos.planner import PlannerContractError, PlannerCredentialError, PlannerTransientError

UNSUPPORTED_NEMOTRON_KEYWORDS = {"$schema", "$id"}


def project_nemotron_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Derive a provider schema projection by stripping meta-schema keywords for strict Structured Outputs."""
    if not isinstance(schema, dict):
        return schema

    projected: Dict[str, Any] = {}
    for k, v in schema.items():
        if k in UNSUPPORTED_NEMOTRON_KEYWORDS:
            continue
        if isinstance(v, dict):
            projected[k] = project_nemotron_schema(v)
        elif isinstance(v, list):
            projected[k] = [project_nemotron_schema(item) if isinstance(item, dict) else item for item in v]
        else:
            projected[k] = v
    return projected


class NemotronPlannerProvider:
    """PlannerProvider adapter for NVIDIA Nemotron using OpenAI-compatible API."""

    NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
    DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"

    def __init__(self, model: str = DEFAULT_MODEL, thinking_budget: int = 0):
        self.model = model
        self.thinking_budget = thinking_budget

    def generate_plan(self, prompt: str, schema: Dict[str, Any]) -> Tuple[Dict[str, Any], str | None, Dict[str, Any] | None]:
        api_key = os.environ.get("NVIDIA_API_KEY")
        if not api_key:
            raise PlannerCredentialError("NVIDIA_API_KEY environment variable is missing")

        import openai
        client = openai.OpenAI(api_key=api_key, base_url=self.NVIDIA_BASE_URL)

        instructions = (
            "You are the AOS Shadow Planner. Your task is to evaluate canonical project control "
            "context and output a bounded planner decision JSON matching the provided schema. "
            "You MUST select the canonical milestone and canonical next_action EXACTLY as provided "
            "in the bounded input. In shadow mode, mutation_intent MUST be 'NONE' and risk_class MUST be 'R0'."
        )

        provider_schema = project_nemotron_schema(schema)

        extra_body: Dict[str, Any] = {}
        if self.thinking_budget > 0:
            extra_body["reasoning"] = {"effort": "high"}

        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "planner_decision",
                        "strict": True,
                        "schema": provider_schema,
                    },
                },
                max_tokens=1000,
                temperature=0.0,
                store=False,
                **({"extra_body": extra_body} if extra_body else {})
            )
        except Exception as e:
            err_name = e.__class__.__name__
            if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError)):
                raise PlannerTransientError(f"Nemotron transient error ({err_name}): {e}") from e
            elif isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
                raise PlannerCredentialError(f"Nemotron auth/permission failure ({err_name}): {e}") from e
            elif isinstance(e, openai.BadRequestError):
                raise PlannerContractError(f"Nemotron invalid request/schema ({err_name}): {e}") from e
            else:
                raise PlannerContractError(f"Nemotron provider contract failure ({err_name}): {e}") from e

        if not response.choices:
            raise PlannerContractError("Nemotron returned no choices")

        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason and finish_reason != "stop":
            raise PlannerContractError(f"Nemotron response finished with unacceptable reason: {finish_reason}")

        content_str = getattr(choice.message, "content", None)
        if not content_str:
            refusal = getattr(choice.message, "refusal", None)
            if refusal:
                raise PlannerContractError(f"Nemotron model refused response: {refusal}")
            raise PlannerContractError("Nemotron returned empty content")

        # INVARIANT: Raw reasoning/chain-of-thought content is strictly dropped and never returned or serialized
        try:
            parsed_decision = json.loads(content_str)
        except Exception as e:
            raise PlannerContractError(f"Nemotron output is not valid JSON: {e}") from e

        response_id = getattr(response, "id", None)

        usage_data = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage_data = {
                "input_tokens": getattr(u, "prompt_tokens", 0) or 0,
                "cached_input_tokens": 0,
                "output_tokens": getattr(u, "completion_tokens", 0) or 0,
                "total_tokens": getattr(u, "total_tokens", 0) or 0,
            }

        return parsed_decision, response_id, usage_data
