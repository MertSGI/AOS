"""Ollama PlannerProvider implementation using local HTTP API."""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict, Tuple

from jsonschema import Draft202012Validator, FormatChecker

from aos.planner import PlannerContractError, PlannerCredentialError, PlannerTransientError
from aos.provider_observation import (
    ContractFailureSubtype,
    FailureFamily,
    extract_rate_limit_observation,
    safe_contract_detail,
)
from aos.providers.schema_utils import sanitize_planner_output as _sanitize_planner_output


class OllamaPlannerProvider:
    """PlannerProvider adapter for local Ollama instance."""

    def __init__(self, model: str = "llama3.3:70b", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def generate_plan(self, prompt: str, schema: Dict[str, Any]) -> Tuple[Dict[str, Any], str | None, Dict[str, Any] | None]:
        instructions = (
            "You are the AOS Shadow Planner. Your task is to evaluate canonical project control "
            "context and output a bounded planner decision JSON matching the provided schema. "
            "You MUST select the canonical milestone and canonical next_action EXACTLY as provided "
            "in the bounded input. In shadow mode, mutation_intent MUST be 'NONE' and risk_class MUST be 'R0'."
        )

        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": prompt},
            ],
            "format": schema,
            "stream": False,
            "options": {
                "temperature": 0.0,
                "num_predict": 1000,
            },
        }

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "AOS-Ollama-Adapter"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            observation = extract_rate_limit_observation(
                provider_id="ollama", model_id=self.model,
                task_class="structured_planning", exc=e,
                headers=e.headers, http_status=e.code,
            )
            if e.code == 429 or e.code >= 500:
                raise PlannerTransientError(
                    f"Ollama HTTP transient error ({e.code})",
                    failure_family=FailureFamily.SERVER_CAPACITY,
                    rate_limit_observation=observation,
                ) from e
            raise PlannerContractError(
                f"Ollama provider failure (HTTPError)",
                subtype=ContractFailureSubtype.BAD_REQUEST if e.code == 400 else ContractFailureSubtype.PROVIDER_CONTRACT_ERROR,
                safe_detail=safe_contract_detail(exception_class="HTTPError", http_status=e.code),
            ) from e
        except urllib.error.URLError as e:
            raise PlannerTransientError("Ollama connection error", failure_family=FailureFamily.LOCAL_SERVICE) from e
        except Exception as e:
            err_name = e.__class__.__name__
            raise PlannerContractError(
                f"Ollama provider failure ({err_name})",
                safe_detail=safe_contract_detail(exception_class=err_name),
            ) from e

        done_reason = data.get("done_reason")
        if done_reason == "length":
            raise PlannerTransientError("Ollama response reached configured output capacity before completing JSON")
        if data.get("done") is False:
            raise PlannerContractError(
                "Ollama returned an incomplete non-streaming response",
                subtype=ContractFailureSubtype.BAD_FINISH_REASON,
                safe_detail=safe_contract_detail(finish_reason=done_reason or "incomplete"),
            )

        # Extract message content
        message = data.get("message", {})
        content_str = message.get("content")

        if not content_str:
            raise PlannerContractError("Ollama returned empty content", subtype=ContractFailureSubtype.EMPTY_CONTENT)

        try:
            parsed_decision = json.loads(content_str)
        except Exception as e:
            raise PlannerContractError(
                "Ollama output is not valid JSON",
                subtype=ContractFailureSubtype.INVALID_JSON,
                safe_detail=safe_contract_detail(parser_class=e.__class__.__name__, line=getattr(e, "lineno", None), column=getattr(e, "colno", None)),
            ) from e

        parsed_decision = _sanitize_planner_output(parsed_decision, schema)
        errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(parsed_decision))
        if errors:
            error = errors[0]
            pointer = "/" + "/".join(str(item) for item in error.absolute_path) if error.absolute_path else "/"
            raise PlannerContractError(
                "Ollama output failed canonical JSON schema validation",
                subtype=ContractFailureSubtype.SCHEMA_VALIDATION,
                safe_detail=safe_contract_detail(validator_keyword=error.validator, json_pointer=pointer),
            )

        # Extract usage
        usage_data = None
        if "eval_count" in data or "prompt_eval_count" in data:
            usage_data = {
                "input_tokens": data.get("prompt_eval_count", 0),
                "cached_input_tokens": 0,
                "output_tokens": data.get("eval_count", 0),
                "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
            }

        return parsed_decision, None, usage_data
