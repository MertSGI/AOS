"""Capability proof for the bounded Qwen3-4B Q4_K_M llama.cpp resource."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from aos.process_utils import run_headless
from aos.runtime_store import atomic_json
from aos.validate import validate_document

LLAMA_CPP_ADAPTER_CONTRACT_VERSION = "1.0.0"
QWEN_PROFILE_VERSION = "qwen3-4b-q4_k_m-cpu16gb-v1"
QWEN_MODEL_PATTERN = re.compile(r"qwen3[-_. ]?4b.*q4_k_m.*\.gguf$", re.IGNORECASE)
MAX_MODEL_BYTES = 6 * 1024 * 1024 * 1024


def capability_store_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) / "AOS" / "capabilities" if local else Path.home() / ".aos" / "capabilities"
    return base / "llama-cpp-qwen3-4b.json"


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_llama_cpp_identity(
    command: str = "llama-server",
    *,
    which_resolver: Callable[[str], Optional[str]] = shutil.which,
    runner: Optional[Callable[[list[str]], subprocess.CompletedProcess]] = None,
) -> Optional[Dict[str, str]]:
    path = which_resolver(command) or (command if Path(command).is_file() else None)
    if not path or not Path(path).is_file():
        return None
    try:
        completed = runner([path, "--version"]) if runner else run_headless(
            [path, "--version"], timeout=10
        )
        version = str(completed.stdout or completed.stderr or "").strip().splitlines()[0]
        if completed.returncode or not version:
            return None
        return {
            "path": str(Path(path).resolve()),
            "filename": Path(path).name,
            "sha256": file_sha256(path),
            "version": version[:200],
        }
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def resolve_qwen_model(path: str | Path) -> Optional[Dict[str, Any]]:
    model = Path(path)
    try:
        size = model.stat().st_size
    except OSError:
        return None
    if not model.is_file() or not QWEN_MODEL_PATTERN.search(model.name):
        return None
    if size <= 0 or size > MAX_MODEL_BYTES:
        return None
    return {
        "path": str(model.resolve()),
        "filename": model.name,
        "sha256": file_sha256(model),
        "size_bytes": size,
        "quantization": "Q4_K_M",
        "model_family": "Qwen3-4B",
    }


def resolve_capability_status(
    *,
    executable: Optional[Dict[str, str]] = None,
    model: Optional[Dict[str, Any]] = None,
    store_path: Optional[Path] = None,
) -> str:
    target = store_path or capability_store_path()
    if executable is None or model is None or not target.is_file():
        return "UNPROVEN"
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        validation = validate_document("worker_capability_attestation", value)
        extension = (value.get("extensions") or {}).get("llama_cpp_qwen3_4b", {})
        if (
            validation.is_valid
            and value.get("worker_adapter") == "llama_cpp_qwen3_4b"
            and value.get("adapter_contract_version") == LLAMA_CPP_ADAPTER_CONTRACT_VERSION
            and value.get("executable_sha256") == executable["sha256"]
            and value.get("reported_cli_version") == executable["version"]
            and value.get("capability_status") == "PROVEN"
            and extension.get("model_sha256") == model["sha256"]
            and extension.get("model_size_bytes") == model["size_bytes"]
            and extension.get("benchmark_status") == "PASS"
        ):
            return "PROVEN"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return "UNPROVEN"


def write_capability_attestation(
    *,
    executable: Dict[str, str],
    model: Dict[str, Any],
    benchmark: Dict[str, Any],
    store_path: Optional[Path] = None,
    aos_revision: str = "0" * 40,
) -> Dict[str, Any]:
    """Persist an explicit benchmark result; this function never runs a model itself."""
    passed = bool(
        benchmark.get("structured_json_valid") is True
        and benchmark.get("classification_correct") is True
        and isinstance(benchmark.get("peak_rss_bytes"), int)
        and benchmark["peak_rss_bytes"] <= 12 * 1024 * 1024 * 1024
        and isinstance(benchmark.get("latency_ms"), (int, float))
        and benchmark["latency_ms"] <= 120_000
    )
    safe_benchmark = {
        "benchmark_status": "PASS" if passed else "FAIL",
        "structured_json_valid": benchmark.get("structured_json_valid") is True,
        "classification_correct": benchmark.get("classification_correct") is True,
        "latency_ms": float(benchmark.get("latency_ms", 0)),
        "peak_rss_bytes": int(benchmark.get("peak_rss_bytes", 0)),
        "context_tokens": int(benchmark.get("context_tokens", 0)),
        "output_tokens": int(benchmark.get("output_tokens", 0)),
    }
    profile = {
        "context_size": 4096,
        "max_output_tokens": 512,
        "max_threads": min(8, os.cpu_count() or 1),
        "max_parallel": 1,
        "loopback_only": True,
    }
    attestation = {
        "schema_version": "0.1.0",
        "worker_adapter": "llama_cpp_qwen3_4b",
        "adapter_contract_version": LLAMA_CPP_ADAPTER_CONTRACT_VERSION,
        "executable_filename": executable["filename"],
        "executable_sha256": executable["sha256"],
        "reported_cli_version": executable["version"],
        "runtime_environment_profile_version": QWEN_PROFILE_VERSION,
        "runtime_environment_fingerprint_sha256": hashlib.sha256(json.dumps(
            {"model_sha256": model["sha256"], **profile},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest(),
        "capability_status": "PROVEN" if passed else "UNPROVEN",
        "probe_id": f"LLAMA-QWEN-PROBE-{uuid.uuid4().hex[:12]}",
        "probe_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "aos_revision_used_for_probe": aos_revision,
        "capabilities_proven": [
            "LOCAL_STRUCTURED_REASONING", "QWEN3_4B", "Q4_K_M", "LOOPBACK_ONLY"
        ] if passed else [],
        "limitations": [
            "CLASSIFICATION_TRIAGE_BOUNDED_DECISIONS_ONLY",
            "NO_LONG_HORIZON_AGENTIC_WORK",
            "NO_PRODUCTION_AUTHORITY",
        ],
        "extensions": {"llama_cpp_qwen3_4b": {
            "model_filename": model["filename"],
            "model_sha256": model["sha256"],
            "model_size_bytes": model["size_bytes"],
            "quantization": "Q4_K_M",
            **profile,
            **safe_benchmark,
        }},
    }
    if not validate_document("worker_capability_attestation", attestation).is_valid:
        raise ValueError("Qwen capability attestation failed schema validation")
    atomic_json(store_path or capability_store_path(), attestation)
    return attestation
