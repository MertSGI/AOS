"""Real Visual Critic Provider Adapter.

Binds real screenshot evaluation to VisualCriticAdapter contract using direct
Google GenAI SDK multimodal image byte inputs.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Callable
from pathlib import Path
import os
import json
import uuid

from extensions.design_intelligence.contracts import (
    CritiqueFinding,
    JudgmentVerdict,
    EvidenceModality,
    EvidenceOrigin,
    DesignDNA,
    ProductStorySpec,
    GroundedFactLedger,
)
from extensions.design_intelligence.critics import VisualCriticAdapter

DEFAULT_VISUAL_MODEL = "gemini-3.8-flash"


class RealVisualCriticAdapter(VisualCriticAdapter):
    """Production visual critic adapter powered by real image inspection."""

    evidence_origin = EvidenceOrigin.REAL_PROVIDER_VISUAL_REVIEW

    def __init__(
        self,
        model_name: str = DEFAULT_VISUAL_MODEL,
        api_key_env_var: str = "GEMINI_API_KEY",
        client_factory: Optional[Callable[..., Any]] = None,
    ):
        self.model_name = model_name
        self.api_key_env_var = api_key_env_var
        self.client_factory = client_factory

    def evaluate_visuals(
        self,
        screenshot_paths: Dict[int, str],
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        fact_ledger: Optional[GroundedFactLedger] = None,
        negative_preferences: Optional[List[str]] = None,
    ) -> CritiqueFinding:
        """Executes real visual critique across screenshot artifacts using multimodal provider API."""
        try:
            # 1. Check credential presence
            api_key = os.environ.get(self.api_key_env_var, "").strip()
            if not api_key:
                raise ValueError(f"Missing required credential in environment variable '{self.api_key_env_var}'")

            # 2. Validate required screenshot inputs
            if not screenshot_paths:
                raise ValueError("No screenshot paths provided in screenshot_paths dictionary")

            # Pick key viewports (mobile 375px and desktop 1440px)
            mobile_path_str = screenshot_paths.get(375) or next(iter(screenshot_paths.values()))
            desktop_path_str = screenshot_paths.get(1440) or mobile_path_str

            mobile_path = Path(mobile_path_str)
            desktop_path = Path(desktop_path_str)

            if not mobile_path.exists() or not desktop_path.exists():
                raise FileNotFoundError("One or more required screenshot files do not exist")

            # 3. Reject zero-byte screenshot artifacts
            mobile_bytes = mobile_path.read_bytes()
            desktop_bytes = desktop_path.read_bytes()

            if len(mobile_bytes) == 0 or len(desktop_bytes) == 0:
                raise ValueError("Zero-byte screenshot artifact detected")

            # 4. Construct client & multimodal image parts using google-genai
            if self.client_factory:
                client = self.client_factory(api_key=api_key)
            else:
                from google import genai
                client = genai.Client(api_key=api_key)

            from google.genai import types

            mobile_part = types.Part.from_bytes(data=mobile_bytes, mime_type="image/png")
            desktop_part = types.Part.from_bytes(data=desktop_bytes, mime_type="image/png")

            prompt = """
Perform a strict design critique on these visual website screenshots (Mobile 375px and Desktop 1440px).

Evaluate:
1. Business identity prominence & clarity
2. Visual hierarchy & layout density
3. Generic / template / AI appearance risk
4. Primary CTA dominance
5. Mobile first-fold composition

Respond with EXACT JSON format matching this schema:
{
  "verdict": "PASS" | "WARN" | "FAIL",
  "is_generic_or_template": true | false,
  "findings": ["finding 1", "finding 2"],
  "suggested_fix": "fix string or null"
}
"""

            contents = [mobile_part, desktop_part, prompt]

            # Native structured output response schema where supported
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
            )

            response = client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=config,
            )

            # Check for blocked/refused/empty response
            if not response or not getattr(response, "text", None):
                raise ValueError("Provider response was empty or blocked/refused")

            output_text = response.text.strip()
            if not output_text:
                raise ValueError("Provider returned empty text output")

            # Parse JSON safely
            json_str = output_text
            if "```json" in output_text:
                json_str = output_text.split("```json")[1].split("```")[0].strip()
            elif "```" in output_text:
                json_str = output_text.split("```")[1].split("```")[0].strip()

            try:
                parsed = json.loads(json_str)
            except Exception as json_err:
                raise ValueError(f"Provider output is malformed JSON: {json_err}")

            if not isinstance(parsed, dict):
                raise ValueError("Parsed JSON response is not an object/dict")

            # Strict field checks
            if "verdict" not in parsed:
                raise ValueError("Response missing required field 'verdict'")

            verdict_raw = parsed["verdict"]
            if not isinstance(verdict_raw, str):
                raise ValueError("Field 'verdict' is not a string")

            verdict_str = verdict_raw.strip().upper()
            if verdict_str not in ("PASS", "WARN", "FAIL"):
                raise ValueError(f"Unknown verdict value '{verdict_str}'")

            verdict = JudgmentVerdict[verdict_str]

            if "is_generic_or_template" not in parsed:
                raise ValueError("Response missing required field 'is_generic_or_template'")

            is_generic = parsed["is_generic_or_template"]
            if not isinstance(is_generic, bool):
                raise ValueError("Field 'is_generic_or_template' is not a boolean")

            # Generic template forces FAIL if verdict was PASS
            if is_generic and verdict == JudgmentVerdict.PASS:
                verdict = JudgmentVerdict.FAIL

            findings_raw = parsed.get("findings", [])
            if not isinstance(findings_raw, list):
                raise ValueError("Field 'findings' is not a list")

            for f_item in findings_raw:
                if not isinstance(f_item, str):
                    raise ValueError("Finding element in 'findings' is not a string")

            details = "; ".join(findings_raw) if findings_raw else "Visual inspection passed clean."

            suggested_fix = parsed.get("suggested_fix")
            if suggested_fix is not None and not isinstance(suggested_fix, str):
                raise ValueError("Field 'suggested_fix' must be a string or null")

        except Exception as e:
            # Strict fail closed behavior for missing credentials, malformed output, network errors, etc.
            verdict = JudgmentVerdict.FAIL
            details = f"Real visual provider evaluation failed or closed: {e}"
            suggested_fix = "Verify screenshot files and provider accessibility."

        return CritiqueFinding(
            finding_id=f"f-realvis-{uuid.uuid4().hex[:6]}",
            critic_name="RealVisualCriticAdapter",
            verdict=verdict,
            dimension="pixel_visual_quality",
            title="Real Provider Pixel Visual Critique",
            details=details,
            evidence_modality=EvidenceModality.PIXEL_VISUAL,
            evidence_ids=[os.path.basename(p) for p in screenshot_paths.values()],
            suggested_fix=suggested_fix,
        )
