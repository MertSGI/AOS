"""Real Visual Critic Provider Adapter (Section 7 & 8).

Binds real screenshot evaluation to VisualCriticAdapter contract using the verified
Antigravity CLI binary runtime.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from pathlib import Path
import os
import subprocess
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

ANTIGRAVITY_EXE = r"C:\Users\mozcelikbas\AppData\Local\AOS\runtime\antigravity-cli\1.1.20\antigravity.exe"


class RealVisualCriticAdapter(VisualCriticAdapter):
    """Production visual critic adapter powered by real image inspection (Section 7)."""

    evidence_origin = EvidenceOrigin.REAL_PROVIDER_VISUAL_REVIEW

    def __init__(self, cli_path: Optional[str] = None):
        self.cli_path = cli_path or ANTIGRAVITY_EXE

    def evaluate_visuals(
        self,
        screenshot_paths: Dict[int, str],
        dna: Optional[DesignDNA] = None,
        story: Optional[ProductStorySpec] = None,
        fact_ledger: Optional[GroundedFactLedger] = None,
        negative_preferences: Optional[List[str]] = None,
    ) -> CritiqueFinding:
        """Executes real visual critique across screenshot artifacts."""
        try:
            if not os.path.exists(self.cli_path):
                raise RuntimeError(f"Antigravity CLI binary not found at '{self.cli_path}'")

            # Pick key viewports (mobile 375px and desktop 1440px)
            mobile_path = screenshot_paths.get(375) or next(iter(screenshot_paths.values()))
            desktop_path = screenshot_paths.get(1440) or mobile_path

            prompt = f"""
Perform a strict design critique on these visual website screenshot files:
Mobile (375px): {mobile_path}
Desktop (1440px): {desktop_path}

Evaluate:
1. Business identity prominence & clarity
2. Visual hierarchy & layout density
3. Generic / template / AI appearance risk
4. Primary CTA dominance
5. Mobile first-fold composition

Respond with EXACT JSON format:
{{
  "verdict": "PASS" | "WARN" | "FAIL",
  "is_generic_or_template": true | false,
  "findings": ["finding 1", "finding 2"],
  "suggested_fix": "fix string or null"
}}
"""

            cmd = [
                self.cli_path,
                "--print", prompt,
                "--model", "gemini-3.8-flash-low",
                "--dangerously-skip-permissions",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=45, check=True)
            output = res.stdout.strip()
            
            # Parse JSON from output
            json_str = output
            if "```json" in output:
                json_str = output.split("```json")[1].split("```")[0].strip()
            elif "```" in output:
                json_str = output.split("```")[1].split("```")[0].strip()

            parsed = json.loads(json_str)
            verdict_str = str(parsed.get("verdict", "PASS")).upper()
            verdict = JudgmentVerdict[verdict_str] if verdict_str in JudgmentVerdict.__members__ else JudgmentVerdict.PASS
            is_generic = bool(parsed.get("is_generic_or_template", False))
            if is_generic and verdict == JudgmentVerdict.PASS:
                verdict = JudgmentVerdict.FAIL

            findings_list = parsed.get("findings", [])
            details = "; ".join(findings_list) if findings_list else "Visual inspection passed clean."
            suggested_fix = parsed.get("suggested_fix")

        except Exception as e:
            # If CLI fail-closed or parsing error occurs, fail closed
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
