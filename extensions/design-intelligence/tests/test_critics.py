"""Unit tests for Design Critic Ensemble (R13)."""

import pytest
from extensions.design_intelligence.contracts import JudgmentVerdict
from extensions.design_intelligence.critics import DesignCriticEnsemble


def test_clean_design_passes_critic_ensemble():
    ensemble = DesignCriticEnsemble()
    clean_html = """
    <!DOCTYPE html>
    <html>
      <head><title>Melis Güzellik Salonu</title></head>
      <body>
        <header>
          <h1>Melis Güzellik Salonu - Randevu Sistemi</h1>
          <img src="/logo.png" alt="Melis Güzellik Logo" />
        </header>
        <main>
          <a href="#randevu" class="btn btn-primary">HEMEN RANDEVU AL</a>
        </main>
      </body>
    </html>
    """
    clean_css = "h1 { font-family: 'Playfair Display', serif; color: #111827; }"

    scorecard = ensemble.evaluate_project("p-clean", clean_html, clean_css)
    assert scorecard.overall_verdict == JudgmentVerdict.PASS
    assert not scorecard.has_critical_failure()


def test_internal_version_leak_causes_critical_fail():
    ensemble = DesignCriticEnsemble()
    leaky_html = """
    <div>
      <h1>Melis Güzellik (v2 pilot - AOS-runtime build 1.8.14)</h1>
      <img src="/logo.png" alt="Logo" />
      <button class="btn">Book</button>
    </div>
    """
    scorecard = ensemble.evaluate_project("p-leaky", leaky_html, "")
    assert scorecard.overall_verdict == JudgmentVerdict.FAIL
    assert scorecard.has_critical_failure()

    # ProductSemanticsCritic MUST have failed
    semantics_finding = next(f for f in scorecard.critic_findings if f.critic_name == "ProductSemanticsCritic")
    assert semantics_finding.verdict == JudgmentVerdict.FAIL
    assert "v2 pilot" in semantics_finding.details
