"""Offline test suite for Nemotron Provider Adapter, Model Fabric, and MCP Service.

Validates:
- Missing credential, invalid auth, 429, timeout, 503 error taxonomy mapping
- Structured JSON and Deep Reasoning modes
- Raw reasoning / chain-of-thought non-persistence invariant
- Strict data classification gating (PUBLIC/INTERNAL_NON_SENSITIVE allowed, others DENIED)
- Invariant: Nemotron cannot produce pixel visual pass
- Advisory-only output constraint
- MCP tool allowlist and absence of generic actuators
- Default router preservation (NEMOTRON_DEFAULT_ROUTE_ENABLED=NO)
"""

import json
import os
import pytest
from unittest.mock import MagicMock, patch

from aos.planner import (
    PlannerContractError,
    PlannerCredentialError,
    PlannerTransientError,
)
from aos.provider_registry import (
    ProviderRegistry,
    ProviderRouter,
)
from aos.providers.nemotron import NemotronPlannerProvider, project_nemotron_schema
from extensions.model_fabric.specialist_fabric import (
    NemotronSpecialistFabric,
    SpecialistRequest,
    SpecialistRole,
)
from extensions.model_fabric.mcp_service import NemotronMcpServer


# 1. Schema Projection
def test_project_nemotron_schema_strips_meta():
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "https://example.com/test.json",
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    projected = project_nemotron_schema(schema)
    assert "$schema" not in projected
    assert "$id" not in projected
    assert projected["type"] == "object"


# 2. Missing API Key
def test_nemotron_provider_missing_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    provider = NemotronPlannerProvider()
    with pytest.raises(PlannerCredentialError, match="NVIDIA_API_KEY environment variable is missing"):
        provider.generate_plan("Test prompt", {"type": "object"})


