"""Deterministic loopback integration tests for the FreeLLMAPI meta-provider."""
from __future__ import annotations

import json
import socket
import threading
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

from aos.autonomous_host import ProviderFailoverReasoningBackend
from aos.planner import PlannerContractError, PlannerTransientError
from aos.provider_circuit import ProviderCircuitBreakerRegistry
from aos.provider_registry import ProviderRegistry, ProviderRouter
from aos.providers.freellmapi_local import (
    FreeLLMAPILocalPlannerProvider,
    sanitize_routing_headers,
)
from aos.validate import validate_document
from extensions.autonomy_fabric.execution_backend import ExecutionCapability, ExecutionRequest


CANONICAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "required_nullable"],
    "properties": {
        "decision": {"type": "string"},
        "required_nullable": {"type": ["string", "null"]},
        "optional_text": {"type": "string"},
    },
}
VALID_PLAN = {
    "decision": "SHADOW_ACCEPT",
    "required_nullable": None,
    "optional_text": None,
}


class _GatewayState:
    def __init__(self) -> None:
        self.readiness_status = 200
        self.readiness_body: Dict[str, Any] = {"status": "ok", "ready_upstreams": 1}
        self.liveness_status = 200
        self.liveness_body: Dict[str, Any] = {"status": "ok", "database": "ok", "encryption": "ok"}
        self.completion_status = 200
        self.completion_content = json.dumps(VALID_PLAN)
        self.completion_error: Dict[str, Any] = {
            "error": {"type": "provider_error", "code": "upstream_failed", "message": "unavailable"}
        }
        self.response_headers: Dict[str, str] = {}
        self.readiness_calls = 0
        self.liveness_calls = 0
        self.completion_calls = 0
        self.authorization: str | None = None
        self.request_model: str | None = None


@contextmanager
def _fake_gateway() -> Iterator[tuple[_GatewayState, str]]:
    state = _GatewayState()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send_json(self, status: int, body: Dict[str, Any]) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for key, value in state.response_headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            if self.path == "/livez":
                state.liveness_calls += 1
                self._send_json(state.liveness_status, state.liveness_body)
                return
            if self.path != "/readyz":
                self._send_json(404, {"error": {"type": "not_found"}})
                return
            state.readiness_calls += 1
            self._send_json(state.readiness_status, state.readiness_body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            if self.path != "/v1/chat/completions":
                self._send_json(404, {"error": {"type": "not_found"}})
                return
            state.completion_calls += 1
            state.authorization = self.headers.get("Authorization")
            size = int(self.headers.get("Content-Length", "0"))
            request_body = json.loads(self.rfile.read(size).decode("utf-8"))
            state.request_model = request_body.get("model")
            if state.completion_status != 200:
                self._send_json(state.completion_status, state.completion_error)
                return
            self._send_json(
                200,
                {
                    "id": "chatcmpl-local-test",
                    "object": "chat.completion",
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": state.completion_content},
                    }],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
                },
            )

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield state, f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _unused_loopback_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


def _policy(base_url: str) -> Dict[str, Any]:
    return {
        "routing_mode": "DETERMINISTIC",
        "allow_paid_fallback": False,
        "paid_fallback_enabled": False,
        "paid_daily_budget_usd": 0,
        "paid_monthly_budget_usd": 0,
        "allow_provider_fallback": True,
        "data_classification": "PUBLIC",
        "risk_routes": {
            "R0": {"preferred_providers": ["direct_free", "freellmapi_local", "openai_paid_safety"]}
        },
        "providers": {
            "direct_free": {
                "provider_id": "direct_free",
                "model_id": "direct-model",
                "credential_env_var": None,
                "billing_class": "FREE",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
            },
            "freellmapi_local": {
                "provider_id": "freellmapi_local",
                "model_id": "auto:reliable",
                "credential_env_var": "FREELLMAPI_LOCAL_API_KEY",
                "billing_class": "FREE",
                "structured_output": True,
                "cloud_local": "LOCAL",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
                "adapter_type": "FREELLMAPI_LOCAL",
                "base_url": base_url,
            },
            "openai_paid_safety": {
                "provider_id": "openai_paid_safety",
                "model_id": "paid-model",
                "credential_env_var": None,
                "billing_class": "PAID",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
            },
        },
    }


