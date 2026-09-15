"""AOS Direct local control panel.

Loopback-only HTTP surface for the AOS Local Autonomous Host. This is deliberately
independent of Antigravity's agent/model quota. It does not grant authority; submitted
jobs are revalidated by aos.local_host and then fresh-bound by Autonomous Host V1.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from aos.local_host import _atomic_json, load_config, validate_job

MAX_BODY_BYTES = 256 * 1024

_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AOS Direct</title>
<style>
:root { color-scheme: dark; font-family: Inter, Segoe UI, sans-serif; }
body { margin:0; background:#111418; color:#e8edf2; }
main { max-width:1100px; margin:32px auto; padding:0 20px; }
h1 { margin-bottom:4px; }
.sub { color:#9eabb7; margin-top:0; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin:20px 0; }
.card { background:#191e24; border:1px solid #2b333c; border-radius:10px; padding:14px; }
.label { color:#9eabb7; font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
.value { font-size:18px; margin-top:6px; word-break:break-word; }
.ok { color:#74d99f; } .hold { color:#f0b66c; }
textarea { width:100%; min-height:260px; box-sizing:border-box; background:#0d1014; color:#e8edf2; border:1px solid #36414c; border-radius:8px; padding:12px; font:13px Consolas,monospace; }
button { background:#e8edf2; color:#111418; border:0; border-radius:7px; padding:10px 16px; font-weight:700; cursor:pointer; }
button.secondary { background:#27313b; color:#e8edf2; margin-left:8px; }
#message { margin-top:10px; white-space:pre-wrap; }
small { color:#9eabb7; }
</style>
</head>
<body>
<main>
<h1>AOS Direct</h1>
<p class="sub">AG-independent local AOS control surface · loopback only · production NO_GO</p>
<div class="grid">
  <div class="card"><div class="label">Host</div><div id="host" class="value">Loading…</div></div>
  <div class="card"><div class="label">Production</div><div id="production" class="value">NO_GO</div></div>
  <div class="card"><div class="label">AG backend</div><div id="ag" class="value">Disabled</div></div>
  <div class="card"><div class="label">Pending jobs</div><div id="pending" class="value">0</div></div>
</div>
<div class="card">
  <div class="label">Reasoning providers</div>
  <div id="providers" class="value"></div>
  <small>AOS Direct does not consume Antigravity model quota. A reasoning provider is still required for autonomous free-text planning.</small>
</div>
<div class="card" style="margin-top:12px">
  <div class="label">Submit bounded AOS job</div>
  <p><small>This bridge accepts validated non-production job envelopes only. Natural-language Goal Mode will be enabled after canonical execution-base binding and provider readiness are complete.</small></p>
  <textarea id="job" spellcheck="false" placeholder='Paste a validated *.aosjob.json envelope here'></textarea>
  <div style="margin-top:10px">
    <button onclick="submitJob()">Submit to AOS</button>
    <button class="secondary" onclick="refreshStatus()">Refresh</button>
  </div>
  <div id="message"></div>
</div>
</main>
<script>
const TOKEN = __AOS_TOKEN_JSON__;
async function refreshStatus() {
  try {
    const r = await fetch('/api/status', {cache:'no-store'});
    const s = await r.json();
    document.getElementById('host').textContent = s.host_state || 'UNKNOWN';
    document.getElementById('host').className = 'value ' + ((s.host_state||'').includes('HOLD') ? 'hold' : 'ok');
    document.getElementById('production').textContent = s.production || 'NO_GO';
    document.getElementById('ag').textContent = s.ag_backend_enabled ? 'ENABLED' : 'Disabled';
    document.getElementById('pending').textContent = String(s.pending_jobs ?? 0);
    const p = s.providers || {};
    document.getElementById('providers').textContent =
      ['NVIDIA','GEMINI','GROQ','OPENAI','OLLAMA'].map(k => `${k}: ${p[k] ? 'ready' : 'not ready'}`).join(' · ');
  } catch (e) {
    document.getElementById('host').textContent = 'PANEL_ERROR';
    document.getElementById('host').className = 'value hold';
  }
}
async function submitJob() {
  const out = document.getElementById('message');
  let payload;
  try { payload = JSON.parse(document.getElementById('job').value); }
  catch (e) { out.textContent = 'Invalid JSON: ' + e; return; }
  try {
    const r = await fetch('/api/jobs', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-AOS-Panel-Token':TOKEN},
      body:JSON.stringify(payload)
    });
    const data = await r.json();
    out.textContent = JSON.stringify(data, null, 2);
    await refreshStatus();
  } catch (e) { out.textContent = 'Submit failed: ' + e; }
}
refreshStatus();
setInterval(refreshStatus, 5000);
</script>
</body>
</html>
"""


