"""Bounded lifecycle and in-memory credential bridge for FreeLLMAPI.

This module defines an explicitly-invoked local process envelope. Importing it
never starts, stops, installs, updates, or registers a service.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from aos.process_utils import popen_headless, run_headless
from aos.providers.freellmapi_local import (
    DEFAULT_BASE_URL,
    FreeLLMAPILocalPlannerProvider,
    FreeLLMAPIReadiness,
)
from aos.secure_store import PROVIDER_ENV_VARS, read_provider_secret


FREELLMAPI_UPSTREAM_SHA = "15c30081d2ce832bea16d804d9edac4ed87c7bc3"
DEFAULT_INSTALL_PATH = Path(r"C:\Projects\freellmapi-aos-pin-15c30081")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3000
ENCRYPTION_KEY_ENV_VAR = "FREELLMAPI_LOCAL_ENCRYPTION_KEY"
GATEWAY_API_KEY_ENV_VAR = "FREELLMAPI_LOCAL_API_KEY"
_ENCRYPTION_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class FreeLLMAPILifecycleError(RuntimeError):
    """Fail-closed lifecycle/configuration error with no secret values."""


@dataclass(frozen=True)
class CredentialBinding:
    aos_identity: str
    aos_env_var: str
    freellmapi_platform: str


# Paid OpenAI is intentionally absent. These are the existing AOS free/direct
# credential identities supported by the pinned FreeLLMAPI commit.
APPROVED_FREE_BINDINGS: tuple[CredentialBinding, ...] = (
    CredentialBinding("NVIDIA", "NVIDIA_API_KEY", "nvidia"),
    CredentialBinding("GEMINI", "GEMINI_API_KEY", "google"),
    CredentialBinding("GROQ", "GROQ_API_KEY", "groq"),
    CredentialBinding("CLOUDFLARE", "CLOUDFLARE_API_TOKEN", "cloudflare"),
    CredentialBinding("OPENROUTER", "OPENROUTER_API_KEY", "openrouter"),
    CredentialBinding("CEREBRAS", "CEREBRAS_API_KEY", "cerebras"),
    CredentialBinding("HUGGINGFACE", "HF_TOKEN", "huggingface"),
)


def default_data_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    source = os.environ if environ is None else environ
    local_app_data = str(source.get("LOCALAPPDATA", "")).strip()
    if local_app_data:
        root = Path(local_app_data)
    else:
        root = Path.home() / ".local" / "share"
    return root / "AOS" / "freellmapi-local" / "data"


@dataclass(frozen=True)
class FreeLLMAPIConfig:
    install_path: Path = DEFAULT_INSTALL_PATH
    data_dir: Optional[Path] = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    node_executable: str = "node"
    upstream_sha: str = FREELLMAPI_UPSTREAM_SHA
    readiness_timeout_seconds: float = 1.5
    stop_timeout_seconds: float = 10.0

    @property
    def resolved_data_dir(self) -> Path:
        return (self.data_dir or default_data_dir()).expanduser().resolve()

    @property
    def db_path(self) -> Path:
        return self.resolved_data_dir / "freeapi.db"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    @property
    def server_entrypoint(self) -> Path:
        return self.install_path.expanduser().resolve() / "server" / "dist" / "index.js"

    def validate_static(self) -> None:
        if self.host != DEFAULT_HOST:
            raise FreeLLMAPILifecycleError("FreeLLMAPI host must be exactly 127.0.0.1")
        if not 1 <= int(self.port) <= 65535:
            raise FreeLLMAPILifecycleError("FreeLLMAPI port is outside the valid range")
        if not re.fullmatch(r"[0-9a-f]{40}", self.upstream_sha):
            raise FreeLLMAPILifecycleError("FreeLLMAPI upstream revision must be a full Git SHA")
        install = self.install_path.expanduser().resolve()
        data = self.resolved_data_dir
        if data == install or install in data.parents:
            raise FreeLLMAPILifecycleError("FreeLLMAPI data directory must be outside its source checkout")


CredentialReader = Callable[[str, Optional[str]], Optional[str]]


def _read_aos_secret(
    binding: CredentialBinding,
    environ: Mapping[str, str],
    credential_reader: CredentialReader,
) -> Optional[str]:
    value = str(environ.get(binding.aos_env_var, "")).strip()
    if value:
        return value
    stored = credential_reader(binding.aos_identity, binding.aos_env_var)
    return stored.strip() if isinstance(stored, str) and stored.strip() else None


def _encryption_key(
    environ: Mapping[str, str],
    credential_reader: CredentialReader,
) -> str:
    value = str(environ.get(ENCRYPTION_KEY_ENV_VAR, "")).strip()
    if not value:
        stored = credential_reader("FREELLMAPI_LOCAL", ENCRYPTION_KEY_ENV_VAR)
        value = stored.strip() if isinstance(stored, str) else ""
    if not _ENCRYPTION_KEY_RE.fullmatch(value):
        raise FreeLLMAPILifecycleError(
            "A 64-hex FreeLLMAPI encryption key is required in the AOS secure store"
        )
    return value


def build_declarative_config(
    *,
    environ: Optional[Mapping[str, str]] = None,
    credential_reader: CredentialReader = read_provider_secret,
    bindings: Sequence[CredentialBinding] = APPROVED_FREE_BINDINGS,
) -> tuple[Dict[str, Any], list[str]]:
    """Build supported FreeLLMAPI configuration in memory only.

    The returned object contains secrets and must only be serialized into the
    child environment. Callers must never log or persist it.
    """
    source = os.environ if environ is None else environ
    keys = []
    configured_platforms = []
    for binding in bindings:
        secret = _read_aos_secret(binding, source, credential_reader)
        if not secret:
            continue
        if binding.freellmapi_platform == "cloudflare":
            account_id = str(source.get("CLOUDFLARE_ACCOUNT_ID", "")).strip()
            if not account_id:
                continue
            secret = f"{account_id}:{secret}"
        keys.append({
            "platform": binding.freellmapi_platform,
            "key": secret,
            "label": f"aos-bridge-{binding.freellmapi_platform}",
            "enabled": True,
        })
        configured_platforms.append(binding.freellmapi_platform)
    return {
        "keys": keys,
        "routing": {"strategy": "reliable", "keySelectionStrategy": "auto"},
    }, configured_platforms


@dataclass
class FreeLLMAPIProcessSpec:
    command: tuple[str, ...]
    cwd: Path
    environment: Dict[str, str] = field(repr=False)
    configured_platforms: tuple[str, ...] = ()
    _sensitive_cleared: bool = field(default=False, init=False, repr=False)

    def to_telemetry(self) -> Dict[str, Any]:
        return {
            "command": [self.command[0], "server/dist/index.js"],
            "cwd": str(self.cwd),
            "host": self.environment.get("HOST"),
            "port": int(self.environment.get("PORT", "0") or 0),
            "db_path": self.environment.get("FREEAPI_DB_PATH"),
            "configured_platforms": list(self.configured_platforms),
            "configured_credential_count": len(self.configured_platforms),
            "secrets_exposed": False,
        }

    def clear_sensitive(self) -> None:
        for key in ("ENCRYPTION_KEY", "FREEAPI_CONFIG_JSON"):
            if key in self.environment:
                self.environment[key] = ""
                del self.environment[key]
        self._sensitive_cleared = True

    @property
    def sensitive_cleared(self) -> bool:
        return self._sensitive_cleared


def build_process_spec(
    config: FreeLLMAPIConfig,
    *,
    environ: Optional[Mapping[str, str]] = None,
    credential_reader: CredentialReader = read_provider_secret,
) -> FreeLLMAPIProcessSpec:
    """Create a foreground process spec without writing files or starting it."""
    config.validate_static()
    source = dict(os.environ if environ is None else environ)
    declarative, platforms = build_declarative_config(
        environ=source,
        credential_reader=credential_reader,
    )
    encryption_key = _encryption_key(source, credential_reader)

    # Do not leak unrelated AOS provider credentials into the child. Approved
    # values exist only in the one supported in-memory declarative payload.
    for env_var in set(PROVIDER_ENV_VARS.values()) | {
        GATEWAY_API_KEY_ENV_VAR,
        ENCRYPTION_KEY_ENV_VAR,
        "OPENAI_API_KEY",
    }:
        source.pop(env_var, None)

    source.update({
        "HOST": config.host,
        "PORT": str(config.port),
        "NODE_ENV": "production",
        "FREEAPI_DB_PATH": str(config.db_path),
        "FREEAPI_DB_DIR_HARDENING": "1",
        # Prevent an accidental checkout-local .env from becoming another
        # credential authority. The path is deliberately absent.
        "FREEAPI_ENV_PATH": str(config.resolved_data_dir / ".env.disabled"),
        "ENCRYPTION_KEY": encryption_key,
        "FREEAPI_CONFIG_JSON": json.dumps(declarative, separators=(",", ":")),
    })
    return FreeLLMAPIProcessSpec(
        command=(config.node_executable, str(config.server_entrypoint)),
        cwd=config.install_path.expanduser().resolve(),
        environment=source,
        configured_platforms=tuple(platforms),
    )


def checkout_revision(install_path: Path) -> str:
    result = run_headless(
        ["git", "rev-parse", "HEAD"],
        cwd=str(install_path),
        timeout=15,
    )
    if result.returncode != 0:
        raise FreeLLMAPILifecycleError("FreeLLMAPI checkout revision is unavailable")
    return str(result.stdout or "").strip()


@dataclass(frozen=True)
class FreeLLMAPILifecycleStatus:
    state: str
    service_available: bool
    eligible: bool
    reason: str
    pid: Optional[int] = None
    quota_limited: bool = False

    def to_telemetry(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "service_available": self.service_available,
            "eligible": self.eligible,
            "reason": self.reason,
            "pid": self.pid,
            "quota_limited": self.quota_limited,
            "secrets_exposed": False,
            "production": "NO_GO",
        }


class FreeLLMAPILifecycle:
    """Own one explicitly-started foreground child process tree."""

    def __init__(
        self,
        config: Optional[FreeLLMAPIConfig] = None,
        *,
        environ: Optional[Mapping[str, str]] = None,
        credential_reader: CredentialReader = read_provider_secret,
        revision_reader: Callable[[Path], str] = checkout_revision,
        spawner: Callable[..., Any] = popen_headless,
        readiness_probe: Optional[Callable[[], FreeLLMAPIReadiness]] = None,
    ) -> None:
        self.config = config or FreeLLMAPIConfig()
        self._environ = dict(os.environ if environ is None else environ)
        self._credential_reader = credential_reader
        self._revision_reader = revision_reader
        self._spawner = spawner
        self._readiness_probe = readiness_probe
        self._process: Optional[Any] = None

    def validate_checkout(self) -> None:
        self.config.validate_static()
        install = self.config.install_path.expanduser().resolve()
        if not (install / "package.json").is_file():
            raise FreeLLMAPILifecycleError("Pinned FreeLLMAPI checkout is missing package.json")
        actual = self._revision_reader(install)
        if actual != self.config.upstream_sha:
            raise FreeLLMAPILifecycleError("FreeLLMAPI checkout does not match the pinned upstream SHA")
        if not self.config.server_entrypoint.is_file():
            raise FreeLLMAPILifecycleError(
                "Pinned FreeLLMAPI server is not built; run the documented bounded build first"
            )

    def start(self) -> FreeLLMAPILifecycleStatus:
        if self._process is not None and self._process.poll() is None:
            return FreeLLMAPILifecycleStatus(
                "RUNNING", True, False, "already_running", int(self._process.pid)
            )
        if self._process is not None:
            if hasattr(self._process, "close"):
                self._process.close()
            self._process = None
        self.validate_checkout()
        spec = build_process_spec(
            self.config,
            environ=self._environ,
            credential_reader=self._credential_reader,
        )
        self.config.resolved_data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._process = self._spawner(
                spec.command,
                cwd=str(spec.cwd),
                env=spec.environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            spec.clear_sensitive()
        if self._process is None:
            raise FreeLLMAPILifecycleError("FreeLLMAPI process did not start")
        if self._process.poll() is not None:
            if hasattr(self._process, "close"):
                self._process.close()
            self._process = None
            raise FreeLLMAPILifecycleError("FreeLLMAPI process exited during startup")
        return FreeLLMAPILifecycleStatus(
            "STARTING", False, False, "readiness_not_yet_observed", int(self._process.pid)
        )

    def probe(self) -> FreeLLMAPILifecycleStatus:
        probe = self._readiness_probe
        if probe is None:
            adapter = FreeLLMAPILocalPlannerProvider(
                base_url=self.config.base_url,
                readiness_timeout_seconds=self.config.readiness_timeout_seconds,
            )
            probe = lambda: adapter.check_readiness(force=True)
        readiness = probe()
        pid = int(self._process.pid) if self._process is not None else None
        if readiness.eligible:
            return FreeLLMAPILifecycleStatus("RUNNING", True, True, readiness.reason, pid)
        if readiness.service_available:
            return FreeLLMAPILifecycleStatus(
                "DEGRADED",
                True,
                False,
                readiness.reason,
                pid,
                quota_limited=readiness.reason == "all_upstreams_rate_limited",
            )
        return FreeLLMAPILifecycleStatus("UNAVAILABLE", False, False, readiness.reason, pid)

    def stop(self) -> FreeLLMAPILifecycleStatus:
        process = self._process
        self._process = None
        if process is None:
            return FreeLLMAPILifecycleStatus("STOPPED", False, False, "not_running")
        if process.poll() is None:
            process.terminate_tree()
            try:
                process.wait(timeout=self.config.stop_timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=self.config.stop_timeout_seconds)
        if hasattr(process, "close"):
            process.close()
        return FreeLLMAPILifecycleStatus("STOPPED", False, False, "operator_stop")

    def restart(self) -> FreeLLMAPILifecycleStatus:
        self.stop()
        return self.start()
