import json
from pathlib import Path

import pytest

from extensions.autonomy_fabric.execution_backend import (
    ExecutionCapability,
    ExecutionRequest,
)
from extensions.autonomy_fabric.llama_cpp_reasoning_backend import (
    LlamaCppQwenReasoningBackend,
    build_llama_server_argv,
)


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["class", "confidence"],
    "properties": {
        "class": {"enum": ["safe", "review"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


def _request(tmp_path: Path, **payload):
    return ExecutionRequest(
        task_id="classify-1",
        project_id="project-1",
        workspace=str(tmp_path),
        operation_class="REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH-1",
        payload={"prompt": "Classify this bounded case", "schema": SCHEMA, **payload},
    )


def _backend(transport, *, status="TEST_DOUBLE", healthy=True):
    return LlamaCppQwenReasoningBackend(
        capability_status_provider=lambda: status,
        health_reader=lambda: healthy,
        completion_transport=transport,
    )


def test_bounded_structured_completion_is_transient_and_evidence_is_redacted(tmp_path):
    seen = {}

    def transport(payload, timeout):
        seen.update({"payload": payload, "timeout": timeout})
        return {
            "choices": [{"message": {"content": json.dumps({
                "class": "safe", "confidence": 0.9,
            })}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        }

    result = _backend(transport).execute(_request(tmp_path))
    assert result.status == "SUCCESS"
    assert result.transient_structured_output == {"class": "safe", "confidence": 0.9}
    assert seen["payload"]["temperature"] == 0
    assert seen["payload"]["seed"] == 1
    assert seen["payload"]["max_tokens"] == 512
    assert seen["payload"]["stream"] is False
    serialized = json.dumps(result.to_dict())
    assert "Classify this bounded case" not in serialized
    assert '"class": "safe"' not in serialized
    assert result.resource_usage == {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}


def test_unproven_or_unhealthy_resource_never_invokes_model(tmp_path):
    calls = []
    transport = lambda payload, timeout: calls.append(payload)
    unproven = _backend(transport, status="UNPROVEN")
    result = unproven.execute(_request(tmp_path))
    assert result.status == "DEGRADED"
    assert result.availability.state.value == "CONTRACT_FAILURE"

    unhealthy = _backend(transport, healthy=False)
    result = unhealthy.execute(_request(tmp_path))
    assert result.availability.state.value == "TEMPORARILY_UNAVAILABLE"
    assert calls == []


@pytest.mark.parametrize("response", [
    {},
    {"choices": []},
    {"choices": [{"message": {"content": "not-json"}}]},
    {"choices": [{"message": {"content": '{"class":"unsafe","confidence":2}'}}]},
    {"choices": [{"message": {"content": '{"class":"safe","confidence":0.9,"extra":1}'}}]},
])
def test_malformed_or_schema_invalid_output_fails_closed(tmp_path, response):
    result = _backend(lambda payload, timeout: response).execute(_request(tmp_path))
    assert result.status == "DEGRADED"
    assert result.sanitized_errors == ["QWEN_STRUCTURED_CONTRACT_FAILURE"]
    assert "not-json" not in json.dumps(result.to_dict())


def test_prompt_schema_output_and_timeout_bounds_are_enforced_before_transport(tmp_path):
    calls = []
    backend = _backend(lambda payload, timeout: calls.append(payload))
    assert backend.execute(_request(tmp_path, prompt="x" * 12_001)).sanitized_errors == ["QWEN_PROMPT_BOUND"]
    assert backend.execute(_request(tmp_path, schema={"description": "x" * 16_001})).sanitized_errors == ["QWEN_SCHEMA_BOUND"]
    assert backend.execute(_request(tmp_path, max_output_tokens=513)).sanitized_errors == ["QWEN_OUTPUT_BOUND"]
    assert calls == []


def test_endpoint_must_be_loopback():
    with pytest.raises(ValueError, match="loopback"):
        LlamaCppQwenReasoningBackend(base_url="https://example.com")


def test_server_argv_is_cpu_bounded_loopback_and_exact_model(tmp_path):
    model = tmp_path / "Qwen3-4B-Q4_K_M.gguf"
    model.write_bytes(b"test model fixture")
    argv = build_llama_server_argv("llama-server", str(model), port=18080)
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--ctx-size") + 1] == "4096"
    assert argv[argv.index("--n-predict") + 1] == "512"
    assert argv[argv.index("--parallel") + 1] == "1"
    with pytest.raises(ValueError, match="exact Qwen"):
        build_llama_server_argv("llama-server", str(tmp_path / "missing.gguf"))


def test_host_registers_local_qwen_ahead_of_cloud_reasoning(tmp_path):
    from aos.autonomous_host import build_execution_router

    policy = Path(__file__).resolve().parents[3] / "descriptors" / "nemotron.planner-policy.json"
    router = build_execution_router(policy, tmp_path / "runtime")
    backend = router.get_backend("qwen3_4b_llama_cpp")
    assert isinstance(backend, LlamaCppQwenReasoningBackend)
    assert backend.cost.value == "FREE_LOCAL"
