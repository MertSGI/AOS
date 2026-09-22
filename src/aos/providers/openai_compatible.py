"""Generic OpenAI-compatible planner provider implementation for AOS."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from jsonschema import Draft202012Validator, FormatChecker

from aos.planner import PlannerContractError, PlannerCredentialError, PlannerTransientError
from aos.providers.schema_utils import sanitize_planner_output

UNSUPPORTED_META_KEYWORDS = {"$schema", "$id"}
DEFAULT_MAX_OUTPUT_TOKENS = 2200
PLAN_MAX_OUTPUT_TOKENS = 3200
OBJECTIVE_MAX_OUTPUT_TOKENS = 1000
COMPLETION_MAX_OUTPUT_TOKENS = 1000


def _is_credit_exhaustion_error(exc: Exception) -> bool:
    """Recognize account-credit exhaustion without conflating contract errors."""
    if getattr(exc, "status_code", None) == 402:
        return True
    text = str(exc).lower()
    explicit_markers = (
        "payment required",
        "insufficient credit",
        "insufficient credits",
        "out of credit",
        "out of credits",
        "credit balance exhausted",
        "monthly included credits",
        "pre-paid credits",
        "prepaid credits",
    )
    return any(marker in text for marker in explicit_markers) or (
        "depleted" in text and "credit" in text
    )


def default_max_output_tokens(schema: Dict[str, Any], default_val: int = DEFAULT_MAX_OUTPUT_TOKENS) -> int:
    """Reserve output capacity proportionate to the known reasoning contract."""
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        return default_val
    keys = set(properties)
    if {"objective_id", "parallel_candidates", "completion_criteria"}.issubset(keys) and "tasks" not in keys:
        return OBJECTIVE_MAX_OUTPUT_TOKENS
    if {"disposition", "satisfied_criteria", "unsatisfied_criteria"}.issubset(keys):
        return COMPLETION_MAX_OUTPUT_TOKENS
    if {"objective_id", "tasks", "parallel_safe_groups"}.issubset(keys):
        return max(PLAN_MAX_OUTPUT_TOKENS, default_val)
    return default_val


def project_openai_compatible_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Strip unsupported meta-schema keywords ($schema, $id)."""
    if not isinstance(schema, dict):
        return schema
    projected: Dict[str, Any] = {}
    for k, v in schema.items():
        if k in UNSUPPORTED_META_KEYWORDS:
            continue
        if isinstance(v, dict):
            projected[k] = project_openai_compatible_schema(v)
        elif isinstance(v, list):
            projected[k] = [project_openai_compatible_schema(item) if isinstance(item, dict) else item for item in v]
        else:
            projected[k] = v
    return projected


def strict_schema_compatible(schema: Any) -> bool:
    """Return whether every object is closed as required by strict structured outputs."""
    if isinstance(schema, list):
        return all(strict_schema_compatible(item) for item in schema)
    if not isinstance(schema, dict):
        return True
    declared_type = schema.get("type")
    is_object = declared_type == "object" or (
        isinstance(declared_type, list) and "object" in declared_type
    )
    if is_object and schema.get("additionalProperties") is not False:
        return False
    return all(strict_schema_compatible(value) for value in schema.values())


