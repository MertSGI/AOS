"""Planner provider interface and OpenAI Responses API implementation."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Protocol

class PlannerProvider(Protocol):
    def generate_plan(self, prompt: str, schema: Dict[str, Any]) -> Tuple[Dict[str, Any], str | None, Dict[str, Any] | None]:
        ...

class OpenAIPlannerProvider:
    def __init__(self, model: str = "gpt-5.6-sol"):
        self.model = model

    def generate_plan(self, prompt: str, schema: Dict[str, Any]) -> Tuple[Dict[str, Any], str | None, Dict[str, Any] | None]:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise PermissionError("OPENAI_API_KEY environment variable is missing")

        import openai
        client = openai.OpenAI(api_key=api_key)

        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are the AOS Shadow Planner. Your task is to evaluate canonical project control "
                        "context and output a bounded planner decision JSON matching the provided schema. "
                        "You MUST select the canonical milestone and canonical next_action EXACTLY as provided "
                        "in the bounded input. In shadow mode, mutation_intent MUST be 'NONE' and risk_class MUST be 'R0'."
                    )
                },
                {"role": "user", "content": prompt}
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "planner_decision",
                    "strict": True,
                    "schema": schema
                }
            }
        )

        choice = response.choices[0]
        content_str = choice.message.content
        if not content_str:
            raise ValueError("OpenAI model returned empty content")

        parsed_decision = json.loads(content_str)
        response_id = getattr(response, "id", None)

        usage_data = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage_data = {
                "input_tokens": getattr(u, "prompt_tokens", 0),
                "cached_input_tokens": getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) if hasattr(u, "prompt_tokens_details") else 0,
                "output_tokens": getattr(u, "completion_tokens", 0),
                "total_tokens": getattr(u, "total_tokens", 0)
            }

        return parsed_decision, response_id, usage_data

class FakePlannerProvider:
    def __init__(self, decision_override: Dict[str, Any] | None = None):
        self.decision_override = decision_override

    def generate_plan(self, prompt: str, schema: Dict[str, Any]) -> Tuple[Dict[str, Any], str | None, Dict[str, Any] | None]:
        if self.decision_override:
            return self.decision_override, "fake-response-id-001", {
                "input_tokens": 150,
                "cached_input_tokens": 0,
                "output_tokens": 45,
                "total_tokens": 195
            }

        # Parse prompt to extract context values if possible
        source_sha = "262f7ed87d71419ec469234d4b611c2556069f2d"
        milestone = "Package/Customer Customization"
        next_act = "Audit Core extension points and define package/customer customization contract against frozen Core RC baseline (e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a)"

        for line in prompt.splitlines():
            if line.startswith("RESOLVED SOURCE SHA:"):
                source_sha = line.split(":", 1)[1].strip()
            elif line.startswith("CANONICAL MILESTONE:"):
                milestone = line.split(":", 1)[1].strip()
            elif line.startswith("CANONICAL NEXT ACTION:"):
                next_act = line.split(":", 1)[1].strip()

        decision = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": source_sha,
            "selected_milestone": milestone,
            "selected_next_action": next_act,
            "target_base_sha": None,
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
