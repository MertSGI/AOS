"""Client for the local AOS Runtime Contract V1 API."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from aos.runtime_contract import CONTRACT_VERSION


class RuntimeClientError(RuntimeError):
    pass


class RuntimeClient:
    def __init__(self, base_url: str, token_path: Path) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_path = token_path.expanduser().resolve()

    def _token(self) -> str:
        token = self.token_path.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise RuntimeClientError("Runtime token is unavailable")
        return token

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None, *, auth: bool = True) -> Dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "AOS-Runtime-Client/1.0",
        }
        if auth:
            headers["X-AOS-Runtime-Token"] = self._token()
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                value = json.loads(raw) if raw else {}
                if not isinstance(value, dict):
                    raise RuntimeClientError("Runtime API returned non-object JSON")
                return value
        except urllib.error.HTTPError as exc:
            try:
                value = json.loads(exc.read().decode("utf-8"))
            except Exception:
                value = {}
            message = value.get("message") or value.get("error") or f"HTTP {exc.code}"
            raise RuntimeClientError(str(message)) from exc
        except OSError as exc:
            raise RuntimeClientError(str(exc)) from exc

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/v1/health", auth=False)

    def status(self) -> Dict[str, Any]:
        return self._request("GET", "/v1/status")

    def continue_project(
        self,
        *,
        goal: str,
        project_id: Optional[str] = None,
        constraints: Iterable[str] = (),
        red_lines: Iterable[str] = (),
        max_batches_per_cycle: int = 1,
        max_iterations_per_batch: int = 30,
        continuous: bool = True,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "command_type": "continue_project",
            "goal": goal,
            "constraints": list(constraints),
            "red_lines": list(red_lines),
            "max_batches_per_cycle": int(max_batches_per_cycle),
            "max_iterations_per_batch": int(max_iterations_per_batch),
            "continuous": bool(continuous),
            "production": "NO_GO",
            "ag_backend_enabled": False,
        }
        if project_id:
            payload["project_id"] = project_id
        return self._request("POST", "/v1/commands/continue", payload)

    def command(self, command_id: str) -> Dict[str, Any]:
        safe = urllib.parse.quote(command_id, safe="")
        return self._request("GET", f"/v1/commands/{safe}")

    def events(self, command_id: str, *, after_seq: int = 0) -> Dict[str, Any]:
        safe = urllib.parse.quote(command_id, safe="")
        return self._request("GET", f"/v1/commands/{safe}/events?after_seq={int(after_seq)}")

    def current_project_state(self, command_id: Optional[str] = None) -> Dict[str, Any]:
        """Expose current project runtime state for AG and administrative observers."""
        if command_id:
            return self.command(command_id)
        health = self.health()
        status = self.status()
        latest = status.get("latest_command")
        if isinstance(latest, dict) and latest.get("command_id"):
            return self.command(str(latest["command_id"]))
        return {"health": health, "status": status, "command": None, "state": None}

    def pause_safe(self) -> Dict[str, Any]:
        return self._request("POST", "/v1/commands/pause-safe", {})

    def quiesce(self, timeout_seconds: float = 10.0) -> Dict[str, Any]:
        return self._request("POST", "/v1/commands/quiesce", {"timeout_seconds": timeout_seconds})

    def shutdown(self, timeout_seconds: float = 10.0) -> Dict[str, Any]:
        return self._request("POST", "/v1/commands/shutdown", {"timeout_seconds": timeout_seconds})

    def resume(self) -> Dict[str, Any]:
        return self._request("POST", "/v1/commands/resume", {})

    def restart_worker(self, command_id: str) -> Dict[str, Any]:
        return self._request("POST", "/v1/commands/restart-worker", {"command_id": command_id})

    def heartbeat_now(self) -> Dict[str, Any]:
        return self._request("POST", "/v1/commands/heartbeat-now", {})

    def checkpoint_now(self) -> Dict[str, Any]:
        return self._request("POST", "/v1/commands/checkpoint-now", {})

    def publish_relay_now(self) -> Dict[str, Any]:
        return self._request("POST", "/v1/commands/publish-relay-now", {})
