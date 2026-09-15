"""Sanitized provider/runtime discovery for AOS Autonomous Planning Kernel."""
from __future__ import annotations

import ctypes
import datetime as _dt
import json
import os
import platform
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from aos.providers import GeminiPlannerProvider, GroqPlannerProvider, NemotronPlannerProvider, OllamaPlannerProvider
from aos.planner import PlannerContractError, PlannerCredentialError, PlannerTransientError
from extensions.autonomy_fabric.native_workers import redact_secrets


_PROVIDERS = {
    "nemotron": NemotronPlannerProvider,
    "gemini": GeminiPlannerProvider,
    "groq": GroqPlannerProvider,
    "ollama": OllamaPlannerProvider,
}
_ENV = {
    "nemotron": "NVIDIA_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "ollama": None,
}

PROBE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version", "project_id", "source_sha", "selected_milestone", "selected_next_action",
        "target_base_sha", "risk_class", "mutation_intent", "ambiguity_detected", "ambiguity_reasons",
        "human_gate_required", "rationale", "disposition",
    ],
    "properties": {
        "schema_version": {"type": "string", "enum": ["0.1.0"]},
        "project_id": {"type": "string"},
        "source_sha": {"type": "string"},
        "selected_milestone": {"type": "string"},
        "selected_next_action": {"type": "string"},
        "target_base_sha": {"type": ["string", "null"]},
        "risk_class": {"type": "string"},
        "mutation_intent": {"type": "string"},
        "ambiguity_detected": {"type": "boolean"},
        "ambiguity_reasons": {"type": "array", "items": {"type": "string"}},
        "human_gate_required": {"type": "boolean"},
        "rationale": {"type": "string"},
        "disposition": {"type": "string"},
    },
}

PROBE_PROMPT = """PUBLIC SYNTHETIC CONNECTIVITY PROBE. No private data is supplied.
Return one structured planner decision object only:
schema_version=0.1.0
project_id=synthetic-public-probe
source_sha=0000000000000000000000000000000000000000
selected_milestone=Connectivity Probe
selected_next_action=No action; synthetic probe only.
target_base_sha=null
risk_class=R0
mutation_intent=NONE
ambiguity_detected=false
ambiguity_reasons=[]
human_gate_required=false
rationale=Public synthetic provider contract probe.
disposition=SHADOW_ACCEPT
"""


def _hydrate() -> None:
    try:
        from aos.secure_store import hydrate_environment
        hydrate_environment(overwrite=False)
    except Exception:
        return


