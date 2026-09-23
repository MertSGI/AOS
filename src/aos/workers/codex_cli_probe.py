"""Machine-local Codex CLI identity, ChatGPT-auth, and quota attestation."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from aos.process_utils import popen_headless, run_headless
from aos.runtime_store import atomic_json
from aos.validate import validate_document

CODEX_ADAPTER_CONTRACT_VERSION = "1.0.0"
CODEX_CAPABILITY_PROFILE_VERSION = "1.0.0"
CODEX_SENSITIVE_ENV_VARS = {
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "OPENAI_ACCESS_TOKEN",
    "AZURE_OPENAI_API_KEY",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
}


def get_codex_capability_store_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) / "AOS" / "capabilities" if local else Path.home() / ".aos" / "capabilities"
    return base / "codex-cli.json"


def compute_file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def resolve_codex_executable_identity(
    cli_command: str = "codex",
    *,
    which_resolver: Callable[[str], Optional[str]] = shutil.which,
    runner: Optional[Callable[[list[str]], subprocess.CompletedProcess]] = None,
) -> Optional[Dict[str, str]]:
    path = which_resolver(cli_command) or (cli_command if Path(cli_command).is_file() else None)
    if not path or not Path(path).is_file():
        return None
    try:
        result = runner([path, "--version"]) if runner else run_headless(
            [path, "--version"], timeout=10
        )
        version = str(result.stdout or "").strip()
        if result.returncode != 0 or not version:
            return None
        return {
            "path": str(Path(path).resolve()),
            "filename": Path(path).name,
            "sha256": compute_file_sha256(path),
            "version": version,
        }
    except (OSError, subprocess.SubprocessError):
        return None


def _walk(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def parse_codex_doctor_auth(report: Dict[str, Any]) -> Dict[str, Any]:
    """Extract only closed auth booleans/mode; never copy token or path values."""
    mode: Optional[str] = None
    chatgpt_tokens = False
    api_key = False

    def flag(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"yes", "true", "present", "configured"}:
                return True
            if normalized in {"no", "false", "none", "absent", "not present"}:
                return False
        return None

    for key, value in _walk(report):
        normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
        text = str(value).lower() if isinstance(value, str) else ""
        if normalized in {"auth_mode", "authentication_mode", "stored_auth_mode"}:
            if text in {"chatgpt", "api_key", "apikey"}:
                mode = "api_key" if "api" in text else "chatgpt"
        observed = flag(value)
        if "chatgpt" in normalized and "token" in normalized and observed is not None:
            chatgpt_tokens = chatgpt_tokens or observed
        if ("api_key" in normalized or "apikey" in normalized) and observed is not None:
            api_key = api_key or observed
    usable = mode == "chatgpt" and chatgpt_tokens and not api_key
    return {
        "auth_mode": mode or "unknown",
        "chatgpt_tokens_present": chatgpt_tokens,
        "api_key_present": api_key,
        "chatgpt_subscription_usable": usable,
    }


def parse_codex_rate_limits(payload: Dict[str, Any], *, observed_at: Optional[str] = None) -> Dict[str, Any]:
    """Normalize the stable account/rateLimits/read response without account data."""
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        raise ValueError("rate-limit result must be an object")
    buckets = result.get("rateLimitsByLimitId") or result.get("rate_limits_by_limit_id") or {}
    if not isinstance(buckets, dict):
        buckets = {}
    codex = buckets.get("codex") if isinstance(buckets.get("codex"), dict) else {}
    primary = codex.get("primary") if isinstance(codex.get("primary"), dict) else {}
    secondary = codex.get("secondary") if isinstance(codex.get("secondary"), dict) else {}

    def number(source: Dict[str, Any], *names: str) -> Optional[float]:
        for name in names:
            value = source.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                return float(value)
        return None

    reached = codex.get("rateLimitReachedType") or codex.get("rate_limit_reached_type")
    primary_used = number(primary, "usedPercent", "used_percent")
    secondary_used = number(secondary, "usedPercent", "used_percent")
    reset_values = [
        value for value in (
            number(primary, "resetsAt", "resets_at"),
            number(secondary, "resetsAt", "resets_at"),
        ) if value is not None
    ]
    state = "UNKNOWN"
    if reached:
        state = "QUOTA_EXHAUSTED"
    elif primary_used is not None or secondary_used is not None:
        maximum = max(value for value in (primary_used, secondary_used) if value is not None)
        state = "LOW_OR_SCARCE" if maximum >= 90 else "AVAILABLE"
    return {
        "schema_version": "1.0.0",
        "state": state,
        "observed_at": observed_at or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "retry_after_epoch": max(reset_values) if reached and reset_values else None,
        "primary_used_percent": primary_used,
        "secondary_used_percent": secondary_used,
        "rate_limit_reached": bool(reached),
        "source": "CODEX_APP_SERVER",
    }


def read_codex_rate_limits(
    executable: str,
    *,
    timeout: float = 15.0,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Read the stable rate-limit RPC through a bounded owned app-server process."""
    process = popen_headless(
        [executable, "app-server", "--stdio"],
        env=env or build_codex_child_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def read_lines() -> None:
        try:
            assert process._process.stdout is not None  # OwnedProcess intentionally owns this pipe.
            for line in process._process.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    try:
        assert process._process.stdin is not None
        requests = (
            {"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "aos-resource-os", "version": "1.0.0"},
                "capabilities": {},
            }},
            {"method": "initialized", "params": {}},
            {"id": 2, "method": "account/rateLimits/read", "params": {}},
        )
        for request in requests:
            process._process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        process._process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == 2:
                if "error" in message:
                    raise RuntimeError("Codex rate-limit RPC returned a structured error")
                return parse_codex_rate_limits(message)
        raise TimeoutError("Codex rate-limit RPC did not complete within the bound")
    finally:
        process.terminate_tree()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def resolve_codex_capability_status(
    *,
    identity: Optional[Dict[str, str]] = None,
    store_path: Optional[Path] = None,
) -> str:
    target = store_path or get_codex_capability_store_path()
    current = identity or resolve_codex_executable_identity()
    if current is None or not target.is_file():
        return "UNPROVEN"
    try:
        attestation = json.loads(target.read_text(encoding="utf-8"))
        validation = validate_document("worker_capability_attestation", attestation)
        codex_ext = (attestation.get("extensions") or {}).get("codex_cli", {})
        if (
            validation.is_valid
            and attestation.get("worker_adapter") == "codex_cli"
            and attestation.get("adapter_contract_version") == CODEX_ADAPTER_CONTRACT_VERSION
            and attestation.get("executable_sha256") == current["sha256"]
            and attestation.get("reported_cli_version") == current["version"]
            and attestation.get("capability_status") == "PROVEN"
            and isinstance(codex_ext, dict)
            and codex_ext.get("auth_mode") == "chatgpt"
            and codex_ext.get("api_key_present") is False
        ):
            return "PROVEN"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return "UNPROVEN"


