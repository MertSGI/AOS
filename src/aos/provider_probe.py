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
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from jsonschema import Draft202012Validator

from aos.providers import (
    GeminiPlannerProvider,
    GroqPlannerProvider,
    NemotronPlannerProvider,
    OllamaPlannerProvider,
    GenericOpenAICompatiblePlannerProvider,
    FreeLLMAPILocalPlannerProvider,
)
from aos.planner import PlannerContractError, PlannerCredentialError, PlannerTransientError
from aos.provider_observation import RateLimitObservation, TaskClass
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
    "cloudflare": "CLOUDFLARE_API_TOKEN",
    "openrouter_free": "OPENROUTER_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "huggingface_router": "HF_TOKEN",
    "openai": "OPENAI_API_KEY",
    "openai_paid_safety": "OPENAI_API_KEY",
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
target_base_sha=0000000000000000000000000000000000000000
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
    observation = getattr(exc, "rate_limit_observation", None)
    if observation is not None:
        if observation.classification == "CREDIT_EXHAUSTED":
            return "CREDIT_EXHAUSTED"
        if observation.classification == "QUOTA_EXHAUSTED":
            return "QUOTA_EXHAUSTED"
        if observation.classification == "RATE_LIMITED":
            return "RATE_LIMITED"
    text = str(exc).lower()
    if (
        "credit_exhausted" in text
        or "payment required" in text
        or "insufficient credit" in text
        or "out of credit" in text
        or "monthly included credits" in text
        or ("depleted" in text and "credit" in text)
    ):
        return "CREDIT_EXHAUSTED"
    if isinstance(exc, PlannerCredentialError):
        return "CREDENTIAL_UNAVAILABLE"
    if isinstance(exc, PlannerContractError):
        return "CONTRACT_FAILURE"
    if any(token in text for token in ("401", "403", "unauthorized", "invalid api key", "authentication")):
        return "AUTH_FAILURE"
    if isinstance(exc, PlannerTransientError):
        if "quota" in text or "resource_exhausted" in text:
            return "QUOTA_EXHAUSTED"
        if "429" in text or "rate limit" in text:
            return "RATE_LIMITED"
        if any(token in text for token in ("capacity", "500", "502", "503", "504", "overloaded")):
            return "SERVER_CAPACITY"
        if "timeout" in text:
            return "TIMEOUT"
        return "NETWORK_UNAVAILABLE"
    if isinstance(exc, (ConnectionError, urllib.error.URLError, OSError)):
        return "NETWORK_UNAVAILABLE"
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    return "UNKNOWN"


