"""Gemini PlannerProvider implementation using the Google GenAI SDK."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Tuple

from aos.planner import PlannerContractError, PlannerCredentialError, PlannerTransientError


class GeminiPlannerProvider:
    """PlannerProvider adapter for Google Gemini via the google-genai SDK."""

    def __init__(self, model: str = "gemini-3.6-flash"):
        self.model = model

    def generate_plan(self, prompt: str, schema: Dict[str, Any]) -> Tuple[Dict[str, Any], str | None, Dict[str, Any] | None]:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise PlannerCredentialError("GEMINI_API_KEY environment variable is missing")

        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        instructions = (
            "You are the AOS Shadow Planner. Your task is to evaluate canonical project control "
            "context and output a bounded planner decision JSON matching the provided schema. "
            "You MUST select the canonical milestone and canonical next_action EXACTLY as provided "
            "in the bounded input. In shadow mode, mutation_intent MUST be 'NONE' and risk_class MUST be 'R0'."
        )

        try:
            response = client.models.generate_content(
                model=self.model,
                contents=f"{instructions}\n\n{prompt}",
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.0,
                    max_output_tokens=1000,
                ),
            )
        except Exception as e:
            err_name = e.__class__.__name__
            err_str = str(e).lower()
            if "api_key" in err_str or "authentication" in err_str or "permission" in err_str or "403" in err_str or "401" in err_str:
                raise PlannerCredentialError(f"Gemini auth/permission failure ({err_name}): {e}") from e
            elif "timeout" in err_str or "connection" in err_str or "rate" in err_str or "429" in err_str or "503" in err_str:
                raise PlannerTransientError(f"Gemini transient error ({err_name}): {e}") from e
            else:
                raise PlannerContractError(f"Gemini provider contract failure ({err_name}): {e}") from e

        # Extract text content
        content_str = None
        if hasattr(response, "text") and response.text:
            content_str = response.text
        elif hasattr(response, "candidates") and response.candidates:
            for candidate in response.candidates:
                if hasattr(candidate, "content") and candidate.content:
                    for part in candidate.content.parts:
                        if hasattr(part, "text") and part.text:
                            content_str = part.text
                            break
                    if content_str:
                        break

        # Check for blocked/refused response
        if hasattr(response, "candidates") and response.candidates:
            for candidate in response.candidates:
                finish_reason = getattr(candidate, "finish_reason", None)
                if finish_reason and str(finish_reason) not in ("STOP", "FinishReason.STOP", "1"):
                    raise PlannerContractError(f"Gemini response finished with reason: {finish_reason}")

        if not content_str:
            raise PlannerContractError("Gemini returned empty content")

        try:
            parsed_decision = json.loads(content_str)
        except Exception as e:
            raise PlannerContractError(f"Gemini output is not valid JSON: {e}") from e

        # Extract usage
        usage_data = None
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            um = response.usage_metadata
            usage_data = {
                "input_tokens": getattr(um, "prompt_token_count", 0) or 0,
                "cached_input_tokens": getattr(um, "cached_content_token_count", 0) or 0,
                "output_tokens": getattr(um, "candidates_token_count", 0) or 0,
                "total_tokens": getattr(um, "total_token_count", 0) or 0,
            }

        return parsed_decision, None, usage_data
