"""Antigravity WorkerAdapter implementation for AOS-3."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from aos.validate import validate_document
from aos.workers.base import WorkerAdapter, WorkerExecutionResult

ADAPTER_CONTRACT_VERSION = "0.2.1"
SENSITIVE_ENV_VARS = {"OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"}
SUPPORTED_OUTPUT_FORMATS = {"json", "stream-json"}
NATIVE_FILE_EDIT_TOOLS = {"write_file", "write_to_file", "edit_file", "replace_file_content", "multi_replace_file_content"}


def get_local_capability_store_path(adapter_name: str = "antigravity") -> Path:
    """Get machine-local OS-appropriate path for capability attestation storage."""
    custom_dir = os.environ.get("AOS_CAPABILITY_STORE_DIR")
    if custom_dir:
        return Path(custom_dir) / f"{adapter_name}.json"

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            base_dir = Path(local_app_data) / "AOS" / "capabilities"
        else:
            base_dir = Path.home() / ".aos" / "capabilities"
    else:
        base_dir = Path.home() / ".aos" / "capabilities"

    return base_dir / f"{adapter_name}.json"


def compute_file_sha256(path: str | Path) -> str:
    """Compute SHA-256 hash of a local file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def get_reported_cli_version(
    cli_command: str = "agy",
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> Optional[str]:
    """Query CLI reported version."""
    try:
        if runner:
            res = runner([cli_command, "--version"])
        else:
            res = subprocess.run([cli_command, "--version"], capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            return (res.stdout or "").strip()
        return None
    except Exception:
        return None


def resolve_executable_identity(
    cli_command: str = "agy",
    version_runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
    which_resolver: Optional[Callable[[str], Optional[str]]] = None,
    hash_computer: Optional[Callable[[str | Path], str]] = None,
    custom_version: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    """Resolve full runtime identity of the CLI executable."""
    resolver = which_resolver or shutil.which
    exe_path = resolver(cli_command) or (cli_command if os.path.isfile(cli_command) else None)
    if not exe_path or not os.path.isfile(exe_path):
        return None

    try:
        hasher = hash_computer or compute_file_sha256
        sha256 = hasher(exe_path)
    except Exception:
        return None

    if custom_version is not None:
        cli_version = custom_version
    else:
        cli_version = get_reported_cli_version(cli_command, runner=version_runner)

    if not cli_version:
        return None

    return {
        "path": os.path.abspath(exe_path),
        "filename": os.path.basename(exe_path),
        "sha256": sha256,
        "version": cli_version,
    }


def resolve_capability_status(
    cli_command: str = "agy",
    store_path: Optional[Path] = None,
    version_runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
    identity: Optional[Dict[str, str]] = None,
) -> str:
    """Dynamically resolve machine-local capability status for the Antigravity CLI."""
    target_store = store_path or get_local_capability_store_path("antigravity")
    if not target_store.is_file():
        return "UNPROVEN"

    current_identity = identity or resolve_executable_identity(cli_command, version_runner=version_runner)
    if not current_identity:
        return "UNPROVEN"

    try:
        with open(target_store, "r", encoding="utf-8") as f:
            attestation = json.load(f)

        val = validate_document("worker_capability_attestation", attestation)
        if not val.is_valid:
            return "UNPROVEN"

        if (
            attestation.get("worker_adapter") == "antigravity"
            and attestation.get("adapter_contract_version") == ADAPTER_CONTRACT_VERSION
            and attestation.get("executable_sha256") == current_identity["sha256"]
            and attestation.get("reported_cli_version") == current_identity["version"]
            and attestation.get("capability_status") == "PROVEN"
        ):
            return "PROVEN"
        return "UNPROVEN"
    except Exception:
        return "UNPROVEN"


def build_antigravity_argv(
    executable_path: str,
    workspace_path: str,
    prompt: str,
    timeout_seconds: int = 180,
    output_format: str = "json",
) -> List[str]:
    """Construct unambiguous, headless Antigravity CLI argv matching contract v0.2.1."""
    if output_format not in SUPPORTED_OUTPUT_FORMATS:
        raise ValueError(
            f"Unsupported output_format: '{output_format}'. Supported formats: {sorted(list(SUPPORTED_OUTPUT_FORMATS))}"
        )

    return [
        executable_path,
        "--mode=accept-edits",
        "--add-dir",
        workspace_path,
        f"--output-format={output_format}",
        f"--print-timeout={timeout_seconds}s",
        "-p",
        prompt,
    ]


def parse_antigravity_json_output(stdout: str) -> Dict[str, Any]:
    """Parse top-level structured Antigravity JSON output and extract sanitized summary fields."""
    sanitized: Dict[str, Any] = {
        "status": None,
        "error_present": False,
        "exit_code": None,
        "timed_out": False,
    }
    if not stdout or not stdout.strip():
        return sanitized

    try:
        parsed = json.loads(stdout.strip())
        if isinstance(parsed, dict):
            if "status" in parsed:
                sanitized["status"] = str(parsed["status"])
            if "error" in parsed or "errors" in parsed or parsed.get("status") in ("ERROR", "FAIL", "HOLD"):
                sanitized["error_present"] = True
            if "exit_code" in parsed and isinstance(parsed["exit_code"], int):
                sanitized["exit_code"] = parsed["exit_code"]
            if "timed_out" in parsed and isinstance(parsed["timed_out"], bool):
                sanitized["timed_out"] = parsed["timed_out"]
    except Exception:
        # If output is not valid top-level JSON, leave sanitized defaults
        pass
    return sanitized


def parse_antigravity_stream_output(stdout: str, workspace_path: Optional[str] = None) -> Dict[str, Any]:
    """Parse Antigravity stream-json (NDJSON) output with fail-closed validation rules.

    Extracts sanitized behavioral evidence for capability diagnosis without persisting
    raw parameters, raw outputs, or full agent transcripts.
    """
    result: Dict[str, Any] = {
        "is_valid_stream": False,
        "terminal_status": None,
        "permission_mode": None,
        "reported_cwd_matches_workspace": None,
        "write_tool_available": False,
        "tool_call_count": 0,
        "tool_calls": [],
        "agent_response_observed": False,
        "permission_soft_denial_observed": False,
        "terminal_error_present": False,
        "parser_errors": [],
    }

    if not stdout or not stdout.strip():
        result["parser_errors"].append("Stream output is empty")
        return result

    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        result["parser_errors"].append("Stream output contains no non-empty lines")
        return result

    init_count = 0
    terminal_result_count = 0

    normalized_expected_ws = os.path.abspath(workspace_path).lower() if workspace_path else None

    for idx, line in enumerate(lines):
        try:
            event = json.loads(line)
        except Exception as e:
            result["parser_errors"].append(f"Malformed JSON on stream line {idx + 1}: {e}")
            return result

        if not isinstance(event, dict):
            result["parser_errors"].append(f"Stream event on line {idx + 1} is not a JSON object")
            return result

        event_type = event.get("type")

        # 1. Init event
        if event_type == "init":
            init_count += 1
            if "permission_mode" in event:
                result["permission_mode"] = str(event["permission_mode"])
            elif "permissionMode" in event:
                result["permission_mode"] = str(event["permissionMode"])
            elif "mode" in event:
                result["permission_mode"] = str(event["mode"])

            # Check reported cwd if available
            reported_cwd = event.get("cwd") or event.get("workspace")
            if reported_cwd and normalized_expected_ws:
                normalized_reported = os.path.abspath(str(reported_cwd)).lower()
                result["reported_cwd_matches_workspace"] = (normalized_reported == normalized_expected_ws)

            # Check available tools in init
            available_tools = event.get("tools") or event.get("available_tools") or event.get("availableTools") or []
            if isinstance(available_tools, list):
                for t in available_tools:
                    t_name = t.get("name") if isinstance(t, dict) else str(t)
                    if t_name in NATIVE_FILE_EDIT_TOOLS or "write" in str(t_name).lower():
                        result["write_tool_available"] = True

        # 2. Step update / Tool / Message events
        elif event_type in ("step_update", "tool_call", "tool_result", "message", "action"):
            # Check for agent response text / message
            if event_type == "message" or "message" in event or "content" in event or "response" in event:
                result["agent_response_observed"] = True

            # Check for tool call records
            tool_data = event.get("tool_call") or event.get("tool") or (event if event_type in ("tool_call", "tool_result") else None)
            if isinstance(tool_data, dict):
                raw_tool_name = tool_data.get("name") or tool_data.get("tool_name") or tool_data.get("tool") or "unknown_tool"
                tool_state = str(tool_data.get("state") or tool_data.get("status") or ("completed" if event_type == "tool_result" else "called"))
                # Check error / denial status
                err_present = False
                err_type = None
                if tool_data.get("error") or tool_data.get("errors") or tool_state in ("error", "failed", "denied"):
                    err_present = True
                    err_val = str(tool_data.get("error") or tool_data.get("error_type") or tool_state)
                    if "permission" in err_val.lower() or "denied" in err_val.lower() or "ask" in err_val.lower():
                        result["permission_soft_denial_observed"] = True
                        err_type = "PERMISSION_DENIED"
                    else:
                        err_type = "TOOL_ERROR"

                sanitized_call = {
                    "tool_name": str(raw_tool_name),
                    "state": tool_state,
                    "error_present": err_present,
                    "error_type": err_type,
                }
                result["tool_calls"].append(sanitized_call)
                result["tool_call_count"] += 1

                if str(raw_tool_name) in NATIVE_FILE_EDIT_TOOLS or "write" in str(raw_tool_name).lower():
                    result["write_tool_available"] = True

            # Soft denial indications in step_update
            if event.get("soft_denial") or event.get("permission_denied"):
                result["permission_soft_denial_observed"] = True

        # 3. Terminal result event
        elif event_type in ("result", "terminal", "session_end", "completed"):
            terminal_result_count += 1
            status_val = str(event.get("status") or event.get("result") or "UNKNOWN")
            result["terminal_status"] = status_val
            if status_val not in ("SUCCESS", "PASS", "COMPLETED") or event.get("error") or event.get("errors"):
                result["terminal_error_present"] = True
                if "permission" in str(event.get("error") or "").lower():
                    result["permission_soft_denial_observed"] = True

        # Check soft-denial hints in any event
        if "permission" in str(event).lower() and ("denied" in str(event).lower() or "prompt" in str(event).lower() or "approval" in str(event).lower()):
            result["permission_soft_denial_observed"] = True

    # Validate fail-closed stream rules
    if init_count != 1:
        result["parser_errors"].append(f"Expected exactly 1 init event, found {init_count}")
    if terminal_result_count != 1:
        result["parser_errors"].append(f"Expected exactly 1 terminal result event, found {terminal_result_count}")
    if result["terminal_status"] != "SUCCESS":
        result["parser_errors"].append(f"Terminal status is not SUCCESS: got '{result['terminal_status']}'")

    if not result["parser_errors"]:
        result["is_valid_stream"] = True

    return result


class AntigravityWorkerAdapter(WorkerAdapter):
    """WorkerAdapter implementation for Google Antigravity (agy CLI)."""

    def __init__(
        self,
        cli_command: str = "agy",
        runner: Optional[Callable[[List[str], str, int, Dict[str, str]], subprocess.CompletedProcess]] = None,
        capability_status_override: Optional[str] = None,
        store_path: Optional[Path] = None,
        version_runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
        injected_identity: Optional[Dict[str, str]] = None,
    ):
        self.cli_command = cli_command
        self.runner = runner or self._default_runner
        self.store_path = store_path
        self.version_runner = version_runner
        self.injected_identity = injected_identity
        self.pinned_identity: Optional[Dict[str, str]] = None

        if capability_status_override is not None:
            if capability_status_override == "TEST_DOUBLE":
                self.capability_status = "TEST_DOUBLE"
            else:
                raise ValueError(
                    f"Invalid capability_status_override: '{capability_status_override}'. "
                    "Only 'TEST_DOUBLE' is permitted for injected offline/test execution."
                )
        else:
            resolved_id = self.injected_identity or resolve_executable_identity(
                self.cli_command, version_runner=self.version_runner
            )
            self.capability_status = resolve_capability_status(
                self.cli_command,
                store_path=self.store_path,
                version_runner=self.version_runner,
                identity=resolved_id,
            )
            if self.capability_status == "PROVEN" and resolved_id:
                self.pinned_identity = dict(resolved_id)

    def _default_runner(self, cmd: List[str], cwd: str, timeout: int, env: Dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)

    def revalidate_runtime_identity(self) -> bool:
        """Re-resolve runtime executable identity and verify match against pinned attestation identity."""
        if self.capability_status != "PROVEN" or not self.pinned_identity:
            return False

        current_id = self.injected_identity or resolve_executable_identity(
            self.cli_command, version_runner=self.version_runner
        )
        if not current_id:
            return False

        # Verify all identity fields match pinned identity
        if (
            current_id.get("path") != self.pinned_identity.get("path")
            or current_id.get("sha256") != self.pinned_identity.get("sha256")
            or current_id.get("version") != self.pinned_identity.get("version")
        ):
            return False

        # Re-verify store attestation still validates and matches contract
        current_status = resolve_capability_status(
            self.cli_command,
            store_path=self.store_path,
            version_runner=self.version_runner,
            identity=current_id,
        )
        return current_status == "PROVEN"

    def execute(
        self,
        task: Dict[str, Any],
        workspace_path: str,
        allowed_scope: Dict[str, Any],
        base_sha: str,
        timeout_seconds: int = 3600,
    ) -> WorkerExecutionResult:
        """Execute task inside isolated workspace using agy CLI with scrubbed environment."""
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        task_id = task.get("task_id", "UNKNOWN_TASK")
        title = task.get("title", "")
        desc = task.get("description", "")

        # Strict identity and capability revalidation prior to real subprocess execution
        if self.capability_status == "TEST_DOUBLE":
            pass
        elif self.capability_status == "PROVEN":
            if not self.revalidate_runtime_identity():
                self.capability_status = "UNPROVEN"
                finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                return WorkerExecutionResult(
                    worker_identity=f"antigravity-cli ({self.capability_status})",
                    workspace_path=workspace_path,
                    exit_code=1,
                    timed_out=False,
                    stdout_summary="",
                    stderr_summary="Executable runtime identity revalidation failed before execution",
                    mutation_attempted=False,
                    started_at=started_at,
                    finished_at=finished_at,
                )
        else:
            # UNPROVEN or other
            finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            return WorkerExecutionResult(
                worker_identity=f"antigravity-cli ({self.capability_status})",
                workspace_path=workspace_path,
                exit_code=1,
                timed_out=False,
                stdout_summary="",
                stderr_summary="Worker capability is UNPROVEN. Execution prohibited.",
                mutation_attempted=False,
                started_at=started_at,
                finished_at=finished_at,
            )

        allowed_paths = allowed_scope.get("paths", [])
        forbidden_paths = allowed_scope.get("forbidden_paths", [])

        # Build secret-safe instruction prompt with explicit safety constraints
        prompt = (
            f"Task {task_id}: {title}\n"
            f"Description: {desc}\n"
            f"Base SHA: {base_sha}\n"
            f"Allowed Scope Paths: {allowed_paths}\n"
            f"Forbidden Scope Paths: {forbidden_paths}\n"
            "Execution Safety Rules:\n"
            "- Workspace is disposable and isolated.\n"
            "- Modify only allowed paths.\n"
            "- Do not modify forbidden paths.\n"
            "- Do not commit.\n"
            "- Do not push.\n"
            "- Do not merge.\n"
            "- Do not modify canonical project-control files unless explicitly allowed.\n"
            "- Do not access another repository.\n"
            "- Stop on ambiguity."
        )

        resolved_exe = (self.pinned_identity or {}).get("path") or shutil.which(self.cli_command) or self.cli_command
        workspace_dir = Path(workspace_path)

        cmd = build_antigravity_argv(
            executable_path=str(resolved_exe),
            workspace_path=str(workspace_dir),
            prompt=prompt,
            timeout_seconds=min(timeout_seconds, 180),
        )

        # Scrub sensitive environment variables
        env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_VARS}

        exit_code = 0
        timed_out = False
        stdout_summary = ""
        stderr_summary = ""
        mutation_attempted = False

        try:
            res = self.runner(cmd, workspace_path, timeout_seconds, env)
            exit_code = res.returncode
            parsed_json = parse_antigravity_json_output(res.stdout or "")
            stdout_summary = (res.stdout or "")[:1000]
            stderr_summary = (res.stderr or "")[:1000]
            mutation_attempted = True
        except subprocess.TimeoutExpired as te:
            timed_out = True
            exit_code = None
            stdout_summary = (te.stdout or "")[:1000] if isinstance(te.stdout, str) else ""
            stderr_summary = (te.stderr or "")[:1000] if isinstance(te.stderr, str) else "Command timed out"
            mutation_attempted = True
        except Exception as e:
            exit_code = 1
            stderr_summary = str(e)[:1000]

        finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        return WorkerExecutionResult(
            worker_identity=f"antigravity-cli ({self.capability_status})",
            workspace_path=workspace_path,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout_summary=stdout_summary,
            stderr_summary=stderr_summary,
            mutation_attempted=mutation_attempted,
            started_at=started_at,
            finished_at=finished_at,
        )
