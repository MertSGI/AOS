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

            console_events: List[Dict[str, Any]] = []
            page_errors: List[str] = []
            dom_metrics: Dict[str, Any] = {}

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()

                    def handle_console(msg):
                        console_events.append({"type": msg.type, "text": msg.text, "location": msg.location})

                    def handle_pageerror(err):
                        page_errors.append(str(err))

                    page.on("console", handle_console)
                    page.on("pageerror", handle_pageerror)

                    for vp in REQUIRED_VIEWPORTS:
                        await page.set_viewport_size({"width": vp, "height": 900})

                        target_dest = None
                        if url.startswith("data:text/html") or url.startswith("http") or url.startswith("file:"):
                            target_dest = url
                        elif os.path.exists(url):
                            target_dest = Path(url).absolute().as_uri()

                        if target_dest:
                            try:
                                await page.goto(target_dest, wait_until="networkidle", timeout=10000)
                            except Exception:
                                await page.goto(target_dest, wait_until="load", timeout=10000)
                        else:
                            await page.set_content(url)

                        # Capture real DOM inspection metrics on the page
                        if not dom_metrics:
                            dom_metrics = await page.evaluate(
                                "() => ({ title: document.title, bodyChildCount: document.body ? document.body.children.length : 0, readyState: document.readyState })"
                            )

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

            manifest = VisualEvidenceManifest(
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
            # Attach measured layout & console evidence
            setattr(manifest, "console_errors", [c["text"] for c in console_events if c["type"] in ("error", "warning")])
            setattr(manifest, "console_events", console_events)
            setattr(manifest, "page_errors", page_errors)
            setattr(manifest, "dom_inspection", "PASS" if dom_metrics.get("readyState") == "complete" else "DEGRADED")
            setattr(manifest, "dom_metrics", dom_metrics)
            return manifest
        finally:
            # Restore environment variable state
            if env_browsers_path:
                if env_was_set:
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = orig_env_val  # type: ignore
                else:
                    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