def _request(tmp_path: Path) -> ExecutionRequest:
    return ExecutionRequest(
        task_id="freellmapi-test",
        project_id="aos",
        workspace=str(tmp_path),
        operation_class="REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH-FREELLMAPI-TEST",
        payload={"prompt": "test", "schema": CANONICAL_SCHEMA, "risk_class": "R0"},
    )


class _DirectProvider:
    execution_provenance = "LIVE_EXTERNAL"

    def __init__(self, *, fail: bool = False, failure: str = "503 capacity") -> None:
        self.fail = fail
        self.failure = failure

    def generate_plan(self, _prompt: str, _schema: Dict[str, Any]):
        if self.fail:
            raise PlannerTransientError(self.failure)
        return {"decision": "DIRECT", "required_nullable": None}, "direct-response", None


class _RecoveryProvider:
    execution_provenance = "LOCAL_OFFLINE"

    def generate_plan(self, _prompt: str, _schema: Dict[str, Any]):
        return {"decision": "RECOVERY", "required_nullable": None}, "recovery-response", None


def test_loopback_boundary_rejects_nonlocal_urls() -> None:
    with pytest.raises(ValueError, match="loopback"):
        FreeLLMAPILocalPlannerProvider(base_url="http://192.168.1.20:3000/v1")
    with pytest.raises(ValueError, match="loopback"):
        FreeLLMAPILocalPlannerProvider(base_url="https://127.0.0.1:3000/v1")


def test_absent_gateway_is_truthfully_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREELLMAPI_LOCAL_API_KEY", "not-a-real-key")
    provider = FreeLLMAPILocalPlannerProvider(
        base_url=_unused_loopback_url(),
        readiness_timeout_seconds=0.2,
        request_timeout_seconds=1,
    )
    readiness = provider.check_readiness(force=True)
    assert readiness.service_available is False
    assert readiness.eligible is False
    assert readiness.reason == "local_gateway_unavailable"
    with pytest.raises(PlannerTransientError, match="LOCAL_GATEWAY_UNAVAILABLE"):
        provider.generate_plan("test", CANONICAL_SCHEMA)


def test_liveness_is_a_separate_non_inference_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREELLMAPI_LOCAL_API_KEY", "not-a-real-key")
    with _fake_gateway() as (gateway, base_url):
        provider = FreeLLMAPILocalPlannerProvider(base_url=base_url)
        liveness = provider.check_liveness()
    assert liveness.service_available is True
    assert liveness.reason == "live"
    assert gateway.liveness_calls == 1
    assert gateway.readiness_calls == 0
    assert gateway.completion_calls == 0


def test_reachable_gateway_is_eligible_and_preserves_nullable_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FREELLMAPI_LOCAL_API_KEY", "not-a-real-key")
    with _fake_gateway() as (gateway, base_url):
        provider = FreeLLMAPILocalPlannerProvider(base_url=base_url)
        readiness = provider.check_readiness(force=True)
        assert readiness.eligible is True
        proposal, response_id, usage = provider.generate_plan("test", CANONICAL_SCHEMA)

    assert proposal == {"decision": "SHADOW_ACCEPT", "required_nullable": None}
    assert response_id == "chatcmpl-local-test"
    assert usage == {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}
    assert gateway.request_model == "auto:reliable"
    assert gateway.readiness_calls == 1
    assert gateway.completion_calls == 1


