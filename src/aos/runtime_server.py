"""Loopback Runtime API V1 for AOS.

AOS Direct and the CLI both use this API. The server persists commands before
spawning detached workers and recovers unfinished work after process restarts.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from aos.runtime_contract import (
    CONTRACT_VERSION,
    ContinueProjectCommand,
    ProjectProfile,
    resolve_project,
    resolve_under_authorized_roots,
    utc_now,
    validate_runtime_config,
)
from aos.runtime_store import RuntimeStore, atomic_json, read_json
from aos.process_utils import popen_headless, run_headless, get_headless_creationflags

MAX_BODY_BYTES = 64 * 1024


def load_config(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Runtime config must be a JSON object")
    return validate_runtime_config(value)


def pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        if os.name == "nt":
            proc = run_headless(
                ["tasklist", "/FI", f"PID eq {value}", "/FO", "CSV", "/NH"],
                timeout=10,
            )
            return proc.returncode == 0 and str(value) in (proc.stdout or "")
        os.kill(value, 0)
        return True
    except Exception:
        return False


def _creationflags() -> int:
    return get_headless_creationflags(detached=True)


def _resolve_worker_executable() -> str:
    py = sys.executable
    if os.name == "nt" and py.lower().endswith("pythonw.exe"):
        candidate = Path(py).with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return py


def _build_worker_env(slot_root: Optional[str] = None) -> Dict[str, str]:
    env = dict(os.environ)
    site_dirs: list[str] = []
    if slot_root:
        candidate_site = Path(slot_root) / "site"
        if candidate_site.is_dir():
            site_dirs.append(str(candidate_site.resolve()))
    module_parent = Path(__file__).resolve().parent.parent
    if module_parent.is_dir():
        site_dirs.append(str(module_parent))
    existing = env.get("PYTHONPATH", "")
    all_parts = [p for p in site_dirs if p]
    if existing:
        all_parts.append(existing)
    if all_parts:
        env["PYTHONPATH"] = os.pathsep.join(all_parts)
    return env


class RuntimeEngine:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = validate_runtime_config(config)
        self.runtime_root = Path(self.config["runtime_root"])
        self.store = RuntimeStore(self.runtime_root)
        self.stop_event = threading.Event()
        self.recovery_thread = threading.Thread(target=self._recovery_loop, name="aos-runtime-recovery", daemon=True)
        self.recovery_thread.start()

    def _normalize_project(self, project_id: Optional[str]) -> ProjectProfile:
        profile = resolve_project(self.config, project_id)
        roots = self.config["authorized_roots"]
        descriptor = resolve_under_authorized_roots(profile.descriptor_path, roots)
        workspace = resolve_under_authorized_roots(profile.workspace, roots)
        policy = resolve_under_authorized_roots(profile.routing_policy_path, roots)
        if not descriptor.is_file():
            raise ValueError(f"Project descriptor missing: {descriptor}")
        if not workspace.is_dir():
            raise ValueError(f"Project workspace missing: {workspace}")
        if not policy.is_file():
            raise ValueError(f"Routing policy missing: {policy}")
        return ProjectProfile(
            project_id=profile.project_id,
            descriptor_path=str(descriptor),
            workspace=str(workspace),
            routing_policy_path=str(policy),
            standing_authority=profile.standing_authority,
        )

    def submit_continue(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        profile = self._normalize_project(payload.get("project_id"))
        command = ContinueProjectCommand.from_mapping(payload, project=profile)
        self.store.create_command(command.to_dict())
        self._spawn_worker(command.command_id, recovered=False)
        return {
            "contract_version": CONTRACT_VERSION,
            "accepted": True,
            "command_id": command.command_id,
            "state": "QUEUED",
            "project_id": profile.project_id,
            "run_plan_required": False,
            "production": "NO_GO",
            "ag_backend_enabled": False,
        }

    def _spawn_worker(self, command_id: str, *, recovered: bool) -> Optional[int]:
        state = self.store.read_state(command_id)
        existing = state.get("worker_pid")
        if pid_alive(existing):
            return int(existing)
        command = self.store.read_command(command_id)
        if not command:
            return None
        target_state = "RECOVERING" if recovered else "QUEUED"
        self.store.write_state(command_id, state=target_state, worker_pid=None)
        cmd = [
            _resolve_worker_executable(),
            "-m",
            "aos.runtime_worker",
            "--runtime-root",
            str(self.runtime_root),
            "--command-id",
            command_id,
        ]
        slot_root = self.config.get("runtime_slot_root")
        worker_env = _build_worker_env(str(slot_root) if slot_root else None)
        proc = popen_headless(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            detached=True,
            env=worker_env,
        )
        # The worker can reach RUNNING before Popen returns. Never downgrade a
        # concurrently advanced state back to QUEUED/RECOVERING.
        current = self.store.read_state(command_id)
        updates = {"worker_pid": proc.pid}
        if str(current.get("state")) not in ("RUNNING", "WAITING_FOR_REASONING_PROVIDER", "PROJECT_COMPLETE", "HUMAN_REQUIRED", "FAILED"):
            updates["state"] = target_state
        self.store.write_state(command_id, **updates)
        if recovered:
            self.store.append_event(command_id, "runtime.worker_respawned", {
                "worker_pid": proc.pid,
                "reason": "unfinished_command_recovery",
            })
        return proc.pid

    def _recover_one(self, command_id: str) -> None:
        state = self.store.read_state(command_id)
        current = str(state.get("state") or "")
        if current in ("PROJECT_COMPLETE", "HUMAN_REQUIRED", "FAILED"):
            return
        if current == "WAITING_FOR_REASONING_PROVIDER":
            retry = float(state.get("retry_after_epoch", 0) or 0)
            if retry and time.time() < retry:
                return
        if pid_alive(state.get("worker_pid")):
            return
        self._spawn_worker(command_id, recovered=True)

    def recover_unfinished(self) -> None:
        for command_id in self.store.list_command_ids():
            try:
                self._recover_one(command_id)
            except Exception as exc:
                try:
                    self.store.append_event(command_id, "runtime.recovery_failed", {
                        "error_class": exc.__class__.__name__,
                        "message": str(exc)[:1000],
                    })
                except Exception:
                    pass

    def _recovery_loop(self) -> None:
        while not self.stop_event.is_set():
            self.recover_unfinished()
            self.stop_event.wait(3.0)

    def health(self) -> Dict[str, Any]:
        active = []
        waiting = []
        terminal = []
        active_by_project: Dict[str, List[str]] = {}
        command_ids = self.store.list_command_ids()[-200:]
        latest_summary = None
        for command_id in command_ids:
            state = self.store.read_state(command_id)
            current = str(state.get("state") or "UNKNOWN")
            cmd = self.store.read_command(command_id)
            proj_id = (cmd.get("project") or {}).get("project_id") or self.config["default_project"]
            if current in ("QUEUED", "RUNNING", "RECOVERING"):
                active.append(command_id)
                active_by_project.setdefault(proj_id, []).append(command_id)
            elif current == "WAITING_FOR_REASONING_PROVIDER":
                waiting.append(command_id)
            else:
                terminal.append(command_id)
        if command_ids:
            latest_id = command_ids[-1]
            latest_state = self.store.read_state(latest_id)
            latest_summary = {
                "command_id": latest_id,
                "state": latest_state.get("state"),
                "disposition": latest_state.get("disposition"),
                "completed_batch_count": int(latest_state.get("completed_batch_count", 0) or 0),
                "failure_class": latest_state.get("failure_class"),
                "canonical_source_sha": latest_state.get("canonical_source_sha"),
            }
        return {
            "contract_version": CONTRACT_VERSION,
            "runtime_state": "HEALTHY",
            "pid": os.getpid(),
            "timestamp": utc_now(),
            "active_commands": active,
            "active_commands_by_project": active_by_project,
            "registered_projects": list((self.config.get("projects") or {}).keys()),
            "waiting_commands": waiting,
            "terminal_command_count": len(terminal),
            "latest_command": latest_summary,
            "default_project": self.config["default_project"],
            "runtime_source_sha": self.config.get("candidate_source_sha"),
            "runtime_asset_tree_sha256": self.config.get("runtime_asset_tree_sha256"),
            "runtime_slot_root": self.config.get("runtime_slot_root"),
            "runtime_slot_id": self.config.get("runtime_slot_id"),
            "runtime_launch_nonce": os.environ.get("AOS_RUNTIME_LAUNCH_NONCE"),
            "runtime_supervisor_pid": os.environ.get("AOS_RUNTIME_SUPERVISOR_PID"),
            "production": "NO_GO",
            "ag_backend_enabled": False,
        }

    def shutdown(self) -> None:
        self.stop_event.set()
        self.recovery_thread.join(timeout=3.0)


class RuntimeHandler(BaseHTTPRequestHandler):
    server_version = "AOSRuntimeV1/1.0"

    @property
    def engine(self) -> RuntimeEngine:
        return self.server.engine  # type: ignore[attr-defined]

    @property
    def token(self) -> str:
        return self.server.runtime_token  # type: ignore[attr-defined]

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

    def _authorized(self) -> bool:
        return self.headers.get("X-AOS-Runtime-Token") == self.token

    def do_OPTIONS(self) -> None:
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "CORS_DISABLED"})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/v1/health":
            self._json(HTTPStatus.OK, self.engine.health())
            return
        if not self._authorized():
            self._json(HTTPStatus.FORBIDDEN, {"error": "INVALID_RUNTIME_TOKEN"})
            return
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["v1", "commands"]:
            command_id = parts[2]
            snap = self.engine.store.command_snapshot(command_id)
            if not snap["command"]:
                self._json(HTTPStatus.NOT_FOUND, {"error": "COMMAND_NOT_FOUND"})
                return
            self._json(HTTPStatus.OK, snap)
            return
        if len(parts) == 4 and parts[:2] == ["v1", "commands"] and parts[3] == "events":
            command_id = parts[2]
            qs = parse_qs(parsed.query)
            try:
                after = int((qs.get("after_seq") or ["0"])[0])
            except ValueError:
                after = 0
            events = self.engine.store.read_events(command_id, after_seq=max(0, after))
            self._json(HTTPStatus.OK, {
                "contract_version": CONTRACT_VERSION,
                "command_id": command_id,
                "events": events,
            })
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/v1/commands/continue":
            self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            return
        if not self._authorized():
            self._json(HTTPStatus.FORBIDDEN, {"error": "INVALID_RUNTIME_TOKEN"})
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
                raise ValueError("Request body must be an object")
            result = self.engine.submit_continue(payload)
            self._json(HTTPStatus.ACCEPTED, result)
        except Exception as exc:
            self._json(HTTPStatus.BAD_REQUEST, {
                "error": exc.__class__.__name__,
                "message": str(exc)[:1000],
            })

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def _load_runtime_token(config: Dict[str, Any]) -> str:
    token_path = Path(config["runtime_token_path"]).expanduser().resolve()
    token = token_path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("Runtime token is missing or too short")
    return token


def serve(config_path: Path) -> int:
    config = load_config(config_path)
    env_slot = os.environ.get("AOS_RUNTIME_SLOT_ID")
    env_sha = os.environ.get("AOS_RUNTIME_SOURCE_SHA")
    if env_slot and config.get("runtime_slot_id") != env_slot:
        raise ValueError("Runtime launch slot identity does not match config")
    if env_sha and config.get("candidate_source_sha") != env_sha:
        raise ValueError("Runtime launch source SHA does not match config")
    env_nonce = os.environ.get("AOS_RUNTIME_LAUNCH_NONCE")
    env_supervisor = os.environ.get("AOS_RUNTIME_SUPERVISOR_PID")
    if env_nonce or env_slot or env_sha:
        if not env_nonce or not env_supervisor:
            raise ValueError("Runtime supervisor launch identity is incomplete")
        try:
            if int(env_supervisor) <= 0:
                raise ValueError
        except ValueError:
            raise ValueError("Runtime supervisor PID is invalid")
    engine = RuntimeEngine(config)
    token = _load_runtime_token(config)
    server = ThreadingHTTPServer(("127.0.0.1", int(config["port"])), RuntimeHandler)
    server.daemon_threads = True
    server.engine = engine  # type: ignore[attr-defined]
    server.runtime_token = token  # type: ignore[attr-defined]
    runtime_root = Path(config["runtime_root"])
    atomic_json(runtime_root / "server.pid.json", {
        "pid": os.getpid(),
        "started_at": utc_now(),
        "contract_version": CONTRACT_VERSION,
        "runtime_source_sha": config.get("candidate_source_sha"),
        "runtime_slot_id": config.get("runtime_slot_id"),
        "runtime_launch_nonce": os.environ.get("AOS_RUNTIME_LAUNCH_NONCE"),
        "runtime_supervisor_pid": os.environ.get("AOS_RUNTIME_SUPERVISOR_PID"),
    })
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 130
    finally:
        engine.shutdown()
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AOS Runtime V1 loopback API")
    parser.add_argument("--config", required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return serve(Path(args.config).expanduser().resolve())
    except KeyboardInterrupt:
        return 130
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
