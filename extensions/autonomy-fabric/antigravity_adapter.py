"""Antigravity CLI Adapter (R2).

Provides machine-readable headless execution, conversation ID resumption,
response parsing, and fail-closed state mapping into AOS run states.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any
import subprocess
import json
import time
from extensions.autonomy_fabric.run_registry import RunStatus


class AntigravityStatus(str, Enum):
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    CANCELED = "CANCELED"
    INTERRUPTED = "INTERRUPTED"
    INVALID = "INVALID"
    WAITING = "WAITING"
    RUNNING = "RUNNING"
    UNKNOWN = "UNKNOWN"


ANTIGRAVITY_TO_AOS_STATUS_MAP: Dict[AntigravityStatus, RunStatus] = {
    AntigravityStatus.SUCCESS: RunStatus.COMPLETED,
    AntigravityStatus.ERROR: RunStatus.FAILED,
    AntigravityStatus.CANCELED: RunStatus.CANCELED,
    AntigravityStatus.INTERRUPTED: RunStatus.INTERRUPTED,
    AntigravityStatus.INVALID: RunStatus.FAILED,
    AntigravityStatus.WAITING: RunStatus.WAITING_AGENT,
    AntigravityStatus.RUNNING: RunStatus.RUNNING,
}


@dataclass
class AntigravityResponse:
    conversation_id: str
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
    ) -> AntigravityResponse:
        raise NotImplementedError

    def map_status(self, raw_status: str) -> AntigravityResponse:
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
    ) -> AntigravityResponse:
        cid = conversation_id or f"conv-fake-{len(self.invocations) + 1}"
        self.invocations.append({
            "prompt": prompt,
            "conversation_id": cid,
            "workspace_path": workspace_path,
            "output_format": output_format,
            "timestamp": time.time(),
        })

        if cid in self.canned_responses:
            return self.canned_responses[cid]

        mapped = ANTIGRAVITY_TO_AOS_STATUS_MAP.get(self.default_status, RunStatus.FAILED)
        return AntigravityResponse(
            conversation_id=cid,
            status=self.default_status,
            mapped_aos_status=mapped,
            raw_response=json.dumps({"content": f"Fake response for: {prompt[:30]}"}),
            parsed_json={"content": f"Fake response for: {prompt[:30]}"},
            duration_seconds=0.05,
            turn_count=1,
            usage_metadata={"prompt_tokens": 10, "completion_tokens": 20},
        )


class AntigravityCLIAdapter(BaseAntigravityAdapter):
    """Real CLI adapter calling `antigravity` executable with machine-readable interface."""

    def __init__(self, cli_binary_path: str = "antigravity"):
        self.cli_binary_path = cli_binary_path

    def build_cmd(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        workspace_path: Optional[str] = None,
        output_format: str = "json",
    ) -> List[str]:
        cmd = [self.cli_binary_path, "--output-format", output_format]
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        if workspace_path:
            cmd.extend(["--workspace", workspace_path])
        cmd.extend(["--prompt", prompt])
        return cmd

    def parse_cli_output(self, stdout: str, stderr: str, returncode: int) -> AntigravityResponse:
        start_time = time.time()
        try:
            data = json.loads(stdout) if stdout.strip() else {}
        except json.JSONDecodeError:
            data = {"raw": stdout}

        raw_status_str = data.get("status", "SUCCESS" if returncode == 0 else "ERROR").upper()
        try:
            status_enum = AntigravityStatus(raw_status_str)
        except ValueError:
            status_enum = AntigravityStatus.UNKNOWN

        mapped_aos_status = ANTIGRAVITY_TO_AOS_STATUS_MAP.get(status_enum, RunStatus.FAILED)
        conversation_id = data.get("conversation_id") or data.get("id") or "unknown-conv"

        return AntigravityResponse(
            conversation_id=conversation_id,
            status=status_enum,
            mapped_aos_status=mapped_aos_status,
            raw_response=stdout,
            parsed_json=data,
            duration_seconds=data.get("duration", 0.0),
            turn_count=data.get("turn_count", 1),
            usage_metadata=data.get("usage", {}),
            error_message=stderr if returncode != 0 else data.get("error"),
        )

    def execute_prompt(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        workspace_path: Optional[str] = None,
        output_format: str = "json",
    ) -> AntigravityResponse:
        cmd = self.build_cmd(prompt, conversation_id, workspace_path, output_format)
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            resp = self.parse_cli_output(proc.stdout, proc.stderr, proc.returncode)
            resp.duration_seconds = time.time() - t0
            return resp
        except Exception as ex:
            return AntigravityResponse(
                conversation_id=conversation_id or "unresolved-conv",
                status=AntigravityStatus.ERROR,
                mapped_aos_status=RunStatus.FAILED,
                raw_response="",
                parsed_json={},
                duration_seconds=time.time() - t0,
                error_message=str(ex),
            )
