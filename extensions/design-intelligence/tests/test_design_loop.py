"""Unit tests for Autonomous Design Loop Pipeline (R17)."""

import pytest
from extensions.design_intelligence.contracts import DesignProjectBrief, JudgmentVerdict
from extensions.design_intelligence.design_loop import AutonomousDesignLoopPipeline


def test_design_loop_passes_and_auto_revises():
    brief = DesignProjectBrief(
        brief_id="b-loop",
        project_id="p-melis",
        tenant_name="Melis Guzellik",
        industry="Beauty & Wellness",
        target_audience="Salon Clients",
        core_job_to_be_done="Book appointments online",
        brand_posture="Warm Elegance",
    )

    # Initial HTML with internal version leak that gets auto-revised
    leaky_html = """
    <html>
      <body>
        <h1>Melis Güzellik Salonu v2 pilot</h1>
        <img src="/logo.png" alt="Logo" />
        <a href="#book" class="btn btn-primary">HEMEN RANDEVU AL</a>
      </body>
    </html>
    """
    clean_css = "h1 { font-family: 'Playfair Display'; color: #111; }"

    pipeline = AutonomousDesignLoopPipeline(max_design_review_cycles=3)
    res = pipeline.run_pipeline(brief, leaky_html, clean_css)

    # Auto-revision should strip 'v2 pilot' on cycle 1 and pass on cycle 2!
    assert res.overall_verdict == JudgmentVerdict.PASS
    assert res.cycles_completed <= 3
    assert res.human_review_required_with_blockers is False
    assert res.recommendation is not None
