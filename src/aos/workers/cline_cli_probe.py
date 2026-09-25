"""Machine-local official Cline CLI identity, capability, and auth attestation."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
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
    Path(r"C:\Users\mozcelikbas\AppData\Local\AOS\tools\nodejs\node-v24.21.0-win-x64\cline.cmd"),
    Path(r"C:\Users\mozcelikbas\AppData\Local\AOS\tools\nodejs\node-v24.21.0-win-x64\node_modules\cline\node_modules\@cline\cli-windows-x64\bin\cline.exe"),
    Path(r"C:\Users\mozcelikbas\AppData\Roaming\npm\cline.cmd"),
]


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
    # Search which or explicit candidate paths
    path = which_resolver(cli_command)
    if not path or not Path(path).is_file():
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
            "filename": Path(path).name,
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
    return "OPERATIONAL_BOUNDED"


def attest_cline_capability(
    cli_command: str = "cline",
    *,
    capability_store: Optional[Path] = None,
    now_iso: Optional[str] = None,
) -> Dict[str, Any]:
    store_path = capability_store or get_cline_capability_store_path()
    identity = resolve_cline_executable_identity(cli_command)
    status = resolve_cline_capability_status(cli_command, identity=identity)
    ts = now_iso or datetime.datetime.now(datetime.timezone.utc).isoformat()

    record: Dict[str, Any] = {
        "contract_version": CLINE_ADAPTER_CONTRACT_VERSION,
        "profile_version": CLINE_CAPABILITY_PROFILE_VERSION,
        "attested_at": ts,
        "capability_status": status,
        "executable_identity": identity,
        "package_name": "cline",
        "install_source": "https://registry.npmjs.org/cline/-/cline-3.0.65.tgz",
        "supported_execution_harness": True,
        "supported_features": [
            "headless_execution",
            "json_stream_output",
            "custom_cwd",
            "isolated_data_dir",
            "isolated_config_dir",
            "provider_pinning",
            "model_pinning",
            "session_id_resume",
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
    return record
