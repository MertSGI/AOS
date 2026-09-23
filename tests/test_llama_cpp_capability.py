import json

from aos.workers.llama_cpp_probe import (
    resolve_capability_status,
    resolve_llama_cpp_identity,
    resolve_qwen_model,
    write_capability_attestation,
)


def test_exact_binary_and_model_identity_are_hash_bound(tmp_path):
    executable = tmp_path / "llama-server.exe"
    executable.write_bytes(b"llama binary fixture")
    identity = resolve_llama_cpp_identity(
        str(executable),
        which_resolver=lambda command: str(executable),
        runner=lambda argv: __import__("subprocess").CompletedProcess(argv, 0, "llama.cpp test", ""),
    )
    model_path = tmp_path / "Qwen3-4B-Q4_K_M.gguf"
    model_path.write_bytes(b"model fixture")
    model = resolve_qwen_model(model_path)
    assert identity["sha256"]
    assert model["quantization"] == "Q4_K_M"
    assert resolve_qwen_model(tmp_path / "Qwen3-4B-Q8.gguf") is None


def test_attestation_requires_bounded_successful_benchmark_and_detects_drift(tmp_path):
    executable = {
        "path": "llama-test",
        "filename": "llama-test",
        "sha256": "a" * 64,
        "version": "llama.cpp test",
    }
    model = {
        "path": "model.gguf",
        "filename": "Qwen3-4B-Q4_K_M.gguf",
        "sha256": "b" * 64,
        "size_bytes": 2_500_000_000,
        "quantization": "Q4_K_M",
        "model_family": "Qwen3-4B",
    }
    store = tmp_path / "qwen.json"
    value = write_capability_attestation(
        executable=executable,
        model=model,
        benchmark={
            "structured_json_valid": True,
            "classification_correct": True,
            "peak_rss_bytes": 6_000_000_000,
            "latency_ms": 10_000,
            "context_tokens": 1024,
            "output_tokens": 64,
        },
        store_path=store,
        aos_revision="c" * 40,
    )
    assert value["capability_status"] == "PROVEN"
    assert resolve_capability_status(executable=executable, model=model, store_path=store) == "PROVEN"
    assert "prompt" not in json.dumps(value).lower()

    drifted = dict(model, sha256="d" * 64)
    assert resolve_capability_status(executable=executable, model=drifted, store_path=store) == "UNPROVEN"


def test_failed_or_over_memory_benchmark_remains_unproven(tmp_path):
    executable = {"filename": "llama", "sha256": "e" * 64, "version": "v", "path": "llama"}
    model = {
        "filename": "Qwen3-4B-Q4_K_M.gguf", "sha256": "f" * 64,
        "size_bytes": 2_000_000_000, "path": "model", "quantization": "Q4_K_M",
        "model_family": "Qwen3-4B",
    }
    value = write_capability_attestation(
        executable=executable,
        model=model,
        benchmark={
            "structured_json_valid": True, "classification_correct": True,
            "peak_rss_bytes": 13 * 1024 * 1024 * 1024,
            "latency_ms": 1000, "context_tokens": 512, "output_tokens": 32,
        },
        store_path=tmp_path / "failed.json",
    )
    assert value["capability_status"] == "UNPROVEN"