def _read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def _provider_presence() -> Dict[str, bool]:
    result = {
        "NVIDIA": bool(os.environ.get("NVIDIA_API_KEY")),
        "GEMINI": bool(os.environ.get("GEMINI_API_KEY")),
        "GROQ": bool(os.environ.get("GROQ_API_KEY")),
        "OPENAI": bool(os.environ.get("OPENAI_API_KEY")),
        "OLLAMA": False,
    }
    # Keep the panel network-free. Ollama readiness is projected from host status if available.
    return result


def build_status(config: Dict[str, Any]) -> Dict[str, Any]:
    runtime_root = Path(config["runtime_root"]).expanduser().resolve()
    host_status = _read_json(runtime_root / "host-status.json", {})
    pending = len(list((runtime_root / "inbox").glob("*.aosjob.json"))) if (runtime_root / "inbox").is_dir() else 0
    providers = _provider_presence()
    ollama_probe = host_status.get("ollama_probe")
    if isinstance(ollama_probe, dict):
        providers["OLLAMA"] = bool(ollama_probe.get("available"))
    last_job = host_status.get("last_job") if isinstance(host_status.get("last_job"), dict) else None
    return {
        "schema_version": "1.0.0",
        "host_state": host_status.get("state", "UNKNOWN"),
        "pending_jobs": pending,
        "last_job": last_job,
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "providers": providers,
    }


def submit_job(payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    normalized = validate_job(payload, config)
    runtime_root = Path(config["runtime_root"]).expanduser().resolve()
    inbox = runtime_root / "inbox"
    processed = runtime_root / "processed"
    failed = runtime_root / "failed"
    for directory in (inbox, processed, failed):
        directory.mkdir(parents=True, exist_ok=True)
    name = f"{normalized['job_id']}.aosjob.json"
    for directory in (inbox, processed, failed):
        if (directory / name).exists():
            raise ValueError(f"Job id already exists in local host state: {normalized['job_id']}")
    target = inbox / name
    _atomic_json(target, payload)
    return {
        "schema_version": "1.0.0",
        "accepted": True,
        "job_id": normalized["job_id"],
        "state": "QUEUED",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "AOSDirect/1.0"

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, value: str) -> None:
        raw = value.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    @property
    def config(self) -> Dict[str, Any]:
        return self.server.aos_config  # type: ignore[attr-defined]

    @property
    def token(self) -> str:
        return self.server.aos_token  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html = _HTML.replace("__AOS_TOKEN_JSON__", json.dumps(self.token))
            self._html(html)
            return
        if parsed.path == "/api/status":
            self._json(HTTPStatus.OK, build_status(self.config))
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})

    def do_OPTIONS(self) -> None:
        # No CORS support by design.
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "CORS_DISABLED"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/jobs":
            self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            return
        if self.headers.get("X-AOS-Panel-Token") != self.token:
            self._json(HTTPStatus.FORBIDDEN, {"error": "INVALID_PANEL_TOKEN"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON_REQUIRED"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "BODY_SIZE_INVALID"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Job body must be a JSON object")
            result = submit_job(payload, self.config)
            self._json(HTTPStatus.ACCEPTED, result)
        except Exception as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": exc.__class__.__name__, "message": str(exc)[:1000]})

    def log_message(self, fmt: str, *args: Any) -> None:
        # Avoid leaking local request data into console logs.
        return


def ensure_panel_token(panel_config_path: Path) -> Dict[str, Any]:
    data = _read_json(panel_config_path, {})
    token = data.get("panel_token")
    if not isinstance(token, str) or len(token) < 32:
        data["panel_token"] = secrets.token_urlsafe(32)
    data.setdefault("schema_version", "1.0.0")
    data.setdefault("bind_host", "127.0.0.1")
    data.setdefault("port", 8765)
    return data


def serve(local_host_config: Path, panel_config_path: Path) -> int:
    host_config = load_config(local_host_config)
    panel = ensure_panel_token(panel_config_path)
    if panel.get("bind_host") not in ("127.0.0.1", "localhost"):
        raise ValueError("AOS Direct must bind to loopback only")
    port = int(panel.get("port", 8765))
    if not (1024 <= port <= 65535):
        raise ValueError("Invalid panel port")
    _atomic_json(panel_config_path, panel)

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    server.aos_config = host_config  # type: ignore[attr-defined]
    server.aos_token = panel["panel_token"]  # type: ignore[attr-defined]
    print(f"AOS_DIRECT_READY=http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AOS Direct loopback control panel")
    parser.add_argument("--host-config", required=True)
    parser.add_argument("--panel-config", required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return serve(
            Path(args.host_config).expanduser().resolve(),
            Path(args.panel_config).expanduser().resolve(),
        )
    except Exception as exc:
        print(f"AOS_DIRECT_HOLD: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
