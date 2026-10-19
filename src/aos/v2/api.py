"""Authenticated Loopback HTTP API Server for AOS V2.

Implements:
- ThreadingHTTPServer bound exclusively to loopback (127.0.0.1, OS-assigned port 0)
- Private endpoint.json with server identity (PID, birth, nonce, source SHA)
- 32-byte constant-time bearer token check
- Strict input validation: duplicate key rejection, forbidden fields rejection,
  body size bounds (<=64KiB), chunked encoding denial, loopback Host/Origin checks
- Routes:
  - POST /v2/goals (project_id, goal, idempotency_key)
  - GET  /v2/commands/{id}
  - POST /v2/commands/{id}/pause
  - POST /v2/commands/{id}/resume
  - POST /v2/commands/{id}/cancel
  - GET  /v2/health
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from aos.v2.bridge import compute_file_sha256
from aos.v2.engine import Engine, EngineError
from aos.v2.lifecycle import (
    LifecycleConflict,
    OwnerBusyError,
    _no_links,
    _strict_json_loads,
    validate_private_file,
    write_private_file,
)
from aos.v2.registry import RegistryAuthorizationError, authorize, load_registry


logger = logging.getLogger("aos.v2.api")

MAX_BODY_BYTES = 64 * 1024  # 64 KiB
COMMAND_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


class AOSApiRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for AOS V2 Loopback API."""

    server: AOSApiServer

    def log_message(self, format: str, *args: Any) -> None:
        """Sanitize access logs; never output bearer token or prompt bytes."""
        clean_args = []
        for a in args:
            s = str(a)
            if "Bearer" in s or len(s) > 256:
                clean_args.append("[REDACTED]")
            else:
                clean_args.append(s)
        logger.info("%s - - [%s] %s", self.address_string(), self.log_date_time_string(), format % tuple(clean_args))

    def _send_json(self, status_code: int, data: Mapping[str, Any]) -> None:
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_json(self, status_code: int, message: str, code: str = "ERROR") -> None:
        self._send_json(status_code, {"error": message, "code": code})

    def _validate_client_and_headers(self) -> bool:
        """Validate client address, Host, Origin, and Content-Length."""
        # 1. Non-loopback client check
        client_ip = self.client_address[0]
        if client_ip not in ("127.0.0.1", "::1", "localhost"):
            self._send_error_json(403, "Access restricted to loopback interface only", "FORBIDDEN_HOST")
            return False

        # 2. Host header check: exactly one Host header equal to 127.0.0.1:<actual_bound_port>
        host_headers = self.headers.get_all("Host", [])
        if len(host_headers) != 1:
            self._send_error_json(400, "Exactly one Host header required", "INVALID_HOST")
            return False

        assigned_port = self.server.server_address[1]
        expected_host = f"127.0.0.1:{assigned_port}"
        actual_host = host_headers[0].strip()
        if actual_host != expected_host and actual_host != "127.0.0.1":
            self._send_error_json(400, f"Host header must be exactly {expected_host}", "INVALID_HOST")
            return False

        # 3. Origin check: absent or exactly http://127.0.0.1:<actual_bound_port>
        origin_headers = self.headers.get_all("Origin", [])
        if len(origin_headers) > 1:
            self._send_error_json(403, "Duplicate Origin headers forbidden", "CORS_DENIED")
            return False
        if origin_headers:
            origin_val = origin_headers[0].strip()
            expected_origin = f"http://127.0.0.1:{assigned_port}"
            if origin_val != expected_origin:
                self._send_error_json(403, f"Origin must be absent or exactly {expected_origin}", "CORS_DENIED")
                return False

        # 4. Transfer-Encoding denial (no CL/TE ambiguity)
        if self.headers.get("Transfer-Encoding") is not None:
            self._send_error_json(400, "Transfer-Encoding is forbidden", "CHUNKED_DENIED")
            return False

        # 5. Duplicate header checks
        if len(self.headers.get_all("Content-Length", [])) > 1:
            self._send_error_json(400, "Duplicate Content-Length headers forbidden", "INVALID_LENGTH")
            return False
        if len(self.headers.get_all("Authorization", [])) > 1:
            self._send_error_json(400, "Duplicate Authorization headers forbidden", "INVALID_AUTH")
            return False

        return True

    def _validate_auth(self) -> bool:
        """Validate Bearer token using constant-time comparison."""
        auth_header = self.headers.get("Authorization", "").strip()
        expected = f"Bearer {self.server.bearer_token}"
        if not hmac.compare_digest(auth_header, expected):
            self._send_error_json(401, "Invalid or missing Bearer token", "UNAUTHORIZED")
            return False
        return True

    def _read_json_body(self) -> dict[str, Any] | None:
        """Read and validate bounded JSON body with duplicate key checking."""
        cl_header = self.headers.get("Content-Length")
        if cl_header is None:
            self._send_error_json(411, "Missing Content-Length header", "LENGTH_REQUIRED")
            return None

        try:
            cl = int(cl_header)
        except ValueError:
            self._send_error_json(400, "Invalid Content-Length header", "INVALID_LENGTH")
            return None

        if cl < 0 or cl > MAX_BODY_BYTES:
            self._send_error_json(413, f"Body size exceeds maximum limit ({MAX_BODY_BYTES} bytes)", "BODY_TOO_LARGE")
            return None

        content_type = self.headers.get("Content-Type", "")
        if content_type and not content_type.startswith("application/json"):
            self._send_error_json(415, "Content-Type must be application/json", "UNSUPPORTED_MEDIA_TYPE")
            return None

        body_bytes = self.rfile.read(cl)
        if len(body_bytes) != cl:
            self._send_error_json(400, "Incomplete request body read", "INCOMPLETE_BODY")
            return None

        if not body_bytes:
            self._send_error_json(400, "Empty request body", "EMPTY_BODY")
            return None

        try:
            parsed = _strict_json_loads(body_bytes, max_depth=8)
        except ValueError as e:
            self._send_error_json(400, f"Malformed JSON: {e}", "INVALID_JSON")
            return None

        if not isinstance(parsed, dict):
            self._send_error_json(400, "Request JSON root must be an object", "INVALID_ROOT")
            return None

        return parsed

    def do_GET(self) -> None:
        if not self._validate_client_and_headers():
            return

        path = self.path.split("?")[0].rstrip("/")

        if path == "/v2/health":
            self._handle_health()
            return

        if not self._validate_auth():
            return

        # GET /v2/commands/{id}
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "v2" and parts[1] == "commands":
            cmd_id = parts[2]
            self._handle_get_command(cmd_id)
            return

        self._send_error_json(404, f"Route not found: {path}", "NOT_FOUND")

    def do_POST(self) -> None:
        if not self._validate_client_and_headers():
            return

        if not self._validate_auth():
            return

        path = self.path.split("?")[0].rstrip("/")

        if path == "/v2/goals":
            self._handle_post_goal()
            return

        parts = path.strip("/").split("/")
        # POST /v2/commands/{id}/(pause|resume|cancel)
        if len(parts) == 4 and parts[0] == "v2" and parts[1] == "commands":
            cmd_id = parts[2]
            action = parts[3]
            if action in ("pause", "resume", "cancel"):
                self._handle_command_action(cmd_id, action)
                return

        self._send_error_json(404, f"Route not found: {path}", "NOT_FOUND")

    def _handle_health(self) -> None:
        """GET /v2/health - Public loopback health check."""
        meta = self.server.engine.lifecycle.get_engine_meta()
        self._send_json(200, {
            "status": "OK",
            "pid": os.getpid(),
            "owner_epoch": meta["owner_epoch"] if meta else 1,
            "nonce": self.server.server_nonce,
            "alive": True,
        })

    def _handle_get_command(self, cmd_id: str) -> None:
        """GET /v2/commands/{id} - Query command status."""
        if not COMMAND_ID_RE.match(cmd_id):
            self._send_error_json(400, "Invalid command ID format", "INVALID_COMMAND_ID")
            return

        try:
            snapshot = self.server.engine.lifecycle.snapshot(cmd_id)
            self._send_json(200, snapshot)
        except (KeyError, LifecycleConflict):
            self._send_error_json(404, f"Command not found: {cmd_id}", "COMMAND_NOT_FOUND")

    def _handle_post_goal(self) -> None:
        """POST /v2/goals - Submit a new goal or query existing idempotent goal."""
        body = self._read_json_body()
        if body is None:
            return

        # Contract: exactly project_id, goal, idempotency_key. Reject forbidden/extra fields.
        allowed_keys = {"project_id", "goal", "idempotency_key"}
        extra_keys = set(body.keys()) - allowed_keys
        if extra_keys:
            self._send_error_json(
                400,
                f"Forbidden or unrecognized fields in goal request: {sorted(extra_keys)}",
                "FORBIDDEN_FIELDS",
            )
            return

        missing_keys = allowed_keys - set(body.keys())
        if missing_keys:
            self._send_error_json(
                400,
                f"Missing required fields: {sorted(missing_keys)}",
                "MISSING_FIELDS",
            )
            return

        project_id = body["project_id"]
        goal = body["goal"]
        idempotency_key = body["idempotency_key"]

        if not isinstance(project_id, str) or not project_id.strip():
            self._send_error_json(400, "project_id must be a non-empty string", "INVALID_PROJECT_ID")
            return
        if not isinstance(goal, str) or not goal.strip():
            self._send_error_json(400, "goal must be a non-empty string", "INVALID_GOAL")
            return
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            self._send_error_json(400, "idempotency_key must be a non-empty string", "INVALID_IDEMPOTENCY_KEY")
            return

        # Check existing command for this idempotency key scoped to project
        existing_cmd = self.server.engine.lifecycle.get_command_by_idempotency_key(idempotency_key, project_id=project_id)
        if existing_cmd:
            # Verify immutable inputs match
            if existing_cmd["project_id"] != project_id or existing_cmd["goal"] != goal:
                self._send_error_json(
                    409,
                    f"Idempotency conflict: key '{idempotency_key}' already exists with different project_id or goal",
                    "IDEMPOTENCY_CONFLICT",
                )
                return
            # Idempotent replay: return existing snapshot
            snap = self.server.engine.lifecycle.snapshot(existing_cmd["id"])
            self._send_json(200, snap)
            return

        # Preflight project authorization
        try:
            self.server.engine.snapshot = load_registry(self.server.engine.registry_path)
            proj = authorize(self.server.engine.snapshot, project_id)
        except RegistryAuthorizationError as e:
            self._send_error_json(403, f"Project authorization denied: {e}", "AUTHORIZATION_DENIED")
            return

        # Valid authenticated new goal starts explicitly in PAUSED_SAFE or QUEUED state
        command_id = f"cmd_{secrets.token_hex(16)}"
        try:
            from aos.v2.lifecycle import TrustedGrant
            grant = TrustedGrant(
                project_id=proj.project_id,
                authority_id=proj.authority_id,
                workspace=str(proj.workspace),
                base_sha=proj.base_sha,
                write_files=proj.write_files,
            )
            criteria_ids = tuple(c.id for c in proj.criteria) if proj.criteria else ("goal_admission_criterion",)
            cid = self.server.engine.lifecycle.admit(
                command_id=command_id,
                request_key=idempotency_key,
                goal=goal,
                criteria=criteria_ids,
                grant=grant,
                contract_json=json.dumps({"contract": "AOS-V2-SEAM-1", "budget_max_attempts": 4}),
                owner_epoch=self.server.engine.owner_epoch if hasattr(self.server.engine, "owner_epoch") else 1,
            )
            # First startup state is PAUSED_SAFE as per specification
            snap = self.server.engine.lifecycle.snapshot(cid)
            self.server.engine.lifecycle.transition(cid, snap["revision"], "PAUSED_SAFE", reason="initial_startup_paused_safe")
            updated_snap = self.server.engine.lifecycle.snapshot(cid)
            self._send_json(202, updated_snap)
        except (LifecycleConflict, ValueError) as e:
            self._send_error_json(400, f"Failed to admit goal: {e}", "ADMISSION_FAILED")

    def _handle_command_action(self, cmd_id: str, action: str) -> None:
        """POST /v2/commands/{id}/(pause|resume|cancel)"""
        if not COMMAND_ID_RE.match(cmd_id):
            self._send_error_json(400, "Invalid command ID format", "INVALID_COMMAND_ID")
            return

        body = self._read_json_body()
        if body is None:
            return
        if body != {}:
            self._send_error_json(400, "Control action body must be exactly {}", "INVALID_ACTION_BODY")
            return

        try:
            snap = self.server.engine.lifecycle.snapshot(cmd_id)
        except (KeyError, LifecycleConflict):
            self._send_error_json(404, f"Command not found: {cmd_id}", "COMMAND_NOT_FOUND")
            return

        cur_state = snap["state"]
        rev = snap["revision"]

        try:
            if action == "pause":
                if cur_state in ("PROJECT_COMPLETE", "FAILED", "CANCELLED", "SUPERSEDED"):
                    self._send_error_json(409, f"Cannot pause terminal command in state {cur_state}", "INVALID_STATE_TRANSITION")
                    return
                if cur_state != "PAUSED_SAFE":
                    self.server.engine.lifecycle.transition(cmd_id, rev, "PAUSED_SAFE", reason="user_pause_request")
            elif action == "resume":
                if cur_state != "PAUSED_SAFE":
                    self._send_error_json(409, f"Cannot resume command in state {cur_state}; must be PAUSED_SAFE", "INVALID_STATE_TRANSITION")
                    return
                self.server.engine.lifecycle.transition(cmd_id, rev, "QUEUED", reason="user_resume_request")
            elif action == "cancel":
                if cur_state in ("PROJECT_COMPLETE", "FAILED", "CANCELLED", "SUPERSEDED"):
                    self._send_error_json(409, f"Command is already terminal in state {cur_state}", "ALREADY_TERMINAL")
                    return
                # Terminal finalize with CANCELLED
                self.server.engine.lifecycle.finalize(
                    cmd_id,
                    rev,
                    "CANCELLED",
                    criterion_receipts={},
                )

            updated = self.server.engine.lifecycle.snapshot(cmd_id)
            self._send_json(200, updated)
        except (LifecycleConflict, ValueError) as e:
            self._send_error_json(409, f"State transition failed: {e}", "TRANSITION_FAILED")


