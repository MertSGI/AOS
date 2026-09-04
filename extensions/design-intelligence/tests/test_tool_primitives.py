"""Unit tests for Tool, Primitive & Video Intelligence (R16)."""

import pytest
from extensions.design_intelligence.contracts import ProductDemoVideoSpec
from extensions.design_intelligence.tool_primitives import PrimitiveRegistry, StubVideoRendererAdapter


def test_primitive_registration_and_video_stub():
    registry = PrimitiveRegistry()
    ui_prim = registry.register_primitive(
        category="ui",
        name="Gold Accent Button",
        description="Primary CTA button with warm gold hover animation",
        code_snippet_or_spec="<button class='btn-gold'>BOOK NOW</button>",
    )
    assert ui_prim.category == "ui"
    assert len(registry.list_primitives("ui")) == 1

    # Render video using stub adapter
    video_spec = ProductDemoVideoSpec(
        spec_id="vspec-1",
        title="Melis Guzellik Booking Flow Demo",
        duration_seconds=15.0,
        scene_script=[{"scene": 1, "action": "Show Hero"}, {"scene": 2, "action": "Click Booking CTA"}],
    )
    render_result = registry.video_adapter.render_demo_video(video_spec)
    assert render_result["status"] == "STUB_RENDER_SUCCESS"
    assert render_result["duration_seconds"] == 15.0

    recommendation = registry.get_v11_video_recommendation()
    assert "Remotion" in recommendation
    assert "FFmpeg" in recommendation
