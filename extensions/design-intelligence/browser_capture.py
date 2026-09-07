"""Real Browser Capture Adapter.

Captures actual responsive browser screenshots via Playwright, computes full SHA256 hashes,
validates layout metrics (horizontal overflow, rendered CTA visibility), and emits VisualEvidenceManifest.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from pathlib import Path
import os
import sys
import datetime
import hashlib
import uuid
import asyncio
import tempfile

from extensions.design_intelligence.contracts import VisualEvidenceManifest
from extensions.design_intelligence.visual_qa import (
    BaseBrowserScreenshotAdapter,
    REQUIRED_VIEWPORTS,
)


class RealBrowserCaptureAdapter(BaseBrowserScreenshotAdapter):
    """Production Playwright-based browser capture adapter."""

    def __init__(self, output_dir: Optional[str] = None, playwright_browsers_path: Optional[str] = None):
        self.output_dir = output_dir or os.path.join(tempfile.gettempdir(), "aos_evidence", "screenshots")
        os.makedirs(self.output_dir, exist_ok=True)
        self.playwright_browsers_path = playwright_browsers_path

    def capture_manifest(self, url: str, run_id: str) -> VisualEvidenceManifest:
        """Captures actual responsive screenshots for all required viewports."""
        return asyncio.run(self._async_capture(url, run_id))

    async def _async_capture(self, url: str, run_id: str) -> VisualEvidenceManifest:
        # Determine environment override for PLAYWRIGHT_BROWSERS_PATH
        env_browsers_path = self.playwright_browsers_path or os.environ.get("AOS_PLAYWRIGHT_BROWSERS_PATH")
        
        orig_env_val = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        env_was_set = "PLAYWRIGHT_BROWSERS_PATH" in os.environ

        if env_browsers_path:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = env_browsers_path

        try:
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise RuntimeError("Playwright is not installed or accessible in current Python environment.")

            screenshot_paths: Dict[int, str] = {}
            overflow_map: Dict[int, bool] = {}
            cta_map: Dict[int, bool] = {}
            file_hashes: Dict[int, str] = {}
            viewports_captured: List[int] = []

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()

                    for vp in REQUIRED_VIEWPORTS:
                        await page.set_viewport_size({"width": vp, "height": 900})

                        if url.startswith("data:text/html") or url.startswith("http") or url.startswith("file:"):
                            await page.goto(url, wait_until="networkidle", timeout=10000)
                        elif os.path.exists(url):
                            file_url = Path(url).absolute().as_uri()
                            await page.goto(file_url, wait_until="networkidle", timeout=10000)
                        else:
                            await page.set_content(url)

                        # Check horizontal overflow
                        overflow = await page.evaluate(
                            "() => document.documentElement.scrollWidth > window.innerWidth"
                        )
                        overflow_map[vp] = bool(overflow)

                        # Check CTA rendered visibility (Playwright element handle visibility + non-zero geometry)
                        cta_visible = False
                        cta_elements = await page.query_selector_all('.btn-primary, button, a.btn, [role="button"]')
                        for elem in cta_elements:
                            if await elem.is_visible():
                                box = await elem.bounding_box()
                                if box and box["width"] > 0 and box["height"] > 0:
                                    cta_visible = True
                                    break
                        cta_map[vp] = cta_visible

                        # Capture PNG
                        shot_path = os.path.join(self.output_dir, f"{run_id}_{vp}px.png")
                        await page.screenshot(path=shot_path, full_page=False)

                        # Validate file exists & non-zero
                        data = Path(shot_path).read_bytes()
                        if len(data) == 0:
                            raise RuntimeError(f"Captured screenshot file '{shot_path}' is zero-byte")

                        h64 = hashlib.sha256(data).hexdigest()

                        screenshot_paths[vp] = shot_path
                        file_hashes[vp] = h64
                        viewports_captured.append(vp)
                finally:
                    await browser.close()

            # Require offset-aware ISO timestamp
            captured_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

            return VisualEvidenceManifest(
                manifest_id=f"vis-{uuid.uuid4().hex[:8]}",
                run_id=run_id,
                viewports_captured=viewports_captured,
                screenshot_paths=screenshot_paths,
                horizontal_overflow_detected=overflow_map,
                cta_visible=cta_map,
                capture_mode="REAL_LOCAL_BROWSER_SCREENSHOT",
                capture_adapter="RealBrowserCaptureAdapter",
                file_hashes=file_hashes,
                captured_at=captured_at,
            )
        finally:
            # Restore environment variable state
            if env_browsers_path:
                if env_was_set:
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = orig_env_val  # type: ignore
                else:
                    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
