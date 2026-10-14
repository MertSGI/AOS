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

    def __init__(
        self,
        output_dir: Optional[str] = None,
        playwright_browsers_path: Optional[str] = None,
        viewports: Optional[List[int]] = None,
    ):
        self.output_dir = output_dir or os.path.join(tempfile.gettempdir(), "aos_evidence", "screenshots")
        os.makedirs(self.output_dir, exist_ok=True)
        self.playwright_browsers_path = playwright_browsers_path
        self.viewports = viewports

    def capture_manifest(
        self,
        url: str,
        run_id: str,
        *,
        assertions: Optional[List[Dict[str, Any]]] = None,
        expected_route: Optional[str] = None,
        viewports: Optional[List[int]] = None,
    ) -> VisualEvidenceManifest:
        """Captures actual responsive screenshots with optional functional assertions."""
        vps = viewports if viewports is not None else self.viewports
        return asyncio.run(self._async_capture(url, run_id, assertions=assertions, expected_route=expected_route, viewports=vps))

    async def _async_capture(
        self,
        url: str,
        run_id: str,
        *,
        assertions: Optional[List[Dict[str, Any]]] = None,
        expected_route: Optional[str] = None,
        viewports: Optional[List[int]] = None,
    ) -> VisualEvidenceManifest:
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
            assertion_results: List[Dict[str, Any]] = []

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

                    active_viewports = viewports if viewports is not None else REQUIRED_VIEWPORTS
                    for vp in active_viewports:
                        await page.set_viewport_size({"width": vp, "height": 900})

                        target_dest = None
                        if url.startswith("data:text/html") or url.startswith("http") or url.startswith("file:"):
                            target_dest = url
                        elif os.path.exists(url):
                            target_dest = Path(url).absolute().as_uri()

                        from aos.design_render_entry import is_authorized_loopback_url
                        if target_dest and (target_dest.startswith("http:") or target_dest.startswith("https:")):
                            if not is_authorized_loopback_url(target_dest):
                                raise PermissionError(f"UNAUTHORIZED_BROWSER_ORIGIN: Requested URL '{target_dest}' is not an authorized loopback origin")

                        if target_dest:
                            try:
                                await page.goto(target_dest, wait_until="networkidle", timeout=10000)
                            except Exception:
                                await page.goto(target_dest, wait_until="load", timeout=10000)
                        else:
                            await page.set_content(url)

                        # Enforce authorized loopback on final navigated URL
                        from aos.design_render_entry import is_authorized_loopback_url
                        final_nav_url = page.url or ""
                        if final_nav_url.startswith("http:") or final_nav_url.startswith("https:"):
                            if not is_authorized_loopback_url(final_nav_url):
                                raise PermissionError(f"UNAUTHORIZED_BROWSER_ORIGIN: Navigated URL '{final_nav_url}' is not an authorized loopback origin")

                        # Capture real DOM inspection metrics on the page
                        if not dom_metrics:
                            dom_metrics = await page.evaluate(
                                "() => ({ title: document.title, bodyChildCount: document.body ? document.body.children.length : 0, readyState: document.readyState, url: window.location.href, hash: window.location.hash })"
                            )

                        # Evaluate functional assertions once (on first viewport or desktop viewport)
                        if not assertion_results and (vp >= 1024 or vp == active_viewports[0]):
                            from urllib.parse import urlparse
                            parsed_final = urlparse(final_nav_url)
                            final_pathname = parsed_final.path or "/"
                            final_hash = parsed_final.fragment or ""
                            if final_hash and not final_hash.startswith("#"):
                                final_hash = "#" + final_hash

                            # 1. Enforce expected_route if declared
                            if expected_route is not None:
                                exp_route_str = str(expected_route).strip()
                                route_matched = False
                                route_detail = ""
                                if not exp_route_str:
                                    route_matched = False
                                    route_detail = "expected_route declared but is empty"
                                else:
                                    # Compare actual pathname/hash precisely against expected route
                                    # Expected route can be e.g. "/#/appointment/manage", "#/appointment/manage", or "/appointment/manage"
                                    exp_parsed = urlparse(exp_route_str)
                                    exp_path = exp_parsed.path or ""
                                    exp_hash = exp_parsed.fragment or ""
                                    if exp_hash and not exp_hash.startswith("#"):
                                        exp_hash = "#" + exp_hash

                                    # Check exact match combinations:
                                    # a) full path+hash match
                                    actual_path_and_hash = final_pathname + (final_hash if final_hash else "")
                                    if exp_route_str.startswith("#"):
                                        route_matched = (final_hash == exp_route_str)
                                    elif exp_hash:
                                        route_matched = (final_pathname == exp_path and final_hash == exp_hash)
                                    else:
                                        route_matched = (final_pathname == exp_path or final_hash == ("#" + exp_path.lstrip("/")))

                                    if route_matched:
                                        route_detail = f"Current route '{actual_path_and_hash}' exactly matches expected route '{exp_route_str}'"
                                    else:
                                        route_detail = f"Current route '{actual_path_and_hash}' does not match expected route '{exp_route_str}'"

                                assertion_results.append({
                                    "type": "route",
                                    "target": exp_route_str,
                                    "description": f"Verify route is '{exp_route_str}'",
                                    "status": "PASS" if route_matched else "FAIL",
                                    "details": route_detail,
                                })

                            # 2. Evaluate functional assertions
                            if assertions is not None:
                                for assertion in assertions:
                                    if not isinstance(assertion, dict):
                                        assertion_results.append({
                                            "type": "malformed",
                                            "target": "",
                                            "description": "Malformed assertion item",
                                            "status": "FAIL",
                                            "details": "Assertion must be an object/dict",
                                        })
                                        continue

                                    a_type = str(assertion.get("type") or "").strip().lower()
                                    target = str(assertion.get("target") or "").strip()
                                    desc = str(assertion.get("description") or target).strip()
                                    passed = False
                                    detail = ""

                                    # Reject unknown assertion types or empty targets
                                    if a_type not in ("selector_visible", "selector", "text_visible", "text", "route_exact", "route"):
                                        passed = False
                                        detail = f"UNKNOWN_ASSERTION_TYPE: Unsupported assertion type '{a_type}'"
                                    elif not target:
                                        passed = False
                                        detail = "MALFORMED_ASSERTION: target cannot be empty"
                                    else:
                                        try:
                                            if a_type in ("selector_visible", "selector"):
                                                elem = await page.query_selector(target)
                                                if elem and await elem.is_visible():
                                                    box = await elem.bounding_box()
                                                    if box and box["width"] > 0 and box["height"] > 0:
                                                        passed = True
                                                        detail = f"Selector '{target}' is present and visibly rendered ({int(box['width'])}x{int(box['height'])})"
                                                    else:
                                                        passed = False
                                                        detail = f"Selector '{target}' has zero bounding box geometry"
                                                else:
                                                    passed = False
                                                    detail = f"Selector '{target}' not found or not visible"

                                            elif a_type in ("text_visible", "text"):
                                                # Evaluate visible rendered text via document.body.innerText, NOT raw HTML or embedded script tags
                                                body_elem = await page.query_selector("body")
                                                visible_text = await body_elem.inner_text() if body_elem else ""
                                                if target.lower() in visible_text.lower():
                                                    passed = True
                                                    detail = f"Rendered visible text contains '{target}'"
                                                else:
                                                    passed = False
                                                    detail = f"Visible rendered text does not contain '{target}' (ignoring raw HTML/script contents)"

                                            elif a_type in ("route_exact", "route"):
                                                actual_path_and_hash = final_pathname + (final_hash if final_hash else "")
                                                if target.startswith("#"):
                                                    passed = (final_hash == target)
                                                else:
                                                    passed = (final_pathname == target or final_hash == ("#" + target.lstrip("/")))
                                                if passed:
                                                    detail = f"Route exactly matches '{target}'"
                                                else:
                                                    detail = f"Current route '{actual_path_and_hash}' does not match expected '{target}'"
                                        except Exception as a_err:
                                            passed = False
                                            detail = f"Assertion evaluation error: {a_err}"

                                    assertion_results.append({
                                        "type": a_type,
                                        "target": target,
                                        "description": desc,
                                        "status": "PASS" if passed else "FAIL",
                                        "details": detail,
                                    })

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
            has_failed_assertion = any(str(a.get("status", "")).upper() != "PASS" for a in assertion_results if isinstance(a, dict))
            dom_ok = dom_metrics.get("readyState") == "complete"
            if assertions is not None and len(assertions) > 0 and has_failed_assertion:
                dom_status = "FAIL"
            elif dom_ok:
                dom_status = "PASS"
            else:
                dom_status = "DEGRADED"

            setattr(manifest, "console_errors", [c["text"] for c in console_events if c["type"] in ("error", "warning")])
            setattr(manifest, "console_events", console_events)
            setattr(manifest, "page_errors", page_errors)
            setattr(manifest, "dom_inspection", dom_status)
            setattr(manifest, "dom_metrics", dom_metrics)
            setattr(manifest, "assertion_results", assertion_results)
            return manifest
        finally:
            # Restore environment variable state
            if env_browsers_path:
                if env_was_set:
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = orig_env_val  # type: ignore
                else:
                    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
