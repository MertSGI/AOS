"""Groq PlannerProvider implementation reusing the OpenAI Python SDK."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Tuple

from jsonschema import Draft202012Validator, FormatChecker

from aos.planner import PlannerContractError, PlannerCredentialError, PlannerTransientError

UNSUPPORTED_GROQ_KEYWORDS = {"$schema", "$id"}
GROQ_MAX_OUTPUT_TOKENS = 2200
GROQ_PLAN_MAX_OUTPUT_TOKENS = 3200
GROQ_OBJECTIVE_MAX_OUTPUT_TOKENS = 1000
GROQ_COMPLETION_MAX_OUTPUT_TOKENS = 1000


def groq_max_output_tokens(schema: Dict[str, Any]) -> int:
    """Reserve output capacity proportionate to the known reasoning contract."""
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        return GROQ_MAX_OUTPUT_TOKENS
    keys = set(properties)
    if {"objective_id", "parallel_candidates", "completion_criteria"}.issubset(keys) and "tasks" not in keys:
        return GROQ_OBJECTIVE_MAX_OUTPUT_TOKENS
    if {"disposition", "satisfied_criteria", "unsatisfied_criteria"}.issubset(keys):
        return GROQ_COMPLETION_MAX_OUTPUT_TOKENS
    if {"objective_id", "tasks", "parallel_safe_groups"}.issubset(keys):
        return GROQ_PLAN_MAX_OUTPUT_TOKENS
    return GROQ_MAX_OUTPUT_TOKENS


def _is_transient_capacity_error(exc: Exception) -> bool:
    """Return whether Groq rejected the request for replaceable capacity reasons.

    Groq can report organization token-throughput exhaustion as HTTP 413 via
    the generic APIStatusError instead of the SDK's RateLimitError.  That is a
    provider-capacity condition, not a planner schema/security violation.
    """
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and status_code >= 500:
        return True
    if status_code != 413:
        return False
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "tokens per minute",
            "rate_limit_exceeded",
            "rate limit",
            "tpm",
        )
    )


def _is_transient_generation_error(exc: Exception) -> bool:
    """Identify provider-side structured-generation misses, not request schema defects."""
    if getattr(exc, "status_code", None) != 400:
        return False
    try:
        body = json.dumps(getattr(exc, "body", {}), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        body = ""
    details = f"{exc} {body}".lower()
    return "json_validate_failed" in details and "failed_generation" in details


def groq_strict_schema_compatible(schema: Any) -> bool:
    """Return whether every object is closed as required by Groq strict mode."""
    if isinstance(schema, list):
        return all(groq_strict_schema_compatible(item) for item in schema)
    if not isinstance(schema, dict):
        return True
    declared_type = schema.get("type")
    is_object = declared_type == "object" or (
        isinstance(declared_type, list) and "object" in declared_type
    )
    if is_object and schema.get("additionalProperties") is not False:
        return False
    return all(groq_strict_schema_compatible(value) for value in schema.values())


def project_groq_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Derive a provider schema projection by stripping meta-schema keywords for Groq strict Structured Outputs."""
    if not isinstance(schema, dict):
        return schema

    projected: Dict[str, Any] = {}
    for k, v in schema.items():
        if k in UNSUPPORTED_GROQ_KEYWORDS:
            continue
        if isinstance(v, dict):
            projected[k] = project_groq_schema(v)
        elif isinstance(v, list):
            projected[k] = [project_groq_schema(item) if isinstance(item, dict) else item for item in v]
        else:
            projected[k] = v
    return projected


def _sanitize_planner_output(data: Any) -> Any:
    """Recursively strip explicit nulls from dictionaries where properties are optional strings/objects."""
    if isinstance(data, dict):
        return {k: _sanitize_planner_output(v) for k, v in data.items() if v is not None}
    if isinstance(data, list):
        return [_sanitize_planner_output(item) for item in data]
    return data


class GroqPlannerProvider:
    """PlannerProvider adapter for Groq using the OpenAI-compatible API."""

    GROQ_BASE_URL = "https://api.groq.com/openai/v1"

    def __init__(self, model: str = "openai/gpt-oss-120b"):
        self.model = model

    def generate_plan(self, prompt: str, schema: Dict[str, Any]) -> Tuple[Dict[str, Any], str | None, Dict[str, Any] | None]:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise PlannerCredentialError("GROQ_API_KEY environment variable is missing")

        import openai
        client = openai.OpenAI(api_key=api_key, base_url=self.GROQ_BASE_URL)

        instructions = (
            "You are the AOS Shadow Planner. Your task is to evaluate canonical project control "
            "context and output a bounded planner decision JSON matching the provided schema. "
            "You MUST select the canonical milestone and canonical next_action EXACTLY as provided "
            "in the bounded input. In shadow mode, mutation_intent MUST be 'NONE' and risk_class MUST be 'R0'."
        )

        provider_schema = project_groq_schema(schema)
        strict_schema = groq_strict_schema_compatible(provider_schema)
        if not strict_schema:
            instructions += (
                " The provider cannot represent this open-ended canonical schema in strict mode. "
                "Return a JSON object matching this canonical schema exactly; AOS will validate it locally: "
                + json.dumps(provider_schema, ensure_ascii=False, sort_keys=True)
            )
        response_format = (
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "planner_decision",
                    "strict": True,
                    "schema": provider_schema,
                },
            }
            if strict_schema
            else {"type": "json_object"}
        )

        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": prompt},
                ],
                response_format=response_format,
                max_tokens=groq_max_output_tokens(schema),
                temperature=0.0,
                store=False,
            )
        except Exception as e:
            err_name = e.__class__.__name__
            if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError)) or _is_transient_capacity_error(e):
                raise PlannerTransientError(f"Groq transient error ({err_name}): {e}") from e
            elif isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
                raise PlannerCredentialError(f"Groq auth/permission failure ({err_name}): {e}") from e
            elif isinstance(e, openai.BadRequestError) and _is_transient_generation_error(e):
                raise PlannerTransientError(f"Groq structured generation transient failure ({err_name}): {e}") from e
            elif isinstance(e, openai.BadRequestError):
                raise PlannerContractError(f"Groq invalid request/schema ({err_name}): {e}") from e
            else:
                raise PlannerContractError(f"Groq provider contract failure ({err_name}): {e}") from e

        # Extract completion
        if not response.choices:
            raise PlannerContractError("Groq returned no choices")

        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason == "length":
            raise PlannerTransientError("Groq response reached the configured output capacity before completing JSON")
        if finish_reason and finish_reason != "stop":
            raise PlannerContractError(f"Groq response finished with unacceptable reason: {finish_reason}")

        content_str = getattr(choice.message, "content", None)
        if not content_str:
            refusal = getattr(choice.message, "refusal", None)
            if refusal:
                raise PlannerContractError(f"Groq model refused response: {refusal}")
            raise PlannerContractError("Groq returned empty content")

        try:
            parsed_decision = json.loads(content_str)
        except Exception as e:
            raise PlannerContractError(f"Groq output is not valid JSON: {e}") from e

        parsed_decision = _sanitize_planner_output(parsed_decision)

        errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(parsed_decision))
        if errors:
            raise PlannerContractError(
                f"Groq output failed canonical JSON schema validation: {errors[0].message}"
            )

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