def _load_policy(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("routing policy must be object")
    return value


def _model_for(policy: Dict[str, Any], provider_id: str) -> Optional[str]:
    providers = policy.get("providers", {})
    cfg = providers.get(provider_id) if isinstance(providers, dict) else None
    if not isinstance(cfg, dict):
        return None
    model = cfg.get("model_id")
    return str(model) if model else None


def _classify(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, PlannerCredentialError):
        return "UNAVAILABLE"
    if isinstance(exc, PlannerContractError):
        return "NON_RETRYABLE_FAILED"
    if isinstance(exc, PlannerTransientError):
        if any(token in text for token in ("429", "quota", "rate limit", "resource_exhausted")):
            return "QUOTA_EXHAUSTED"
        if "timeout" in text:
            return "TIMED_OUT"
        return "RETRYABLE_FAILED"
    if isinstance(exc, (ConnectionError, urllib.error.URLError, OSError)):
        return "UNAVAILABLE"
    if isinstance(exc, TimeoutError):
        return "TIMED_OUT"
    return "NON_RETRYABLE_FAILED"


def provider_runtime_matrix(policy_path: Path) -> Dict[str, Any]:
    _hydrate()
    policy = _load_policy(policy_path)
    result: Dict[str, Any] = {}
    for provider_id in ("nemotron", "gemini", "groq"):
        env_name = _ENV[provider_id]
        credential_present = bool(env_name and os.environ.get(env_name))
        row: Dict[str, Any] = {
            "provider_id": provider_id,
            "credential_present": "YES" if credential_present else "NO",
            "connectivity": "FAIL",
            "structured_contract": "FAIL",
            "response_id": None,
            "latency_ms": None,
            "failure_class": "UNAVAILABLE" if not credential_present else None,
            "evidence_class": "NOT_PROVEN",
        }
        model = _model_for(policy, provider_id)
        if not credential_present or not model:
            result[provider_id] = row
            continue
        started = time.monotonic()
        try:
            provider = _PROVIDERS[provider_id](model=model)
            proposal, response_id, _usage = provider.generate_plan(PROBE_PROMPT, PROBE_SCHEMA)
            row["latency_ms"] = int((time.monotonic() - started) * 1000)
            row["connectivity"] = "PASS"
            row["structured_contract"] = "PASS" if isinstance(proposal, dict) and proposal.get("project_id") == "synthetic-public-probe" else "FAIL"
            row["response_id"] = str(response_id)[:160] if response_id else None
            provenance = getattr(provider, "execution_provenance", "UNKNOWN")
            row["evidence_class"] = "LIVE_EXTERNAL_PROOF" if provenance == "LIVE_EXTERNAL" else "LOCAL_RUNTIME_PROOF"
            row["failure_class"] = None if row["structured_contract"] == "PASS" else "NON_RETRYABLE_FAILED"
        except Exception as exc:
            row["latency_ms"] = int((time.monotonic() - started) * 1000)
            row["failure_class"] = _classify(exc)
            row["error_class"] = exc.__class__.__name__
            row["message"] = redact_secrets(str(exc))[:300]
        result[provider_id] = row
    result["openai"] = {
        "provider_id": "openai",
        "credential_present": "YES" if os.environ.get("OPENAI_API_KEY") else "NO",
        "connectivity": "NOT_PROBED",
        "structured_contract": "NOT_PROBED",
        "response_id": None,
        "latency_ms": None,
        "failure_class": None,
        "evidence_class": "NOT_PROVEN",
    }
    return result


def _memory_bytes() -> Optional[int]:
    if os.name != "nt":
        return None
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return int(status.ullTotalPhys)
    return None


def local_reasoning_discovery(base_url: str = "http://127.0.0.1:11434") -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "service_available": False,
        "installed_models": [],
        "structured_output_compatible": False,
        "selected_local_fallback": None,
        "system_resources": {
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "total_memory_bytes": _memory_bytes(),
        },
        "state": "UNAVAILABLE_NO_APPROVED_MODEL",
    }
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models = [m.get("name") for m in payload.get("models", []) if isinstance(m, dict) and m.get("name")]
        result["service_available"] = True
        result["installed_models"] = models
    except Exception:
        return result
    if not result["installed_models"]:
        return result
    model = str(result["installed_models"][0])
    body = json.dumps({
        "model": model,
        "prompt": "PUBLIC synthetic structured-output probe. Return JSON object {\\\"ok\\\": true} only.",
        "format": "json",
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
        generated = json.loads(payload.get("response", "{}"))
        if generated.get("ok") is True:
            result["structured_output_compatible"] = True
            result["selected_local_fallback"] = model
            result["state"] = "AVAILABLE_APPROVED_INSTALLED_MODEL"
    except Exception:
        pass
    return result


def _snapshot_provider_env() -> Dict[str, tuple[bool, Optional[str]]]:
    names = tuple(name for name in _ENV.values() if name)
    return {name: (name in os.environ, os.environ.get(name)) for name in names}


def _restore_provider_env(snapshot: Dict[str, tuple[bool, Optional[str]]]) -> None:
    for name, (was_present, value) in snapshot.items():
        if was_present:
            os.environ[name] = "" if value is None else value
        else:
            os.environ.pop(name, None)


def run_sanitized_probe(policy_path: Path) -> Dict[str, Any]:
    # Credential hydration is deliberately scoped to the live probe.  Provider
    # secrets loaded from Windows Credential Manager must never leak into later
    # regression tests, checkpoints, evidence, or unrelated child processes.
    snapshot = _snapshot_provider_env()
    try:
        matrix = provider_runtime_matrix(policy_path)
        local = local_reasoning_discovery()
        nemotron = matrix.get("nemotron", {})
        nemotron_live = "PASS" if (
            nemotron.get("connectivity") == "PASS"
            and nemotron.get("structured_contract") == "PASS"
            and nemotron.get("evidence_class") == "LIVE_EXTERNAL_PROOF"
        ) else "NOT_YET_PROVEN"
        return {
            "schema_version": "1.0.0",
            "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "provider_runtime_matrix": matrix,
            "nemotron_live_external_state": nemotron_live,
            "local_reasoning": local,
            "secrets_exposed": False,
        }
    finally:
        _restore_provider_env(snapshot)