class GenericOpenAICompatiblePlannerProvider:
    """Bounded, policy-driven OpenAI-compatible provider adapter."""

    execution_provenance = "LIVE_EXTERNAL"

    def __init__(
        self,
        provider_id: str,
        model: str,
        base_url: str,
        credential_env_var: Optional[str] = None,
        api_protocol: str = "OPENAI_CHAT_COMPLETIONS",
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        default_headers: Optional[Dict[str, str]] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        cloud_local: str = "CLOUD",
        billing_class: str = "FREE",
    ):
        self.provider_id = provider_id
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.credential_env_var = credential_env_var
        self.api_protocol = api_protocol.upper()
        self.max_output_tokens = max_output_tokens
        self.default_headers = default_headers or {}
        self.extra_body = extra_body or {}
        if self.provider_id == "openrouter_free":
            provider_preferences = dict(self.extra_body.get("provider") or {})
            provider_preferences.setdefault("require_parameters", True)
            self.extra_body = {**self.extra_body, "provider": provider_preferences}
        self.cloud_local = cloud_local.upper()
        self.billing_class = billing_class.upper()
        if self.cloud_local == "LOCAL":
            self.execution_provenance = "LOCAL_OFFLINE"

    def _resolve_base_url(self) -> str:
        """Substitute non-secret environment variables in base_url if present."""
        url = self.base_url
        if "{CLOUDFLARE_ACCOUNT_ID}" in url:
            acc_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
            if not acc_id:
                raise PlannerCredentialError("CLOUDFLARE_ACCOUNT_ID environment variable is missing")
            url = url.replace("{CLOUDFLARE_ACCOUNT_ID}", acc_id)
        return url

    def generate_plan(self, prompt: str, schema: Dict[str, Any]) -> Tuple[Dict[str, Any], str | None, Dict[str, Any] | None]:
        api_key = None
        if self.credential_env_var:
            api_key = os.environ.get(self.credential_env_var)
            if not api_key:
                raise PlannerCredentialError(f"{self.credential_env_var} environment variable is missing for {self.provider_id}")
        else:
            api_key = "dummy-key-not-required"

        base_url = self._resolve_base_url()

        import openai
        client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=self.default_headers if self.default_headers else None,
        )

        instructions = (
            "You are the AOS Shadow Planner. Your task is to evaluate canonical project control "
            "context and output a bounded planner decision JSON matching the provided schema. "
            "You MUST select the canonical milestone and canonical next_action EXACTLY as provided "
            "in the bounded input. In shadow mode, mutation_intent MUST be 'NONE' and risk_class MUST be 'R0'."
        )

        provider_schema = project_openai_compatible_schema(schema)

        if self.api_protocol == "OPENAI_RESPONSES":
            if not hasattr(client, "responses") or not callable(getattr(client.responses, "create", None)):
                raise PlannerContractError("OpenAI Responses API (client.responses.create) is unavailable in current SDK")
            try:
                response = client.responses.create(
                    model=self.model,
                    instructions=instructions,
                    input=prompt,
                    store=False,
                    reasoning={"effort": "medium"},
                    max_output_tokens=min(self.max_output_tokens, 2000),
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "planner_decision",
                            "strict": True,
                            "schema": provider_schema,
                        }
                    },
                )
            except Exception as e:
                err_name = e.__class__.__name__
                if _is_credit_exhaustion_error(e):
                    raise PlannerTransientError(
                        f"{self.provider_id} CREDIT_EXHAUSTED ({err_name}): {e}"
                    ) from e
                elif (
                    isinstance(e, (
                        openai.APIConnectionError,
                        openai.APITimeoutError,
                        openai.RateLimitError,
                        openai.InternalServerError,
                    ))
                    or (isinstance(e, openai.APIStatusError) and getattr(e, "status_code", 0) >= 500)
                ):
                    raise PlannerTransientError(f"{self.provider_id} transient error ({err_name}): {e}") from e
                elif isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
                    raise PlannerCredentialError(f"{self.provider_id} auth/permission failure ({err_name}): {e}") from e
                elif isinstance(e, openai.BadRequestError):
                    raise PlannerContractError(f"{self.provider_id} invalid request/schema ({err_name}): {e}") from e
                else:
                    raise PlannerContractError(f"{self.provider_id} provider contract failure ({err_name}): {e}") from e

            status = getattr(response, "status", None)
            if status and status not in ("completed", "complete"):
                inc_details = getattr(response, "incomplete_details", None)
                reason = getattr(inc_details, "reason", None)
                if reason is None and isinstance(inc_details, dict):
                    reason = inc_details.get("reason")
                if str(reason).lower() in ("max_output_tokens", "max_tokens"):
                    raise PlannerTransientError(
                        f"{self.provider_id} response reached configured output capacity before completing JSON"
                    )
                raise PlannerContractError(f"{self.provider_id} response status '{status}' incomplete: {inc_details}")

            content_str = None
            if hasattr(response, "output_text") and response.output_text:
                content_str = response.output_text
            elif hasattr(response, "output") and response.output:
                for item in response.output:
                    if getattr(item, "type", None) == "message" and hasattr(item, "content"):
                        for part in item.content:
                            if getattr(part, "type", None) == "refusal":
                                raise PlannerContractError(f"{self.provider_id} model refused response: {getattr(part, 'refusal', '')}")
                            if getattr(part, "type", None) == "text":
                                content_str = getattr(part, "text", None)
                                if content_str:
                                    break
            if not content_str:
                raise PlannerContractError(f"{self.provider_id} returned empty content")
            response_id = getattr(response, "id", None)
            usage_data = None
            if hasattr(response, "usage") and response.usage:
                u = response.usage
                usage_data = {
                    "input_tokens": getattr(u, "input_tokens", getattr(u, "prompt_tokens", 0)),
                    "cached_input_tokens": 0,
                    "output_tokens": getattr(u, "output_tokens", getattr(u, "completion_tokens", 0)),
                    "total_tokens": getattr(u, "total_tokens", 0),
                }

        else:
            # Standard OPENAI_CHAT_COMPLETIONS
            strict_compatible = strict_schema_compatible(provider_schema)
            if not strict_compatible:
                instructions += (
                    " The provider cannot represent this open-ended canonical schema in strict mode. "
                    "Return a JSON object matching this canonical schema exactly; AOS will validate it locally: "
                    + json.dumps(provider_schema, ensure_ascii=False, sort_keys=True)
                )
            strict_response_format = (
                {"type": "json_schema", "json_schema": provider_schema}
                if self.provider_id == "cloudflare"
                else {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "planner_decision",
                        "strict": True,
                        "schema": provider_schema,
                    },
                }
            )
            response_format = (
                strict_response_format
                if strict_compatible
                else {"type": "json_object"}
            )

            kwargs: Dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": prompt},
                ],
                "response_format": response_format,
                "max_tokens": default_max_output_tokens(schema, self.max_output_tokens),
                "temperature": 0.0,
            }
            if self.extra_body:
                kwargs["extra_body"] = self.extra_body

            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as e:
                err_name = e.__class__.__name__
                msg_lower = str(e).lower()
                if _is_credit_exhaustion_error(e):
                    raise PlannerTransientError(
                        f"{self.provider_id} CREDIT_EXHAUSTED ({err_name}): {e}"
                    ) from e
                elif (
                    isinstance(e, (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError))
                    or any(code in msg_lower for code in ("tokens per minute", "rate_limit_exceeded", "rate limit", "tpm", "429", "quota", "500", "502", "503", "504"))
                ):
                    raise PlannerTransientError(f"{self.provider_id} transient/capacity error ({err_name}): {e}") from e
                elif isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)) or any(code in msg_lower for code in ("401", "403", "unauthorized", "forbidden")):
                    raise PlannerCredentialError(f"{self.provider_id} auth/permission failure ({err_name}): {e}") from e
                elif isinstance(e, openai.BadRequestError) and ("json_validate_failed" in msg_lower or "failed_generation" in msg_lower):
                    raise PlannerTransientError(f"{self.provider_id} structured generation transient failure ({err_name}): {e}") from e
                elif isinstance(e, openai.BadRequestError):
                    raise PlannerContractError(f"{self.provider_id} invalid request/schema ({err_name}): {e}") from e
                else:
                    raise PlannerContractError(f"{self.provider_id} provider contract failure ({err_name}): {e}") from e

            if not response.choices:
                raise PlannerContractError(f"{self.provider_id} returned no choices")

            choice = response.choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason == "length":
                raise PlannerTransientError(f"{self.provider_id} response reached configured output capacity before completing JSON")
            if finish_reason and finish_reason != "stop":
                raise PlannerContractError(f"{self.provider_id} response finished with unacceptable reason: {finish_reason}")

            content_str = getattr(choice.message, "content", None)
            if not content_str:
                refusal = getattr(choice.message, "refusal", None)
                if refusal:
                    raise PlannerContractError(f"{self.provider_id} model refused response: {refusal}")
                raise PlannerContractError(f"{self.provider_id} returned empty content")

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

        try:
            parsed_decision = json.loads(content_str)
        except Exception as e:
            raise PlannerContractError(f"{self.provider_id} output is not valid JSON: {e}") from e

        parsed_decision = sanitize_planner_output(parsed_decision, schema)
        errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(parsed_decision))
        if errors:
            raise PlannerContractError(f"{self.provider_id} output failed canonical JSON schema validation: {errors[0].message}")

        return parsed_decision, response_id, usage_data