def provider_runtime_matrix(
    policy_path: Path,
    provider_ids: Optional[list[str]] = None,
) -> Dict[str, Any]:
    _hydrate()
    policy = _load_policy(policy_path)
    allow_paid = bool(policy.get("allow_paid_fallback", False))
    paid_enabled = bool(policy.get("paid_fallback_enabled", False))
    paid_budget_available = (
        float(policy.get("paid_daily_budget_usd", 0.0) or 0.0) > 0
        and float(policy.get("paid_monthly_budget_usd", 0.0) or 0.0) > 0
    )
    selected = set(provider_ids) if provider_ids is not None else None
    providers_cfg = policy.get("providers", {}) if isinstance(policy.get("providers"), dict) else {}
    result: Dict[str, Any] = {}

    for provider_id, cfg in providers_cfg.items():
        if selected is not None and provider_id not in selected:
            continue
        if not isinstance(cfg, dict):
            continue
        is_local = str(cfg.get("cloud_local", "CLOUD")).upper() == "LOCAL"
        if is_local and provider_id == "freellmapi_local":
            env_name = cfg.get("credential_env_var") or "FREELLMAPI_LOCAL_API_KEY"
            credential_present = bool(os.environ.get(env_name))
            started = time.monotonic()
            provider = FreeLLMAPILocalPlannerProvider(
                model=cfg.get("model_id") or "auto:reliable",
                base_url=cfg.get("base_url") or "http://127.0.0.1:3000/v1",
                credential_env_var=env_name,
                max_output_tokens=cfg.get("max_output_tokens") or 2200,
                readiness_timeout_seconds=cfg.get("readiness_timeout_seconds") or 1.5,
            )
            readiness = provider.check_readiness(force=True)
            eligible = readiness.eligible and credential_present
            if not credential_present:
                failure_class = "CREDENTIAL_UNAVAILABLE"
            elif readiness.eligible:
                failure_class = None
            elif readiness.reason == "all_upstreams_rate_limited":
                failure_class = "QUOTA_EXHAUSTED"
            elif readiness.service_available:
                failure_class = "LOCAL_GATEWAY_NO_UPSTREAM_ROUTE"
            else:
                failure_class = "LOCAL_GATEWAY_UNAVAILABLE"
            result[provider_id] = {
                "provider_id": provider_id,
                "credential_present": "YES" if credential_present else "NO",
                "connectivity": "PASS" if readiness.service_available else "FAIL",
                "structured_contract": "NOT_PROBED",
                "readiness_eligible": eligible,
                "local_service_available": readiness.service_available,
                "readiness_reason": readiness.reason,
                "response_id": None,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "failure_class": failure_class,
                "evidence_class": "LOCAL_RUNTIME_PROOF" if readiness.service_available else "NOT_PROVEN",
            }
            continue
        if is_local:
            continue

        billing_class = str(cfg.get("billing_class", "FREE")).upper()
        if billing_class == "PAID" and not (allow_paid and paid_enabled and paid_budget_available):
            # Paid safety net disabled: NOT_PROBED without error
            result[provider_id] = {
                "provider_id": provider_id,
                "credential_present": "YES" if (cfg.get("credential_env_var") and os.environ.get(cfg["credential_env_var"])) else "NO",
                "connectivity": "NOT_PROBED",
                "structured_contract": "NOT_PROBED",
                "response_id": None,
                "latency_ms": None,
                "failure_class": None,
                "evidence_class": "NOT_PROVEN",
            }
            continue

        env_name = cfg.get("credential_env_var") or _ENV.get(provider_id)
        credential_present = bool(env_name and os.environ.get(env_name))
        
        # Check nonsecret required env vars if specified
        nonsecret_vars = cfg.get("additional_nonsecret_env_vars") or []
        nonsecret_missing = [v for v in nonsecret_vars if not os.environ.get(v)]

        row: Dict[str, Any] = {
            "provider_id": provider_id,
            "credential_present": "YES" if credential_present else "NO",
            "connectivity": "FAIL",
            "structured_contract": "FAIL",
            "response_id": None,
            "latency_ms": None,
            "failure_class": "CREDENTIAL_UNAVAILABLE" if not credential_present else ("CONFIGURATION_UNAVAILABLE" if nonsecret_missing else None),
            "evidence_class": "NOT_PROVEN",
            "task_class": TaskClass.SMALL_REASONING.value,
            "model_id": cfg.get("model_id"),
        }

        model = cfg.get("model_id")
        if not credential_present or nonsecret_missing or not model:
            result[provider_id] = row
            continue

        started = time.monotonic()
        try:
            if provider_id in _PROVIDERS:
                provider = _PROVIDERS[provider_id](model=model)
            else:
                provider = GenericOpenAICompatiblePlannerProvider(
                    provider_id=provider_id,
                    model=model,
                    base_url=cfg.get("base_url") or "https://api.openai.com/v1",
                    credential_env_var=env_name,
                    api_protocol=cfg.get("api_protocol") or "OPENAI_CHAT_COMPLETIONS",
                    max_output_tokens=cfg.get("max_output_tokens") or 2200,
                    cloud_local=cfg.get("cloud_local") or "CLOUD",
                    billing_class=billing_class,
                )

            proposal, response_id, _usage = provider.generate_plan(PROBE_PROMPT, PROBE_SCHEMA)
            row["latency_ms"] = int((time.monotonic() - started) * 1000)
            row["connectivity"] = "PASS"
            row["structured_contract"] = "PASS" if (
                isinstance(proposal, dict)
                and proposal.get("project_id") == "synthetic-public-probe"
                and Draft202012Validator(PROBE_SCHEMA).is_valid(proposal)
            ) else "FAIL"
            row["response_id"] = str(response_id)[:160] if response_id else None
            provenance = getattr(provider, "execution_provenance", "UNKNOWN")
            row["evidence_class"] = "LIVE_EXTERNAL_PROOF" if provenance == "LIVE_EXTERNAL" else "LOCAL_RUNTIME_PROOF"
            row["failure_class"] = None if row["structured_contract"] == "PASS" else "CONTRACT_FAILURE"
        except Exception as exc:
            row["latency_ms"] = int((time.monotonic() - started) * 1000)
            row["failure_class"] = _classify(exc)
            row["error_class"] = exc.__class__.__name__
            if isinstance(exc, PlannerContractError):
                row["contract_subtype"] = exc.subtype
                if exc.safe_detail:
                    row["safe_detail"] = exc.safe_detail
                row["message"] = None
            elif isinstance(exc, PlannerTransientError):
                observation = exc.rate_limit_observation
                if observation is not None:
                    observation_payload = observation.to_dict()
                    observation_payload["task_class"] = TaskClass.SMALL_REASONING.value
                    row["rate_limit_observation"] = RateLimitObservation.from_dict(
                        observation_payload
                    ).to_dict()
                row["message"] = None
            else:
                row["message"] = redact_secrets(str(exc))[:300]
        result[provider_id] = row

    if (
        "openai" not in result
        and "OPENAI_API_KEY" in os.environ
        and (selected is None or "openai" in selected)
    ):
        result["openai"] = {
            "provider_id": "openai",
            "credential_present": "YES",
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


def local_reasoning_discovery(
    base_url: str = "http://127.0.0.1:11434",
    required_model: Optional[str] = None,
) -> Dict[str, Any]:
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
        "failure_class": "LOCAL_MODEL_UNAVAILABLE",
        "latency_ms": None,
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
    if required_model and required_model not in result["installed_models"]:
        result["state"] = "UNAVAILABLE_CONFIGURED_MODEL_NOT_INSTALLED"
        return result
    model = required_model or str(result["installed_models"][0])
    started = time.monotonic()
    try:
        provider = OllamaPlannerProvider(model=model, base_url=base_url)
        proposal, _response_id, _usage = provider.generate_plan(PROBE_PROMPT, PROBE_SCHEMA)
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        if (
            isinstance(proposal, dict)
            and proposal.get("project_id") == "synthetic-public-probe"
            and Draft202012Validator(PROBE_SCHEMA).is_valid(proposal)
        ):
            result["structured_output_compatible"] = True
            result["selected_local_fallback"] = model
            result["state"] = "AVAILABLE_APPROVED_INSTALLED_MODEL"
            result["failure_class"] = None
    except Exception as exc:
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        result["failure_class"] = _classify(exc)
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


def run_sanitized_probe(
    policy_path: Path,
    provider_ids: Optional[list[str]] = None,
) -> Dict[str, Any]:
    # Credential hydration is deliberately scoped to the live probe.  Provider
    # secrets loaded from Windows Credential Manager must never leak into later
    # regression tests, checkpoints, evidence, or unrelated child processes.
    snapshot = _snapshot_provider_env()
    try:
        policy = _load_policy(policy_path)
        matrix = provider_runtime_matrix(policy_path, provider_ids=provider_ids)
        configured = policy.get("providers", {}) if isinstance(policy.get("providers"), dict) else {}
        ollama_cfg = configured.get("ollama", {}) if isinstance(configured.get("ollama"), dict) else {}
        probe_local = provider_ids is None or "ollama" in provider_ids
        local = (
            local_reasoning_discovery(
                base_url=str(ollama_cfg.get("base_url") or "http://127.0.0.1:11434"),
                required_model=str(ollama_cfg.get("model_id")) if ollama_cfg.get("model_id") else None,
            )
            if probe_local else {}
        )
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


def probe_enabled_providers(
    policy_path: Path,
    provider_ids: Optional[list[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Run one bounded synthetic live cycle for providers enabled by policy.

    The returned mapping contains only sanitized operational fields. Raw model
    responses, response identifiers, prompts, and credentials are deliberately
    excluded so callers can safely persist it in command-local circuit state.
    """
    policy = _load_policy(policy_path)
    configured = policy.get("providers", {})
    enabled = [
        str(provider_id)
        for provider_id, cfg in configured.items()
        if isinstance(cfg, dict) and cfg.get("enabled") is True
    ] if isinstance(configured, dict) else []
    if provider_ids is not None:
        selected = set(provider_ids)
        enabled = [provider_id for provider_id in enabled if provider_id in selected]
    raw = run_sanitized_probe(policy_path, provider_ids=enabled)
    matrix = raw.get("provider_runtime_matrix", {})
    local = raw.get("local_reasoning", {})
    observed_at = str(raw.get("timestamp") or _dt.datetime.now(_dt.timezone.utc).isoformat())
    results: Dict[str, Dict[str, Any]] = {}
    for provider_id in enabled:
        probe_id = f"probe-{provider_id}-{uuid.uuid4().hex}"
        cfg = configured.get(provider_id, {})
        is_local = str(cfg.get("cloud_local", "CLOUD")).upper() == "LOCAL"
        if is_local:
            if provider_id == "freellmapi_local":
                row = matrix.get(provider_id, {}) if isinstance(matrix, dict) else {}
                available = bool(row.get("local_service_available"))
                credential_available = row.get("credential_present") == "YES"
                passed = bool(row.get("readiness_eligible"))
                results[provider_id] = {
                    "provider_id": provider_id,
                    "credential_available": credential_available,
                    "local_service_available": available,
                    "probe_attempted": True,
                    "probe_id": probe_id,
                    "probe_status": "PASS" if passed else "FAIL",
                    "failure_class": None if passed else (row.get("failure_class") or "LOCAL_GATEWAY_UNAVAILABLE"),
                    "last_observed_at": observed_at,
                    "latency_ms": row.get("latency_ms"),
                    "task_class": TaskClass.SMALL_REASONING.value,
                    "model_id": cfg.get("model_id"),
                    "contract_subtype": row.get("contract_subtype"),
                    "safe_detail": row.get("safe_detail"),
                    "rate_limit_observation": row.get("rate_limit_observation"),
                }
                continue
            available = bool(local.get("service_available"))
            passed = bool(local.get("structured_output_compatible"))
            results[provider_id] = {
                "provider_id": provider_id,
                "credential_available": None,
                "local_service_available": available,
                "probe_attempted": available,
                "probe_id": probe_id if available else None,
                "probe_status": "PASS" if passed else ("FAIL" if available else "NOT_ATTEMPTED"),
                "failure_class": None if passed else (local.get("failure_class") or "LOCAL_MODEL_UNAVAILABLE"),
                "last_observed_at": observed_at,
                "latency_ms": local.get("latency_ms"),
                "task_class": TaskClass.SMALL_REASONING.value,
                "model_id": cfg.get("model_id"),
            }
            continue

        row = matrix.get(provider_id, {}) if isinstance(matrix, dict) else {}
        credential_available = row.get("credential_present") == "YES"
        attempted = credential_available and row.get("connectivity") != "NOT_PROBED"
        passed = (
            row.get("connectivity") == "PASS"
            and row.get("structured_contract") == "PASS"
            and row.get("evidence_class") == "LIVE_EXTERNAL_PROOF"
        )
        results[provider_id] = {
            "provider_id": provider_id,
            "credential_available": credential_available,
            "local_service_available": None,
            "probe_attempted": attempted,
            "probe_id": probe_id if attempted else None,
            "probe_status": "PASS" if passed else ("FAIL" if attempted else "NOT_ATTEMPTED"),
            "failure_class": None if passed else (row.get("failure_class") or "UNKNOWN"),
            "last_observed_at": observed_at,
            "latency_ms": row.get("latency_ms"),
            "task_class": TaskClass.SMALL_REASONING.value,
            "model_id": cfg.get("model_id"),
            "contract_subtype": row.get("contract_subtype"),
            "safe_detail": row.get("safe_detail"),
            "rate_limit_observation": row.get("rate_limit_observation"),
        }
    return results
