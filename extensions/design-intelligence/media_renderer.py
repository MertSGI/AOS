"""Programmatic Video Renderer Adapter.

Generates real MP4 product demo videos via Playwright frame capture + FFmpeg assembly,
validates stream integrity via real ffprobe inspection, and emits VideoArtifactMetadata.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from pathlib import Path
import os
import sys
import subprocess
import datetime
import hashlib
import uuid
import json
import asyncio
import shutil
import tempfile

from extensions.design_intelligence.contracts import ProductDemoVideoSpec


@dataclass
class VideoArtifactMetadata:
    artifact_id: str
    file_path: str
    size_bytes: int
    duration_seconds: float
    width: int
    height: int
    sha256_hash: str
    renderer_identity: str
    rendered_at: str
    has_valid_video_stream: bool = False


class ProgrammaticVideoRendererAdapter:
    """Production video renderer assembling Playwright captured frames using FFmpeg into real MP4."""

    def __init__(
        self,
        output_dir: Optional[str] = None,
        ffmpeg_path: Optional[str] = None,
        ffprobe_path: Optional[str] = None,
        playwright_browsers_path: Optional[str] = None,
        fps: int = 24,
        width: int = 1280,
        height: int = 720,
    ):
        if fps <= 0 or width <= 0 or height <= 0:
            raise ValueError("Rendering configuration parameters (fps, width, height) must be strictly > 0.")

        self.output_dir = output_dir or os.path.join(tempfile.gettempdir(), "aos_evidence", "videos")
        os.makedirs(self.output_dir, exist_ok=True)
        
        self.ffmpeg_path = ffmpeg_path or self._discover_ffmpeg()
        self.ffprobe_path = ffprobe_path or self._discover_ffprobe(self.ffmpeg_path)
        self.playwright_browsers_path = playwright_browsers_path
        self.fps = fps
        self.width = width
        self.height = height

    def _discover_ffmpeg(self) -> str:
        if env_path := os.environ.get("AOS_FFMPEG_PATH"):
            if os.path.exists(env_path):
                return env_path
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            if exe and os.path.exists(exe):
                return exe
        except ImportError:
            pass
        if sys_ffmpeg := shutil.which("ffmpeg"):
            return sys_ffmpeg
        raise RuntimeError("FFmpeg binary not found.")

    def _discover_ffprobe(self, ffmpeg_exe: str) -> Optional[str]:
        if env_path := os.environ.get("AOS_FFPROBE_PATH"):
            if os.path.exists(env_path):
                return env_path
        if ffmpeg_exe:
            sibling = Path(ffmpeg_exe).parent / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
            if sibling.exists():
                return str(sibling)
        if sys_ffprobe := shutil.which("ffprobe"):
            return sys_ffprobe
        return None

    def render_video(self, html_content: str, spec: ProductDemoVideoSpec) -> VideoArtifactMetadata:
        """Renders an actual .mp4 video artifact from HTML animation/demo frames."""
        return asyncio.run(self._async_render(html_content, spec))

    async def _async_render(self, html_content: str, spec: ProductDemoVideoSpec) -> VideoArtifactMetadata:
        env_browsers_path = self.playwright_browsers_path or os.environ.get("AOS_PLAYWRIGHT_BROWSERS_PATH")
        orig_env_val = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        env_was_set = "PLAYWRIGHT_BROWSERS_PATH" in os.environ

        if env_browsers_path:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = env_browsers_path

        run_id = uuid.uuid4().hex[:8]
        frames_dir = os.path.join(self.output_dir, f"frames_{run_id}")
        os.makedirs(frames_dir, exist_ok=True)
        output_mp4 = os.path.join(self.output_dir, f"video_{run_id}.mp4")

        try:
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise RuntimeError("Playwright is not available for video rendering.")

            total_frames = int(spec.duration_seconds * self.fps)

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_viewport_size({"width": self.width, "height": self.height})

                    if os.path.exists(html_content):
                        file_url = Path(html_content).absolute().as_uri()
                        await page.goto(file_url, wait_until="networkidle", timeout=10000)
                    else:
                        await page.set_content(html_content)

                    for frame_idx in range(total_frames):
                        frame_path = os.path.join(frames_dir, f"frame_{frame_idx:04d}.png")
                        await page.screenshot(path=frame_path)
                        await asyncio.sleep(1 / self.fps)
                finally:
                    await browser.close()

            # Assemble frames into MP4 using full FFmpeg binary
            ffmpeg_cmd = [
                self.ffmpeg_path,
                "-y",
                "-framerate", str(self.fps),
                "-i", os.path.join(frames_dir, "frame_%04d.png"),
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                output_mp4,
            ]

            res = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=60)
            if res.returncode != 0 or not os.path.exists(output_mp4) or os.path.getsize(output_mp4) == 0:
                raise RuntimeError(f"FFmpeg MP4 rendering failed (exit {res.returncode}): {res.stderr}")

            # Probe the FINAL MP4 artifact
            if not self.ffprobe_path:
                raise RuntimeError("ffprobe binary is not available to probe MP4 artifact")

            ffprobe_cmd = [
                self.ffprobe_path,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration:format=duration",
                "-of", "json",
                output_mp4,
            ]
            probe_res = subprocess.run(ffprobe_cmd, capture_output=True, text=True, timeout=30)
            if probe_res.returncode != 0:
                raise RuntimeError(f"ffprobe stream inspection failed on '{output_mp4}': {probe_res.stderr}")

            try:
                probe_data = json.loads(probe_res.stdout)
            except Exception as e:
                raise RuntimeError(f"ffprobe emitted invalid JSON output: {e}")

            streams = probe_data.get("streams", [])
            if not streams:
                raise RuntimeError("ffprobe detected zero video streams in final MP4 artifact")

            v_stream = streams[0]
            probed_w = int(v_stream.get("width", 0))
            probed_h = int(v_stream.get("height", 0))
            
            # Duration may be under stream or format
            dur_str = v_stream.get("duration") or probe_data.get("format", {}).get("duration", "0")
            probed_dur = float(dur_str)

            if probed_w <= 0 or probed_h <= 0 or probed_dur <= 0:
                raise RuntimeError(f"Invalid probed video dimensions or duration: w={probed_w}, h={probed_h}, dur={probed_dur}")

            has_valid_stream = True

            size_bytes = os.path.getsize(output_mp4)
            file_bytes = Path(output_mp4).read_bytes()
            h64 = hashlib.sha256(file_bytes).hexdigest()
            rendered_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

            return VideoArtifactMetadata(
                artifact_id=f"vid-{run_id}",
                file_path=output_mp4,
                size_bytes=size_bytes,
                duration_seconds=probed_dur,
                width=probed_w,
                height=probed_h,
                sha256_hash=h64,
                renderer_identity="ProgrammaticVideoRendererAdapter",
                rendered_at=rendered_at,
                has_valid_video_stream=has_valid_stream,
            )
        finally:
            # Clean frame files and temporary directory in finally block
            if os.path.exists(frames_dir):
                shutil.rmtree(frames_dir, ignore_errors=True)

            # Restore environment
            if env_browsers_path:
                if env_was_set:
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = orig_env_val  # type: ignore
                else:
                    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