def test_reachable_gateway_without_upstream_route_is_distinct_from_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FREELLMAPI_LOCAL_API_KEY", "not-a-real-key")
    with _fake_gateway() as (gateway, base_url):
        gateway.readiness_status = 503
        gateway.readiness_body = {"status": "unavailable", "reason": "no_upstreams_configured"}
        provider = FreeLLMAPILocalPlannerProvider(base_url=base_url)
        readiness = provider.check_readiness(force=True)
        assert readiness.service_available is True
        assert readiness.eligible is False
        assert readiness.reason == "no_upstreams_configured"
        with pytest.raises(PlannerTransientError, match="LOCAL_GATEWAY_NO_UPSTREAM_ROUTE"):
            provider.generate_plan("test", CANONICAL_SCHEMA)
    assert gateway.completion_calls == 0


def test_rate_limited_readiness_preserves_healthy_local_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FREELLMAPI_LOCAL_API_KEY", "not-a-real-key")
    with _fake_gateway() as (gateway, base_url):
        gateway.readiness_status = 503
        gateway.readiness_body = {"status": "unavailable", "reason": "all_upstreams_rate_limited"}
        provider = FreeLLMAPILocalPlannerProvider(base_url=base_url)
        readiness = provider.check_readiness(force=True)
        assert readiness.service_available is True
        assert readiness.eligible is False
        assert readiness.reason == "all_upstreams_rate_limited"
        with pytest.raises(PlannerTransientError, match="QUOTA_EXHAUSTED"):
            provider.generate_plan("test", CANONICAL_SCHEMA)
    assert gateway.completion_calls == 0


def test_malformed_planner_json_is_contract_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREELLMAPI_LOCAL_API_KEY", "not-a-real-key")
    with _fake_gateway() as (gateway, base_url):
        gateway.completion_content = "{not valid json"
        provider = FreeLLMAPILocalPlannerProvider(base_url=base_url)
        with pytest.raises(PlannerContractError, match="not valid JSON"):
            provider.generate_plan("test", CANONICAL_SCHEMA)


def test_backend_records_malformed_result_as_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FREELLMAPI_LOCAL_API_KEY", "not-a-real-key")
    with _fake_gateway() as (gateway, base_url):
        gateway.completion_content = "{not valid json"
        policy = _policy(base_url)
        policy["risk_routes"]["R0"]["preferred_providers"] = ["freellmapi_local", "openai_paid_safety"]
        backend = ProviderFailoverReasoningBackend(
            ProviderRouter(ProviderRegistry(policy)),
            provider_factory=lambda provider_id, _model_id: (
                FreeLLMAPILocalPlannerProvider(base_url=base_url)
                if provider_id == "freellmapi_local"
                else pytest.fail("paid provider must remain excluded")
            ),
            circuit_registry=ProviderCircuitBreakerRegistry(tmp_path / "circuits.json"),
        )
        result = backend.execute(_request(tmp_path))
    assert result.evidence_payload["provider_attempts"][0]["error_class"] == "CONTRACT_FAILURE"


def test_routing_metadata_is_captured_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREELLMAPI_LOCAL_API_KEY", "not-a-real-key")
    with _fake_gateway() as (gateway, base_url):
        gateway.response_headers = {
            "X-Routed-Via": "groq/openai/gpt-oss-120b",
            "X-Fallback-Attempts": "2",
            "X-Fallback-Trail": "groq/model-a key1=rate_limited; cerebras/model-b key2=upstream",
        }
        provider = FreeLLMAPILocalPlannerProvider(base_url=base_url)
        provider.generate_plan("test", CANONICAL_SCHEMA)

    assert provider.last_routing_metadata == {
        "routed_via": "groq/openai/gpt-oss-120b",
        "routed_provider_id": "groq",
        "routed_model_id": "openai/gpt-oss-120b",
        "fallback_attempts": 2,
        "fallback_trail": "groq/model-a key1=rate_limited; cerebras/model-b key2=upstream",
    }
    assert len(provider.last_routing_metadata["fallback_trail"]) <= 768


def test_untrusted_routing_headers_are_dropped() -> None:
    marker = "unit-" + uuid.uuid4().hex
    metadata = sanitize_routing_headers({
        "X-Routed-Via": "not a valid route",
        "X-Fallback-Attempts": "999999",
        "X-Fallback-Trail": f"Authorization Bearer {marker}",
        "X-Arbitrary": marker,
    })
    assert metadata == {}
    assert marker not in json.dumps(metadata)


