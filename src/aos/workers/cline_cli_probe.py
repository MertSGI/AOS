"""Machine-local official Cline CLI identity, capability, and auth attestation."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from aos.process_utils import run_headless
from aos.runtime_store import atomic_json

CLINE_ADAPTER_CONTRACT_VERSION = "1.0.0"
CLINE_CAPABILITY_PROFILE_VERSION = "1.0.0"
CLINE_SENSITIVE_ENV_VARS = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "CLINE_API_KEY",
    "OPENROUTER_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "GEMINI_API_KEY",
}

DEFAULT_CLINE_PATHS = [
    Path(r"C:\Users\mozcelikbas\AppData\Local\AOS\tools\nodejs\node-v24.21.0-win-x64\node_modules\cline\node_modules\@cline\cli-windows-x64\bin\cline.exe"),
    Path(r"C:\Users\mozcelikbas\AppData\Local\AOS\tools\nodejs\node-v24.21.0-win-x64\cline.cmd"),
    Path(r"C:\Users\mozcelikbas\AppData\Roaming\npm\cline.cmd"),
]

_SAFE_CONFIG_KEYS = {"provider", "model", "config_dir", "data_dir", "cost_class", "paid_fallback"}
_SECRET_KEY_TOKENS = ("secret", "token", "password", "api_key", "apikey", "credential")
_ALLOWED_COST_CLASSES = {"FREE_LOCAL", "FREE_TIER_CLOUD", "QUOTA_LIMITED", "SUBSCRIPTION_INCLUDED"}


@dataclass(frozen=True)
class ClineRuntimeConfig:
    provider: str
    model: str
    config_dir: str
    data_dir: str
    cost_class: str
    paid_fallback: str = "DISABLED"


def get_cline_runtime_config_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) / "AOS" / "config" if local else Path.home() / ".aos" / "config"
    return base / "cline-backend.json"


def load_cline_runtime_config(path: Optional[Path] = None) -> Optional[ClineRuntimeConfig]:
    """Load provider/model selection from non-secret machine-local state."""
    target = path or get_cline_runtime_config_path()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) - _SAFE_CONFIG_KEYS:
        return None
    if any(token in str(key).lower() for key in value for token in _SECRET_KEY_TOKENS):
        return None
    provider = str(value.get("provider") or "").strip()
    model = str(value.get("model") or "").strip()
    config_dir = Path(str(value.get("config_dir") or "")).expanduser()
    data_dir = Path(str(value.get("data_dir") or "")).expanduser()
    cost_class = str(value.get("cost_class") or "").strip().upper()
    paid_fallback = str(value.get("paid_fallback") or "").strip().upper()
    if not provider or not model or len(provider) > 80 or len(model) > 160:
        return None
    if not config_dir.is_absolute() or not data_dir.is_absolute():
        return None
    if cost_class not in _ALLOWED_COST_CLASSES or paid_fallback != "DISABLED":
        return None
    return ClineRuntimeConfig(
        provider=provider,
        model=model,
        config_dir=str(config_dir.resolve()),
        data_dir=str(data_dir.resolve()),
        cost_class=cost_class,
        paid_fallback=paid_fallback,
    )


def build_cline_child_environment(parent: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    source = parent or dict(os.environ)
    env = {key: value for key, value in source.items() if key.upper() not in CLINE_SENSITIVE_ENV_VARS}
    node_dir = r"C:\Users\mozcelikbas\AppData\Local\AOS\tools\nodejs\node-v24.21.0-win-x64"
    if os.path.isdir(node_dir):
        env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    return env


def get_cline_capability_store_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) / "AOS" / "capabilities" if local else Path.home() / ".aos" / "capabilities"
    base.mkdir(parents=True, exist_ok=True)
    return base / "cline-cli.json"


def compute_file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def resolve_cline_executable_identity(
    cli_command: str = "cline",
    *,
    which_resolver: Callable[[str], Optional[str]] = shutil.which,
    runner: Optional[Callable[[list[str]], subprocess.CompletedProcess]] = None,
) -> Optional[Dict[str, str]]:
    # Prefer the signed native Windows executable over mutable wrapper scripts.
    path = None
    if os.name == "nt":
        native = next((candidate for candidate in DEFAULT_CLINE_PATHS if candidate.suffix.lower() == ".exe" and candidate.is_file()), None)
        if native is not None:
            path = str(native)
    if not path:
        discovered = which_resolver(cli_command)
        path = discovered if discovered and Path(discovered).is_file() else None
    if not path:
        for candidate in DEFAULT_CLINE_PATHS:
            if candidate.is_file():
                path = str(candidate)
                break
    if not path or not Path(path).is_file():
        return None

    try:
        cmd = [path, "--version"]
        result = runner(cmd) if runner else run_headless(cmd, timeout=10)
        version = str(result.stdout or "").strip()
        if result.returncode != 0 or not version:
            return None
        return {
            "path": str(Path(path).resolve()),
            "filename": Path(path).name.lower(),
            "sha256": compute_file_sha256(path),
            "version": version,
        }
    except (OSError, subprocess.SubprocessError):
        return None


def resolve_cline_capability_status(
    cli_command: str = "cline",
    *,
    identity: Optional[Dict[str, str]] = None,
    capability_store: Optional[Path] = None,
) -> str:
    target_identity = identity or resolve_cline_executable_identity(cli_command)
    if not target_identity:
        return "NOT_OPERATIONALLY_PROVEN"
    store_path = capability_store or get_cline_capability_store_path()
    runtime_config = load_cline_runtime_config()
    if runtime_config is None:
        return "NOT_OPERATIONALLY_PROVEN"
    try:
        record = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "NOT_OPERATIONALLY_PROVEN"
    if not isinstance(record, dict):
        return "NOT_OPERATIONALLY_PROVEN"
    recorded_identity = record.get("executable_identity")
    if not isinstance(recorded_identity, dict):
        return "NOT_OPERATIONALLY_PROVEN"
    if any(recorded_identity.get(key) != target_identity.get(key) for key in ("path", "sha256", "version")):
        return "NOT_OPERATIONALLY_PROVEN"
    if record.get("provider") != runtime_config.provider or record.get("model") != runtime_config.model:
        return "NOT_OPERATIONALLY_PROVEN"
    required = (
        "executable_proven",
        "provider_model_proven",
        "auth_smoke_proven",
        "non_mutating_smoke_proven",
        "bounded_mutation_smoke_proven",
    )
    if not all(record.get(field) is True for field in required):
        return "NOT_OPERATIONALLY_PROVEN"
    if record.get("session_resume_proven") is not True:
        return "OPERATIONAL_START_ONLY"
    return "OPERATIONAL"


def attest_cline_capability(
    cli_command: str = "cline",
    *,
    capability_store: Optional[Path] = None,
    now_iso: Optional[str] = None,
    proof: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    store_path = capability_store or get_cline_capability_store_path()
    identity = resolve_cline_executable_identity(cli_command)
    runtime_config = load_cline_runtime_config()
    ts = now_iso or datetime.datetime.now(datetime.timezone.utc).isoformat()
    proof_value = dict(proof or {})

    record: Dict[str, Any] = {
        "contract_version": CLINE_ADAPTER_CONTRACT_VERSION,
        "profile_version": CLINE_CAPABILITY_PROFILE_VERSION,
        "attested_at": ts,
        "capability_status": "NOT_OPERATIONALLY_PROVEN",
        "executable_identity": identity,
        "package_name": "cline",
        "provider": runtime_config.provider if runtime_config else None,
        "model": runtime_config.model if runtime_config else None,
        "cost_class": runtime_config.cost_class if runtime_config else None,
        "paid_fallback": "DISABLED",
        "supported_execution_harness": False,
        "executable_proven": bool(identity and proof_value.get("executable_proven") is True),
        "provider_model_proven": proof_value.get("provider_model_proven") is True,
        "auth_smoke_proven": proof_value.get("auth_smoke_proven") is True,
        "non_mutating_smoke_proven": proof_value.get("non_mutating_smoke_proven") is True,
        "bounded_mutation_smoke_proven": proof_value.get("bounded_mutation_smoke_proven") is True,
        "session_resume_proven": proof_value.get("session_resume_proven") is True,
        "supported_features": [
            "headless_execution",
            "json_stream_output",
            "custom_cwd",
            "isolated_data_dir",
            "isolated_config_dir",
            "provider_pinning",
            "model_pinning",
            "tool_auto_approve_control",
            "mcp_server_integration",
        ],
        "underlying_providers_supported": [
            "openai-compatible",
            "anthropic",
            "openrouter",
            "ollama",
            "vertex",
            "aws-bedrock",
        ],
    }
    atomic_json(store_path, record)
    status = resolve_cline_capability_status(
        cli_command, identity=identity, capability_store=store_path
    )
    record["capability_status"] = status
    record["supported_execution_harness"] = status in {"OPERATIONAL", "OPERATIONAL_START_ONLY"}
    if status == "OPERATIONAL":
        record["supported_features"].append("session_id_resume")
    atomic_json(store_path, record)
    return record
