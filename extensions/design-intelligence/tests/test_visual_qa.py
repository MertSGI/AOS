"""Unit tests for Visual Evidence & Responsive QA (R14)."""

import pytest
from extensions.design_intelligence.visual_qa import (
    FakeBrowserScreenshotAdapter,
    VisualQAEvaluator,
    REQUIRED_VIEWPORTS,
)


def test_clean_visual_qa_evaluation():
    adapter = FakeBrowserScreenshotAdapter(simulate_overflow=False, simulate_missing_cta=False)
    manifest = adapter.capture_manifest("http://localhost:3000/#/melis-guzellik", "run-101")

    evaluator = VisualQAEvaluator()
    result = evaluator.evaluate_manifest(manifest)

    assert result.all_required_viewports_covered is True
    assert len(result.overflow_detected_viewports) == 0
    assert len(result.cta_missing_viewports) == 0
    assert result.overall_pass is True


def test_overflow_and_missing_cta_fails_visual_qa():
    adapter = FakeBrowserScreenshotAdapter(simulate_overflow=True, simulate_missing_cta=True)
    manifest = adapter.capture_manifest("http://localhost:3000/#/melis-guzellik", "run-102")

    evaluator = VisualQAEvaluator()
    result = evaluator.evaluate_manifest(manifest)

    assert result.overall_pass is False
    assert 375 in result.overflow_detected_viewports
    assert 375 in result.cta_missing_viewports
