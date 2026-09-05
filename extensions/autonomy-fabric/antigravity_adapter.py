"""Antigravity CLI Adapter (R2 / Final Correction).

Provides official machine-readable headless CLI execution, conversation ID resumption,
stream-json terminal contract enforcement, fail-closed state mapping, and workspace validation.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any
import subprocess
import json
import time
import os
import shutil
from extensions.autonomy_fabric.run_registry import RunStatus


class AntigravityStatus(str, Enum):
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    CANCELED = "CANCELED"
    INTERRUPTED = "INTERRUPTED"
    INVALID = "INVALID"
    WAITING = "WAITING"
    RUNNING = "RUNNING"
    IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"
    UNKNOWN = "UNKNOWN"


ANTIGRAVITY_TO_AOS_STATUS_MAP: Dict[AntigravityStatus, RunStatus] = {
    AntigravityStatus.SUCCESS: RunStatus.COMPLETED,
    AntigravityStatus.ERROR: RunStatus.FAILED,
    AntigravityStatus.CANCELED: RunStatus.CANCELED,
    AntigravityStatus.INTERRUPTED: RunStatus.INTERRUPTED,
    AntigravityStatus.INVALID: RunStatus.FAILED,
    AntigravityStatus.WAITING: RunStatus.WAITING_AGENT,
    AntigravityStatus.RUNNING: RunStatus.RUNNING,
    AntigravityStatus.IDENTITY_UNRESOLVED: RunStatus.FAILED,
    AntigravityStatus.UNKNOWN: RunStatus.FAILED,
}


@dataclass
class AntigravityResponse:
    conversation_id: Optional[str]
    status: AntigravityStatus
    mapped_aos_status: RunStatus
    raw_response: str
    parsed_json: Optional[Dict[str, Any]] = None
    duration_seconds: float = 0.0
    turn_count: int = 1
    usage_metadata: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None


class BaseAntigravityAdapter:
    """Interface for Antigravity adapters."""

    def execute_prompt(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        workspace_path: Optional[str] = None,
        output_format: str = "json",
        continue_conversation: bool = False,
    ) -> AntigravityResponse:
        raise NotImplementedError


class FakeAntigravityAdapter(BaseAntigravityAdapter):
    """Deterministic offline fake adapter for testing."""

    def __init__(self):
        self.invocations: List[Dict[str, Any]] = []
        self.canned_responses: Dict[str, AntigravityResponse] = {}
        self.default_status = AntigravityStatus.SUCCESS

    def set_canned_response(self, conversation_id: str, response: AntigravityResponse):
        self.canned_responses[conversation_id] = response

    def execute_prompt(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        workspace_path: Optional[str] = None,
        output_format: str = "json",
        continue_conversation: bool = False,
    ) -> AntigravityResponse:
        cid = conversation_id or f"conv-fake-{len(self.invocations) + 1}"
        self.invocations.append({
            "prompt": prompt,
            "conversation_id": cid,
            "workspace_path": workspace_path,
            "output_format": output_format,
            "continue_conversation": continue_conversation,
            "timestamp": time.time(),
        })

        if cid in self.canned_responses:
            return self.canned_responses[cid]

        mapped = ANTIGRAVITY_TO_AOS_STATUS_MAP.get(self.default_status, RunStatus.FAILED)
        return AntigravityResponse(
            conversation_id=cid,
            status=self.default_status,
            mapped_aos_status=mapped,
            raw_response=json.dumps({"conversation_id": cid, "status": self.default_status.value, "response": f"Fake response for: {prompt[:30]}"}),
            parsed_json={"conversation_id": cid, "status": self.default_status.value, "response": f"Fake response for: {prompt[:30]}"},
            duration_seconds=0.05,
            turn_count=1,
            usage_metadata={"prompt_tokens": 10, "completion_tokens": 20},
        )


class AntigravityCLIAdapter(BaseAntigravityAdapter):
    """Real CLI adapter calling `antigravity` executable with machine-readable interface."""

    KNOWN_AOS_RUNTIME_PATH = r"C:\Users\mozcelikbas\AppData\Local\AOS\runtime\antigravity-cli\1.1.20\antigravity.exe"

    def __init__(self, cli_binary_path: Optional[str] = None):
        if not cli_binary_path or cli_binary_path == "antigravity":
            if shutil.which("antigravity"):
                self.cli_binary_path = "antigravity"
            elif os.path.exists(self.KNOWN_AOS_RUNTIME_PATH):
                self.cli_binary_path = self.KNOWN_AOS_RUNTIME_PATH
            else:
                self.cli_binary_path = "antigravity"
        else:
            self.cli_binary_path = cli_binary_path

    def build_cmd(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        output_format: str = "json",
        continue_conversation: bool = False,
    ) -> List[str]:
        cmd = [self.cli_binary_path, "--output-format", output_format]
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        if continue_conversation:
            cmd.append("--continue")
        cmd.extend(["--prompt", prompt])
        return cmd

    def parse_cli_json(self, stdout: str, stderr: str, returncode: int) -> AntigravityResponse:
        try:
            data = json.loads(stdout) if stdout.strip() else {}
        except json.JSONDecodeError:
            data = {}

        conversation_id = data.get("conversation_id")
        raw_status_str = data.get("status")

        # Fail closed if conversation_id is missing (returncode=0 or !=0)
        if not conversation_id:
            status_enum = AntigravityStatus.IDENTITY_UNRESOLVED
            mapped_aos_status = RunStatus.FAILED
            return AntigravityResponse(
                conversation_id=None,
                status=status_enum,
                mapped_aos_status=mapped_aos_status,
                raw_response=stdout,
                parsed_json=data,
                duration_seconds=data.get("duration_seconds", 0.0),
                turn_count=data.get("num_turns", 1),
                usage_metadata=data.get("usage", {}),
                error_message=stderr if returncode != 0 else (data.get("error") or "Missing required conversation_id"),
            )

        if raw_status_str:
            try:
                status_enum = AntigravityStatus(raw_status_str.upper())
            except ValueError:
                status_enum = AntigravityStatus.UNKNOWN
        else:
            status_enum = AntigravityStatus.SUCCESS if returncode == 0 else AntigravityStatus.ERROR

        mapped_aos_status = ANTIGRAVITY_TO_AOS_STATUS_MAP.get(status_enum, RunStatus.FAILED)

        return AntigravityResponse(
            conversation_id=conversation_id,
            status=status_enum,
            mapped_aos_status=mapped_aos_status,
            raw_response=stdout,
            parsed_json=data,
            duration_seconds=data.get("duration_seconds", 0.0),
            turn_count=data.get("num_turns", 1),
            usage_metadata=data.get("usage", {}),
            error_message=stderr if returncode != 0 else data.get("error"),
        )

    def parse_cli_stream_json(self, stdout: str, stderr: str, returncode: int) -> AntigravityResponse:
        """Parses line-delimited stream-json events: init, step_update, result.
        
        Requires a valid terminal 'result' event to provide terminal SUCCESS.
        """
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        events = []
        terminal_event = None
        conv_id = None

        for line in lines:
            try:
                evt = json.loads(line)
                events.append(evt)
                evt_type = evt.get("event") or evt.get("type")
                if evt_type == "init":
                    if evt.get("conversation_id"):
                        conv_id = evt.get("conversation_id")
                elif evt_type == "result":
                    terminal_event = evt
                    if evt.get("conversation_id"):
                        conv_id = evt.get("conversation_id")
            except json.JSONDecodeError:
                continue

        # A stream-json execution is terminally successful ONLY when a valid terminal result event exists
        if not terminal_event:
            status_enum = AntigravityStatus.INVALID
            mapped_aos_status = RunStatus.FAILED
            return AntigravityResponse(
                conversation_id=conv_id,
                status=status_enum,
                mapped_aos_status=mapped_aos_status,
                raw_response=stdout,
                parsed_json={"events": events},
                duration_seconds=0.0,
                turn_count=len(events),
                usage_metadata={},
                error_message=stderr if returncode != 0 else "Missing terminal result event in stream-json output",
            )

        raw_status = terminal_event.get("status")
        if raw_status:
            try:
                status_enum = AntigravityStatus(raw_status.upper())
            except ValueError:
                status_enum = AntigravityStatus.UNKNOWN
        else:
            status_enum = AntigravityStatus.UNKNOWN

        if not conv_id:
            status_enum = AntigravityStatus.IDENTITY_UNRESOLVED
            conv_id = None

        mapped_aos_status = ANTIGRAVITY_TO_AOS_STATUS_MAP.get(status_enum, RunStatus.FAILED)

        return AntigravityResponse(
            conversation_id=conv_id,
            status=status_enum,
            mapped_aos_status=mapped_aos_status,
            raw_response=stdout,
            parsed_json=terminal_event,
            duration_seconds=terminal_event.get("duration_seconds", 0.0),
            turn_count=terminal_event.get("num_turns", len(events)),
            usage_metadata=terminal_event.get("usage", {}),
            error_message=stderr if returncode != 0 else terminal_event.get("error"),
        )

    def execute_prompt(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        workspace_path: Optional[str] = None,
        output_format: str = "json",
        continue_conversation: bool = False,
    ) -> AntigravityResponse:
        # Workspace Fail-Closed: If workspace_path is explicitly supplied but does not exist, fail immediately before invoking CLI
        if workspace_path and not os.path.isdir(workspace_path):
            raise ValueError(f"Workspace path '{workspace_path}' does not exist or is not a directory")

        cmd = self.build_cmd(prompt, conversation_id, output_format, continue_conversation)
        t0 = time.time()

        try:
            proc = subprocess.run(
                cmd,
                cwd=workspace_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            if output_format == "stream-json":
                resp = self.parse_cli_stream_json(proc.stdout, proc.stderr, proc.returncode)
            else:
                resp = self.parse_cli_json(proc.stdout, proc.stderr, proc.returncode)
            resp.duration_seconds = time.time() - t0
            return resp
        except Exception as ex:
            if isinstance(ex, ValueError):
                raise
            return AntigravityResponse(
                conversation_id=None,
                status=AntigravityStatus.IDENTITY_UNRESOLVED,
                mapped_aos_status=RunStatus.FAILED,
                raw_response="",
                parsed_json={},
                duration_seconds=time.time() - t0,
                error_message=str(ex),
            )
