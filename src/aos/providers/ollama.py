"""Ollama PlannerProvider implementation using local HTTP API."""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict, Tuple

from jsonschema import Draft202012Validator, FormatChecker

from aos.planner import PlannerContractError, PlannerCredentialError, PlannerTransientError
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
        except urllib.error.URLError as e:
            raise PlannerTransientError(f"Ollama connection error: {e}") from e
        except Exception as e:
            err_name = e.__class__.__name__
            raise PlannerContractError(f"Ollama provider failure ({err_name}): {e}") from e

        done_reason = data.get("done_reason")
        if done_reason == "length":
            raise PlannerTransientError("Ollama response reached configured output capacity before completing JSON")
        if data.get("done") is False:
            raise PlannerContractError("Ollama returned an incomplete non-streaming response")

        # Extract message content
        message = data.get("message", {})
        content_str = message.get("content")

        if not content_str:
            raise PlannerContractError("Ollama returned empty content")

        try:
            parsed_decision = json.loads(content_str)
        except Exception as e:
            raise PlannerContractError(f"Ollama output is not valid JSON: {e}") from e

        parsed_decision = _sanitize_planner_output(parsed_decision, schema)
        errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(parsed_decision))
        if errors:
            raise PlannerContractError(
                f"Ollama output failed canonical JSON schema validation: {errors[0].message}"
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
