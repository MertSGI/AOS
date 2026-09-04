"""Tool, Primitive & Video Intelligence Subsystem (R16).

Maintains a registry of UI, motion, and interaction primitives.
Provides renderer adapter interfaces for ProductDemoVideoSpec and V1.1 video roadmap recommendations.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import uuid
from extensions.design_intelligence.contracts import ProductDemoVideoSpec


@dataclass
class PrimitiveDefinition:
    primitive_id: str
    category: str  # ui, motion, interaction, tooling, video
    name: str
    description: str
    code_snippet_or_spec: str


class BaseVideoRendererAdapter:
    """Interface for video rendering adapters."""

    def render_demo_video(self, spec: ProductDemoVideoSpec) -> Dict[str, Any]:
        raise NotImplementedError


class StubVideoRendererAdapter(BaseVideoRendererAdapter):
    """V1 lightweight stub adapter for demo video generation."""

    def render_demo_video(self, spec: ProductDemoVideoSpec) -> Dict[str, Any]:
        return {
            "status": "STUB_RENDER_SUCCESS",
            "video_spec_id": spec.spec_id,
            "output_artifact": f"/artifacts/video/{spec.title.lower().replace(' ', '_')}.mp4",
            "duration_seconds": spec.duration_seconds,
            "message": "Rendered via V1 stub video adapter. Ready for Remotion / FFmpeg V1.1 integration.",
        }


class PrimitiveRegistry:
    """Registry of UI, motion, and interaction primitives."""

    def __init__(self):
        self._primitives: Dict[str, PrimitiveDefinition] = {}
        self.video_adapter: BaseVideoRendererAdapter = StubVideoRendererAdapter()

    def register_primitive(
        self,
        category: str,
        name: str,
        description: str,
        code_snippet_or_spec: str,
    ) -> PrimitiveDefinition:
        pid = f"prim-{uuid.uuid4().hex[:8]}"
        prim = PrimitiveDefinition(
            primitive_id=pid,
            category=category,
            name=name,
            description=description,
            code_snippet_or_spec=code_snippet_or_spec,
        )
        self._primitives[pid] = prim
        return prim

    def list_primitives(self, category: Optional[str] = None) -> List[PrimitiveDefinition]:
        if not category:
            return list(self._primitives.values())
        return [p for p in self._primitives.values() if p.category == category]

    def get_v11_video_recommendation(self) -> str:
        return (
            "V1.1 Programmatic Demo Video Recommendation:\n"
            "1. Remotion (React-based programmatic video rendering pipeline) for HTML/CSS canvas capture.\n"
            "2. FFmpeg headless compositing for audio track and scene transition stitching.\n"
            "3. Playwright viewport recording for automated product flow walk-throughs."
        )