def test_gateway_quota_is_not_classified_as_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FREELLMAPI_LOCAL_API_KEY", "not-a-real-key")
    with _fake_gateway() as (gateway, base_url):
        gateway.completion_status = 429
        gateway.completion_error = {
            "error": {"type": "rate_limit_error", "code": "rate_limit_exceeded", "message": "secret body"}
        }
        policy = _policy(base_url)
        policy["risk_routes"]["R0"]["preferred_providers"] = ["freellmapi_local", "openai_paid_safety"]
        calls = []

        def factory(provider_id: str, _model_id: str):
            calls.append(provider_id)
            if provider_id == "freellmapi_local":
                return FreeLLMAPILocalPlannerProvider(base_url=base_url)
            pytest.fail("paid provider must remain excluded")

        backend = ProviderFailoverReasoningBackend(
            ProviderRouter(ProviderRegistry(policy)),
            provider_factory=factory,
            circuit_registry=ProviderCircuitBreakerRegistry(tmp_path / "circuits.json"),
        )
        result = backend.execute(_request(tmp_path))

    attempt = result.evidence_payload["provider_attempts"][0]
    assert attempt["error_class"] == "RATE_LIMITED"
    assert attempt["error_class"] != "CONTRACT_FAILURE"
    assert calls == ["freellmapi_local"]


def test_direct_route_failure_selects_meta_provider_and_exposes_upstream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway_key = "unit-" + uuid.uuid4().hex
    monkeypatch.setenv("FREELLMAPI_LOCAL_API_KEY", gateway_key)
    with _fake_gateway() as (gateway, base_url):
        gateway.response_headers = {
            "X-Routed-Via": "groq/openai/gpt-oss-120b",
            "X-Fallback-Attempts": "1",
            "X-Fallback-Trail": "cerebras/model-b key1=rate_limited",
        }
        calls = []

        def factory(provider_id: str, _model_id: str):
            calls.append(provider_id)
            if provider_id == "direct_free":
                return _DirectProvider(fail=True)
            if provider_id == "freellmapi_local":
                return FreeLLMAPILocalPlannerProvider(base_url=base_url)
            pytest.fail("paid provider must remain excluded")

        journal = tmp_path / "attempts.jsonl"
        backend = ProviderFailoverReasoningBackend(
            ProviderRouter(ProviderRegistry(_policy(base_url))),
            provider_factory=factory,
            attempt_journal=journal,
            circuit_registry=ProviderCircuitBreakerRegistry(tmp_path / "circuits.json"),
        )
        result = backend.execute(_request(tmp_path))

    assert result.status == "SUCCESS"
    assert result.evidence_payload["provider_route"] == "freellmapi_local"
    assert calls == ["direct_free", "freellmapi_local"]
    successful_attempt = result.evidence_payload["provider_attempts"][-1]
    assert successful_attempt["routed_provider_id"] == "groq"
    assert successful_attempt["routed_model_id"] == "openai/gpt-oss-120b"
    assert successful_attempt["fallback_attempts"] == 1
    serialized = json.dumps(result.evidence_payload) + journal.read_text(encoding="utf-8")
    assert gateway_key not in serialized
    assert gateway.authorization == f"Bearer {gateway_key}"


def test_healthy_direct_route_does_not_probe_or_call_meta_provider(tmp_path: Path) -> None:
    calls = []

    def factory(provider_id: str, _model_id: str):
        calls.append(provider_id)
        if provider_id == "direct_free":
            return _DirectProvider()
        pytest.fail("freellmapi_local must not be touched while the direct route is healthy")

    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy(_unused_loopback_url()))),
        provider_factory=factory,
        circuit_registry=ProviderCircuitBreakerRegistry(tmp_path / "circuits.json"),
    )
    result = backend.execute(_request(tmp_path))
    assert result.status == "SUCCESS"
    assert result.evidence_payload["provider_route"] == "direct_free"
    assert calls == ["direct_free"]


