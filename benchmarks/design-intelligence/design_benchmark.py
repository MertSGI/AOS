"""Design Intelligence Offline Benchmark Corpus (R18 / Correction R1).

Expanded 24-fixture benchmark corpus evaluating diverse project types, adversarial failure modes,
and near-miss fixtures for false-positive validation.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import time
from extensions.design_intelligence.contracts import BenchmarkResult, JudgmentVerdict
from extensions.design_intelligence.critics import DesignCriticEnsemble


BENCHMARK_FIXTURES: Dict[str, Dict[str, str]] = {
    # Clean / Valid Fixtures (Expected PASS)
    "01_beauty_wellness_clean": {
        "html": "<html><body><h1>Melis Güzellik Salonu</h1><img src='/logo.png' alt='Melis Güzellik Logo'/><a href='#book' class='btn btn-primary'>HEMEN RANDEVU AL</a></body></html>",
        "css": "h1 { font-family: 'Playfair Display'; }",
        "expected_verdict": "PASS",
    },
    "02_b2b_saas_marketing_clean": {
        "html": "<html><body><h1>Enterprise Automation Platform</h1><img src='/ui.png' alt='Platform Dashboard'/><button class='btn btn-primary'>START FREE TRIAL</button></body></html>",
        "css": "h1 { font-family: 'Plus Jakarta Sans'; }",
        "expected_verdict": "PASS",
    },
    "03_b2b_dashboard_clean": {
        "html": "<html><body><h1>Metrics & Operations</h1><img src='/dash.png' alt='Analytics Graph'/><button class='btn btn-primary'>EXPORT REPORT</button></body></html>",
        "css": "h1 { font-family: 'Inter'; }",
        "expected_verdict": "PASS",
    },
    "04_local_professional_service_clean": {
        "html": "<html><body><h1>Özçelik Hukuk Bürosu</h1><img src='/lawyer.png' alt='Avukat Resmi'/><button class='btn btn-primary'>DANIŞMANLIK AL</button></body></html>",
        "css": "h1 { font-family: 'Merriweather'; }",
        "expected_verdict": "PASS",
    },
    "05_consumer_mobile_first_clean": {
        "html": "<html><body><h1>Hızlı Paket Teslimatı</h1><img src='/app.png' alt='Mobil Uygulama Ekranı'/><button class='btn btn-primary'>İNDİR VE BAŞLA</button></body></html>",
        "css": "h1 { font-family: 'Outfit'; }",
        "expected_verdict": "PASS",
    },
    "06_tenant_minisite_clean": {
        "html": "<html><body><h1>Klinik Diş Sağlığı</h1><img src='/dentist.png' alt='Diş Hekimi'/><button class='btn btn-primary'>ONLINE RANDEVU</button></body></html>",
        "css": "h1 { font-family: 'Roboto'; }",
        "expected_verdict": "PASS",
    },

    # Near-Miss / Boundary Fixtures (Expected PASS - Testing False Positives)
    "07_near_miss_modern_gradient": {
        "html": "<html><body><h1>Custom Modern SaaS</h1><img src='/hero.png' alt='Hero UI'/><button class='btn btn-primary'>GET STARTED</button></body></html>",
        "css": "body { background: linear-gradient(to right, #0f172a, #1e293b); }",  # Dark slate gradient (not generic purple AI gradient)
        "expected_verdict": "PASS",
    },
    "08_near_miss_three_cards": {
        "html": "<html><body><h1>Three Pillar Features</h1><div class='card'>Pillar 1</div><div class='card'>Pillar 2</div><div class='card'>Pillar 3</div><button class='btn btn-primary'>LEARN MORE</button></body></html>",
        "css": "",  # 3 cards is acceptable, not repetitive >4 grid
        "expected_verdict": "PASS",
    },
    "09_near_miss_authentic_testimonial": {
        "html": "<html><body><h1>Müşteri Yorumları</h1><p>\"Randevu sistemimiz harika çalışıyor\" - Ayşe K., Melis Müşterisi</p><button class='btn btn-primary'>RANDEVU AL</button></body></html>",
        "css": "",
        "expected_verdict": "PASS",
    },
    "10_near_miss_single_hero_image": {
        "html": "<html><body><h1>Gerçek Ürün Gösterimi</h1><img src='/product.jpg' alt='Gerçek Salon İç Görünümü'/><button class='btn btn-primary'>İNCELE</button></body></html>",
        "css": "",
        "expected_verdict": "PASS",
    },

    # Adversarial Failure Fixtures (Expected FAIL)
    "11_generic_ai_gradient_and_blobs": {
        "html": "<html><body><h1>Revolutionize Everything</h1><button class='btn'>Go</button></body></html>",
        "css": "body { background: linear-gradient(to right, #6366f1, #8b5cf6); backdrop-filter: blur(10px); }",
        "expected_verdict": "FAIL",
    },
    "12_excessive_card_repetition": {
        "html": "<html><body><h1>Generic SaaS Cards</h1><div class='card'>1</div><div class='card'>2</div><div class='card'>3</div><div class='card'>4</div><div class='card'>5</div><button class='btn'>Go</button></body></html>",
        "css": "body { background: linear-gradient(to right, #6366f1, #8b5cf6); }",
        "expected_verdict": "FAIL",
    },
    "13_internal_version_leak_v2_pilot": {
        "html": "<html><body><h1>Melis Güzellik (v2 pilot build)</h1><button>Book</button></body></html>",
        "css": "",
        "expected_verdict": "FAIL",
    },
    "14_internal_version_leak_aos_runtime": {
        "html": "<html><body><h1>Salon UI (aos-runtime canary)</h1><button>Book</button></body></html>",
        "css": "",
        "expected_verdict": "FAIL",
    },
    "15_unsupported_claims_number_1": {
        "html": "<html><body><h1>#1 Rated 5.0 Stars (9999 reviews)</h1><button>Book</button></body></html>",
        "css": "",
        "expected_verdict": "FAIL",
    },
    "16_unsupported_claims_guaranteed": {
        "html": "<html><body><h1>100% Guaranteed Success Worldwide</h1><button>Book</button></body></html>",
        "css": "",
        "expected_verdict": "FAIL",
    },
    "17_fictional_rating_text": {
        "html": "<html><body><h1>Melis Salonu</h1><p>Dummy rating score 4.9/5</p><button>Book</button></body></html>",
        "css": "",
        "expected_verdict": "FAIL",
    },
    "18_missing_primary_cta": {
        "html": "<html><body><h1>Welcome to Our Service</h1><p>No buttons anywhere on this page</p></body></html>",
        "css": "",
        "expected_verdict": "FAIL",
    },
    "19_competing_primary_ctas": {
        "html": "<html><body><h1>Confusing CTAs</h1><a class='btn-primary'>Buy Now</a><a class='btn-primary'>Sign Up</a><a class='btn-primary'>Book Now</a><a class='btn-primary'>Contact Us</a></body></html>",
        "css": "",
        "expected_verdict": "FAIL",
    },
    "20_missing_h1_title": {
        "html": "<html><body><h2>Only Subheading Here</h2><button class='btn'>Click</button></body></html>",
        "css": "",
        "expected_verdict": "FAIL",
    },

    # Warning / Quality Defect Fixtures (Expected WARN or FAIL)
    "21_missing_img_alt_warning": {
        "html": "<html><body><h1>Image Without Alt</h1><img src='/photo.png'/><button class='btn'>Book</button></body></html>",
        "css": "",
        "expected_verdict": "WARN",
    },
    "22_conflicting_font_declarations_warning": {
        "html": "<html><body><h1>Too Many Fonts</h1><button class='btn'>Click</button></body></html>",
        "css": "h1 { font-family: 'F1'; } p { font-family: 'F2'; } div { font-family: 'F3'; } span { font-family: 'F4'; } a { font-family: 'F5'; } button { font-family: 'F6'; }",
        "expected_verdict": "WARN",
    },
    "23_missing_img_alt_and_competing_ctas": {
        "html": "<html><body><h2>Missing H1</h2><img src='/test.png'/><a class='btn-primary'>A</a><a class='btn-primary'>B</a><a class='btn-primary'>C</a><a class='btn-primary'>D</a></body></html>",
        "css": "",
        "expected_verdict": "FAIL",
    },
    "24_adversarial_combo_gradient_leak": {
        "html": "<html><body><h1>Melis Güzellik v1.8.14</h1><img src='/pic.jpg'/><div class='card'>1</div><div class='card'>2</div><div class='card'>3</div><div class='card'>4</div><button>Click</button></body></html>",
        "css": "body { background: linear-gradient(to right, #6366f1, #8b5cf6); }",
        "expected_verdict": "FAIL",
    },
}


class DesignBenchmarkRunner:
    """Runs design critic benchmark suite against the 24-fixture benchmark corpus."""

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

            passed = (actual == expected)
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
