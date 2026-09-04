"""Design Intelligence Offline Benchmark Corpus (R18).

Provides adversarial fixtures across 6 diverse project types and runs the critic ensemble
to measure true detection rates, false positives, and false negatives.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import time
from extensions.design_intelligence.contracts import BenchmarkResult, JudgmentVerdict
from extensions.design_intelligence.critics import DesignCriticEnsemble


BENCHMARK_FIXTURES: Dict[str, Dict[str, str]] = {
    "beauty_wellness_minisite_clean": {
        "html": "<html><body><h1>Melis Güzellik Salonu</h1><img src='/logo.png' alt='Logo'/><a href='#book' class='btn btn-primary'>HEMEN RANDEVU AL</a></body></html>",
        "css": "h1 { font-family: 'Playfair Display'; }",
        "expected_verdict": "PASS",
    },
    "b2b_saas_marketing_clean": {
        "html": "<html><body><h1>Enterprise Automation Platform</h1><img src='/ui.png' alt='Platform Dashboard'/><button class='btn btn-primary'>START FREE TRIAL</button></body></html>",
        "css": "h1 { font-family: 'Plus Jakarta Sans'; }",
        "expected_verdict": "PASS",
    },
    "generic_ai_landing_anti_example": {
        "html": "<html><body><h1>Revolutionize Everything</h1><div class='card'>Card 1</div><div class='card'>Card 2</div><div class='card'>Card 3</div><div class='card'>Card 4</div><div class='card'>Card 5</div></body></html>",
        "css": "body { background: linear-gradient(to right, #6366f1, #8b5cf6); backdrop-filter: blur(10px); }",
        "expected_verdict": "FAIL",
    },
    "adversarial_internal_version_leak": {
        "html": "<html><body><h1>LARI UI v2 pilot (AOS-runtime canary)</h1><img src='/logo.png' alt='Logo'/><button>Click</button></body></html>",
        "css": "",
        "expected_verdict": "FAIL",
    },
    "adversarial_fake_ratings_claims": {
        "html": "<html><body><h1>#1 Rated 5.0 Stars (9999 reviews)</h1><button>Book</button></body></html>",
        "css": "",
        "expected_verdict": "FAIL",
    },
    "b2b_dashboard_clean": {
        "html": "<html><body><h1>Metrics & Operations</h1><img src='/dash.png' alt='Analytics'/><button class='btn btn-primary'>EXPORT REPORT</button></body></html>",
        "css": "h1 { font-family: 'Inter'; }",
        "expected_verdict": "PASS",
    },
}


class DesignBenchmarkRunner:
    """Runs design critic benchmark suite against benchmark fixtures."""

    def __init__(self, ensemble: Optional[DesignCriticEnsemble] = None):
        self.ensemble = ensemble or DesignCriticEnsemble()

    def run_benchmark(self) -> List[BenchmarkResult]:
        results = []
        for name, fixture in BENCHMARK_FIXTURES.items():
            t0 = time.time()
            scorecard = self.ensemble.evaluate_project(
                project_id=name,
                html_content=fixture["html"],
                css_content=fixture["css"],
            )
            duration = time.time() - t0

            expected = JudgmentVerdict(fixture["expected_verdict"])
            actual = scorecard.overall_verdict

            passed = (actual == expected) or (expected == JudgmentVerdict.FAIL and actual == JudgmentVerdict.FAIL)
            detected = [f.details for f in scorecard.critic_findings if f.verdict != JudgmentVerdict.PASS]

            fps = []
            fns = []
            if expected == JudgmentVerdict.PASS and actual == JudgmentVerdict.FAIL:
                fps.append(f"False Positive on {name}: Expected PASS, got FAIL")
            elif expected == JudgmentVerdict.FAIL and actual == JudgmentVerdict.PASS:
                fns.append(f"False Negative on {name}: Expected FAIL, got PASS")

            results.append(
                BenchmarkResult(
                    benchmark_id=f"bm-{name}",
                    fixture_name=name,
                    passed=passed,
                    detected_failures=detected,
                    false_positives=fps,
                    false_negatives=fns,
                    duration_seconds=duration,
                )
            )

        return results