def test_meta_provider_failure_falls_through_to_eligible_recovery_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FREELLMAPI_LOCAL_API_KEY", "not-a-real-key")
    policy = _policy(_unused_loopback_url())
    policy["providers"]["recovery_local"] = {
        "provider_id": "recovery_local",
        "model_id": "recovery-model",
        "credential_env_var": None,
        "billing_class": "LOCAL",
        "structured_output": True,
        "cloud_local": "LOCAL",
        "enabled": True,
        "allowed_data_classifications": ["PUBLIC"],
    }
    policy["risk_routes"]["R0"]["preferred_providers"] = [
        "freellmapi_local",
        "recovery_local",
        "openai_paid_safety",
    ]
    calls = []

    def factory(provider_id: str, _model_id: str):
        calls.append(provider_id)
        if provider_id == "freellmapi_local":
            return FreeLLMAPILocalPlannerProvider(
                base_url=policy["providers"]["freellmapi_local"]["base_url"],
                readiness_timeout_seconds=0.2,
                request_timeout_seconds=1,
            )
        if provider_id == "recovery_local":
            return _RecoveryProvider()
        pytest.fail("paid provider must remain excluded")

    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(policy)),
        provider_factory=factory,
        circuit_registry=ProviderCircuitBreakerRegistry(tmp_path / "circuits.json"),
    )
    result = backend.execute(_request(tmp_path))
    assert result.status == "SUCCESS"
    assert result.evidence_payload["proposal"]["decision"] == "RECOVERY"
    assert result.evidence_payload["provider_attempts"][0]["error_class"] == "LOCAL_GATEWAY_UNAVAILABLE"
    assert calls == ["freellmapi_local", "recovery_local"]


def test_default_policy_preserves_direct_routes_and_disables_paid() -> None:
    root = Path(__file__).resolve().parents[1]
    policy = json.loads((root / "descriptors" / "nemotron.planner-policy.json").read_text(encoding="utf-8"))
    preferred = policy["risk_routes"]["R0"]["preferred_providers"]
    meta_index = preferred.index("freellmapi_local")
    assert preferred[:meta_index] == [
        "nemotron",
        "gemini",
        "groq",
        "cloudflare",
        "openrouter_free",
        "cerebras",
        "huggingface_router",
    ]
    assert preferred[meta_index + 1] == "ollama"
    assert policy["providers"]["freellmapi_local"]["model_id"] == "auto:reliable"
    assert policy["allow_paid_fallback"] is False
    assert policy["paid_fallback_enabled"] is False
    assert policy["paid_daily_budget_usd"] == 0
    assert policy["paid_monthly_budget_usd"] == 0
    validation = validate_document("planner_routing_policy", policy)
    assert validation.is_valid, [error.to_dict() for error in validation.errors]


def test_existing_credit_exhausted_semantics_are_unchanged(tmp_path: Path) -> None:
    policy = _policy(_unused_loopback_url())
    policy["risk_routes"]["R0"]["preferred_providers"] = ["direct_free", "openai_paid_safety"]
    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(policy)),
        provider_factory=lambda _provider_id, _model_id: _DirectProvider(
            fail=True, failure="CREDIT_EXHAUSTED payment required"
        ),
        circuit_registry=ProviderCircuitBreakerRegistry(tmp_path / "circuits.json"),
    )
    result = backend.execute(_request(tmp_path))
    assert result.evidence_payload["provider_attempts"][0]["error_class"] == "CREDIT_EXHAUSTED"


def test_production_remains_no_go() -> None:
    root = Path(__file__).resolve().parents[1]
    state = json.loads(
        (root / "docs" / "codex" / "freellmapi-integration" / "STATE.json").read_text(encoding="utf-8")
    )
    assert state["PRODUCTION"] == "NO_GO"
    assert state["LIVE_RUNTIME_CHANGED"] is False
    assert state["PROTECTED_LINEAGES_CHANGED"] is False