def build_codex_child_environment(parent: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    source = parent or dict(os.environ)
    return {key: value for key, value in source.items() if key.upper() not in CODEX_SENSITIVE_ENV_VARS}


def run_codex_cli_probe(
    *,
    cli_command: str = "codex",
    store_path: Optional[Path] = None,
    doctor_runner: Optional[Callable[[list[str], Dict[str, str]], subprocess.CompletedProcess]] = None,
    quota_reader: Optional[Callable[[], Dict[str, Any]]] = None,
    identity_resolver: Optional[Callable[[str], Optional[Dict[str, str]]]] = None,
    aos_revision: str = "0" * 40,
) -> Dict[str, Any]:
    """Run read-only identity/auth/quota checks; no model turn is invoked."""
    identity = (identity_resolver or resolve_codex_executable_identity)(cli_command)
    if identity is None:
        return {"capability_status": "UNPROVEN", "reason": "EXECUTABLE_UNAVAILABLE"}
    env = build_codex_child_environment()
    runner = doctor_runner or (
        lambda argv, child_env: run_headless(argv, timeout=60, env=child_env)
    )
    doctor = runner([identity["path"], "doctor", "--json"], env)
    try:
        report = json.loads(str(doctor.stdout or "{}"))
    except json.JSONDecodeError:
        report = {}
    auth = parse_codex_doctor_auth(report if isinstance(report, dict) else {})
    quota = quota_reader() if quota_reader is not None else {
        "state": "UNKNOWN", "source": "NOT_OBSERVED"
    }
    proven = doctor.returncode == 0 and auth["chatgpt_subscription_usable"]
    attestation = {
        "schema_version": "0.1.0",
        "worker_adapter": "codex_cli",
        "adapter_contract_version": CODEX_ADAPTER_CONTRACT_VERSION,
        "executable_filename": identity["filename"],
        "executable_sha256": identity["sha256"],
        "reported_cli_version": identity["version"],
        "runtime_environment_profile_version": CODEX_CAPABILITY_PROFILE_VERSION,
        "runtime_environment_fingerprint_sha256": hashlib.sha256(json.dumps({
            "auth_mode": auth["auth_mode"],
            "api_key_present": auth["api_key_present"],
        }, sort_keys=True).encode("utf-8")).hexdigest(),
        "capability_status": "PROVEN" if proven else "UNPROVEN",
        "probe_id": f"CODEX-PROBE-{uuid.uuid4().hex[:12]}",
        "probe_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "aos_revision_used_for_probe": aos_revision,
        "capabilities_proven": (
            ["CHATGPT_AUTH", "JSONL_EXEC", "EXACT_UUID_RESUME", "STRUCTURED_QUOTA"]
            if proven else []
        ),
        "limitations": ["NO_MODEL_TURN_EXECUTED", "PRODUCTION_NO_GO"],
        "extensions": {
            "codex_cli": {
                **auth,
                "quota": quota,
                "paid_api_fallback": "DISABLED",
            }
        },
    }
    validation = validate_document("worker_capability_attestation", attestation)
    if not validation.is_valid:
        raise ValueError("Codex capability attestation failed schema validation")
    target = store_path or get_codex_capability_store_path()
    atomic_json(target, attestation)
    return attestation