class AOSApiServer(ThreadingHTTPServer):
    """Threading HTTP Server with bound loopback, private endpoint, and bearer token."""

    def __init__(
        self,
        home: Path,
        registry_path: Path,
        *,
        bind_host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        if bind_host != "127.0.0.1":
            raise ValueError(f"bind_host must be 127.0.0.1, got {bind_host}")
        if not Path(home).is_absolute():
            raise ValueError(f"home path must be a raw absolute path, got {home}")
        if not Path(registry_path).is_absolute():
            raise ValueError(f"registry_path must be a raw absolute path, got {registry_path}")

        self.home = _no_links(Path(home))
        self.registry_path = _no_links(Path(registry_path))
        self.server_nonce = secrets.token_hex(16)
        self.birth_utc = datetime.now(timezone.utc).isoformat()
        self.bearer_token = secrets.token_hex(32)

        # Initialize durable engine
        self.engine = Engine(self.home, self.registry_path)

        # Bind server to 127.0.0.1
        super().__init__((bind_host, port), AOSApiRequestHandler)

        assigned_port = self.server_address[1]

        # Compute source SHA
        source_file = Path(__file__).resolve()
        source_sha = compute_file_sha256(source_file)

        # Write private token.secret
        token_file = self.home / "token.secret"
        if token_file.exists():
            validate_private_file(token_file)
        write_private_file(token_file, f"{self.bearer_token}\n".encode("utf-8"))

        # Write private endpoint.json
        endpoint_file = self.home / "endpoint.json"
        if endpoint_file.exists():
            validate_private_file(endpoint_file)
        endpoint_data = {
            "schema_version": 1,
            "host": bind_host,
            "port": assigned_port,
            "pid": os.getpid(),
            "birth_utc": self.birth_utc,
            "nonce": self.server_nonce,
            "source_sha": source_sha,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        write_private_file(endpoint_file, json.dumps(endpoint_data, indent=2).encode("utf-8"))
        logger.info(f"AOS V2 API Server listening on http://{bind_host}:{assigned_port}")

    def shutdown_and_close(self) -> None:
        """Safely close server and engine."""
        try:
            self.shutdown()
        finally:
            self.server_close()
            self.engine.close()