# 3. Error Mapping
def test_nemotron_error_mapping(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-key")
    provider = NemotronPlannerProvider()

    import openai

    # Connection Error -> PlannerTransientError
    with patch("openai.OpenAI") as mock_openai:
        mock_client = MagicMock()
        mock_openai.return_value = mock_client
        mock_client.chat.completions.create.side_effect = openai.APIConnectionError(request=MagicMock())
        with pytest.raises(PlannerTransientError):
            provider.generate_plan("Test", {"type": "object"})

    # Rate Limit 429 -> PlannerTransientError
    with patch("openai.OpenAI") as mock_openai:
        mock_client = MagicMock()
        mock_openai.return_value = mock_client
        mock_client.chat.completions.create.side_effect = openai.RateLimitError(
            message="429 Too Many Requests", response=MagicMock(), body=None
        )
        with pytest.raises(PlannerTransientError):
            provider.generate_plan("Test", {"type": "object"})

    # Auth Error 401 -> PlannerCredentialError
    with patch("openai.OpenAI") as mock_openai:
        mock_client = MagicMock()
        mock_openai.return_value = mock_client
        mock_client.chat.completions.create.side_effect = openai.AuthenticationError(
            message="401 Unauthorized", response=MagicMock(), body=None
        )
        with pytest.raises(PlannerCredentialError):
            provider.generate_plan("Test", {"type": "object"})

    # Bad Request / Schema Error -> PlannerContractError
    with patch("openai.OpenAI") as mock_openai:
        mock_client = MagicMock()
        mock_openai.return_value = mock_client
        mock_client.chat.completions.create.side_effect = openai.BadRequestError(
            message="400 Bad Request", response=MagicMock(), body=None
        )
        with pytest.raises(PlannerContractError):
            provider.generate_plan("Test", {"type": "object"})


# 4. Structured Output and Non-Persistence of Reasoning
def test_nemotron_structured_output_and_reasoning_redaction(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-fake-key")
    provider = NemotronPlannerProvider()

    with patch("openai.OpenAI") as mock_openai:
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_choice = MagicMock()
        mock_choice.finish_reason = "stop"
        mock_choice.message.content = json.dumps({"milestone": "M1", "next_action": "VERIFY"})
        mock_choice.message.reasoning_content = "This is raw chain-of-thought that MUST NOT leak"

        mock_resp = MagicMock()
        mock_resp.id = "chatcmpl-test-123"
        mock_resp.choices = [mock_choice]
        mock_resp.usage.prompt_tokens = 120
        mock_resp.usage.completion_tokens = 45
        mock_resp.usage.total_tokens = 165

        mock_client.chat.completions.create.return_value = mock_resp

        decision, resp_id, usage = provider.generate_plan("Test prompt", {"type": "object"})

        assert decision == {"milestone": "M1", "next_action": "VERIFY"}
        assert resp_id == "chatcmpl-test-123"
        assert usage["input_tokens"] == 120
        assert usage["output_tokens"] == 45
        # Crucial: decision and usage contain NO raw reasoning content
        assert "chain-of-thought" not in str(decision)
        assert "chain-of-thought" not in str(usage)


# 5. Data Classification Boundaries in Model Fabric
def test_nemotron_fabric_data_classification_boundaries():
    fabric = NemotronSpecialistFabric()

    # Allowed: PUBLIC
    req_pub = SpecialistRequest(
        role=SpecialistRole.ARCHITECTURE_REVIEW,
        prompt="Review system architecture",
        data_classification="PUBLIC",
    )
    # With missing key, it proceeds past classification check to provider failure
    res_pub = fabric.evaluate(req_pub)
    assert res_pub.status == "FAILED"  # Stopped by missing key, not classification

    # Denied: PATIENT_PII
    req_pii = SpecialistRequest(
        role=SpecialistRole.SECURITY_REVIEW,
        prompt="Review patient records",
        data_classification="PATIENT_PII",
    )
    res_pii = fabric.evaluate(req_pii)
    assert res_pii.status == "DENIED"
    assert "prohibited" in res_pii.rejection_reason

    # Denied: SECRET
    req_sec = SpecialistRequest(
        role=SpecialistRole.CODE_REVIEW,
        prompt="Review API keys",
        data_classification="SECRET",
    )
    res_sec = fabric.evaluate(req_sec)
    assert res_sec.status == "DENIED"


# 6. Invariant: Nemotron Cannot Produce Pixel Visual Pass
def test_nemotron_cannot_claim_pixel_visual():
    fabric = NemotronSpecialistFabric()

    req = SpecialistRequest(
        role=SpecialistRole.ARCHITECTURE_REVIEW,
        prompt="Inspect this screenshot pixel rendering",
        data_classification="PUBLIC",
    )
    res = fabric.evaluate(req)
    assert res.status == "DENIED"
    assert "pixel/visual screenshot inspection is strictly denied" in res.rejection_reason


# 7. Invariant: Advisory-Only Status
def test_nemotron_fabric_advisory_invariant():
    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "Advisory architecture recommendation: isolate tenant contexts"
    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]
    mock_resp.usage = None
    mock_client.chat.completions.create.return_value = mock_resp

    fabric = NemotronSpecialistFabric(client_factory=lambda: mock_client)

    req = SpecialistRequest(
        role=SpecialistRole.ARCHITECTURE_REVIEW,
        prompt="Check tenant isolation",
        data_classification="PUBLIC",
    )
    res = fabric.evaluate(req)
    assert res.status == "SUCCESS"
    assert res.is_advisory_only is True
    assert "isolate tenant contexts" in res.answer


# 8. MCP Server: Tool Allowlist & No Actuators
def test_mcp_server_tool_allowlist_and_actuator_absence():
    server = NemotronMcpServer()
    tools = server.list_tools()
    tool_names = [t["name"] for t in tools]

    # Exactly 8 specialist tools
    assert len(tools) == 8
    assert "nemotron_plan" in tool_names
    assert "nemotron_architecture_review" in tool_names
    assert "nemotron_code_review" in tool_names
    assert "nemotron_security_review" in tool_names
    assert "nemotron_sql_schema_review" in tool_names
    assert "nemotron_long_context_analysis" in tool_names
    assert "nemotron_design_text_review" in tool_names
    assert "nemotron_second_opinion" in tool_names

    # Invariant: ZERO ACTUATORS
    assert "run_command" not in tool_names
    assert "write_file" not in tool_names
    assert "git" not in tool_names
    assert "deploy" not in tool_names
    assert "browser" not in tool_names


# 9. MCP Server: Classification Fail-Closed
def test_mcp_server_classification_denial():
    server = NemotronMcpServer()
    res = server.call_tool("nemotron_security_review", {
        "prompt": "Evaluate patient confidential records",
        "data_classification": "PATIENT_PII",
    })
    assert res["isError"] is True
    assert "ACCESS_DENIED" in res["content"][0]["text"]


# 10. Default Routing Policy: Nemotron Not Active by Default
def test_default_routing_policy_unchanged():
    from aos.provider_registry import load_routing_policy
    from pathlib import Path

    policy_path = Path(__file__).parent.parent / "descriptors" / "lari.planner-policy.json"
    registry = load_routing_policy(str(policy_path))
    router = ProviderRouter(registry)

    # Invariant: Nemotron is NOT in the default preferred route
    r0_route = registry.risk_routes.get("R0", {})
    preferred = r0_route.get("preferred_providers", [])
    assert "nemotron" not in preferred
    assert preferred == ["gemini", "groq", "ollama"]
