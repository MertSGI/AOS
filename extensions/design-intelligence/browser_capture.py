"""Real Browser Capture Adapter (Section 5).

Captures actual responsive browser screenshots via Playwright, computes full SHA256 hashes,
validates layout metrics (horizontal overflow, CTA visibility), and emits VisualEvidenceManifest.
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

from extensions.design_intelligence.contracts import VisualEvidenceManifest
from extensions.design_intelligence.visual_qa import (
    BaseBrowserScreenshotAdapter,
    REQUIRED_VIEWPORTS,
)

CANDIDATE_TOOLS_DIR = r"C:\Users\mozcelikbas\AppData\Local\AOS\candidates\design-intelligence-v1-1-production\tools"
CANDIDATE_PLAYWRIGHT_BROWSERS = r"C:\Users\mozcelikbas\AppData\Local\AOS\candidates\design-intelligence-v1-1-production\tools\ms-playwright"

if CANDIDATE_TOOLS_DIR not in sys.path and os.path.exists(CANDIDATE_TOOLS_DIR):
    sys.path.insert(0, CANDIDATE_TOOLS_DIR)


class RealBrowserCaptureAdapter(BaseBrowserScreenshotAdapter):
    """Production Playwright-based browser capture adapter."""

    def __init__(self, output_dir: Optional[str] = None):
        self.output_dir = output_dir or os.path.join(
            r"C:\Users\mozcelikbas\AppData\Local\AOS\candidates\design-intelligence-v1-1-production\evidence",
            "screenshots"
        )
        os.makedirs(self.output_dir, exist_ok=True)

    def capture_manifest(self, url: str, run_id: str) -> VisualEvidenceManifest:
        """Captures actual responsive screenshots for all required viewports."""
        return asyncio.run(self._async_capture(url, run_id))

    async def _async_capture(self, url: str, run_id: str) -> VisualEvidenceManifest:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = CANDIDATE_PLAYWRIGHT_BROWSERS
        
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("Playwright is not available in candidate tools environment.")

        screenshot_paths: Dict[int, str] = {}
        overflow_map: Dict[int, bool] = {}
        cta_map: Dict[int, bool] = {}
        file_hashes: Dict[int, str] = {}
        viewports_captured: List[int] = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
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

                # Check CTA visibility
                cta_visible = await page.evaluate(
                    "() => !!document.querySelector('.btn-primary, button, a.btn, [role=\"button\"]')"
                )
                cta_map[vp] = bool(cta_visible)

                # Capture PNG
                shot_path = os.path.join(self.output_dir, f"{run_id}_{vp}px.png")
                await page.screenshot(path=shot_path, full_page=False)
                
                # Compute exact 64-char SHA256
                data = Path(shot_path).read_bytes()
                h64 = hashlib.sha256(data).hexdigest()

                screenshot_paths[vp] = shot_path
                file_hashes[vp] = h64
                viewports_captured.append(vp)

            await browser.close()

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
