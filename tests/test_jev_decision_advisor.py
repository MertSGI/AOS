import datetime as dt
import json

import pytest

from aos.decision_advisor import (
    DecisionPrimitive, DecisionQuestion, DecisionRequest, DecisionStatus, NullDecisionAdvisor,
)
from aos.decision_policy import DecisionPolicy
from aos.jev_decision_advisor import JevDecisionAdvisor
from aos.resource_ledger import ResourceLedger


NOW = dt.datetime(2026, 9, 24, tzinfo=dt.timezone.utc)


def _policy(**overrides):
    value = {
        "enabled": True,
        "model": "jev-1.13.0",
        "paid_budget_usd": 0.0,
        "allow_paid_fallback": False,
        "allowed_risk_classes": ["R0", "R1"],
        "forbidden_authorities": [
            "PRODUCTION", "DESTRUCTIVE", "PAYMENT", "SECURITY_OVERRIDE",
            "PROJECT_COMPLETION", "PROTECTED_LINEAGE",
        ],
        "confidence_threshold": 0.6,
        "routes": [{
            "route_id": "vercel_jev_promotional", "cost_class": "PROMOTIONAL_FREE",
            "not_after": "2026-09-25T23:59:59Z",
            "price_observed_at": "2026-09-23T00:00:00Z",
            "zero_price_observed": True,
        }],
    }
    value.update(overrides)
    return DecisionPolicy(value, now=NOW)


def _request(**overrides):
    value = dict(
        request_id="decision-1", objective_id="objective-1", task_class="small_reasoning",
        decision_kind="RESOURCE_RANKING", state={"fact": "PRIVATE_STATE_SHOULD_NOT_PERSIST"},
        questions={
            "route": DecisionQuestion(DecisionPrimitive.CHOICE, "Choose", ("local", "free")),
            "fit": DecisionQuestion(DecisionPrimitive.SCORE, "Score fit", levels=("bad", "ok", "good")),
            "keep": DecisionQuestion(DecisionPrimitive.BOOLEAN_PROBABILITY, "Keep item"),
        },
        eligible_option_ids=("local", "free"), required_evidence_ids=("ev-1",),
        data_classification="PUBLIC", risk_class="R0", source_sha="a" * 40,
        context_fingerprint="b" * 64,
    )
    value.update(overrides)
    return DecisionRequest(**value)


class FakeTransport:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or {
            "model": "jev-1.13.0",
            "answers": {
                "route": {"choice": "local", "probabilities": {"local": 0.8, "free": 0.2}, "confidence": 0.7},
                "fit": {"score": 1.8, "confidence": 0.75},
                "keep": {"yes_probability": 0.9},
            },
        }

    def evaluate(self, payload, *, deadline_ms):
        self.calls.append((payload, deadline_ms))
        return self.response


def test_typed_choice_score_boolean_advice_is_advisory_and_bounded():
    transport = FakeTransport()
    result = JevDecisionAdvisor(
        _policy(), transport, credential_available=lambda: True,
    ).advise(_request())
    assert result.status == DecisionStatus.ADVISED
    assert result.recommended_option_ids == ("local",)
    assert result.advisory_only is True
    assert result.cost.observed_cost_usd == 0
    assert transport.calls[0][0]["questions"]["keep"]["type"] == "boolean"
    durable = json.dumps(result.to_dict())
    assert "PRIVATE_STATE_SHOULD_NOT_PERSIST" not in durable
    assert "Choose" not in durable


@pytest.mark.parametrize("policy,credential,authority", [
    (_policy(enabled=False), True, "TECHNICAL_ADVISORY"),
    (_policy(), False, "TECHNICAL_ADVISORY"),
    (_policy(routes=[{"route_id": "direct", "cost_class": "METERED_PAID"}]), True, "TECHNICAL_ADVISORY"),
    (DecisionPolicy(_policy().document, now=dt.datetime(2026, 9, 26, tzinfo=dt.timezone.utc)), True, "TECHNICAL_ADVISORY"),
    (_policy(), True, "PRODUCTION"),
    (_policy(), True, "PAYMENT"),
    (_policy(), True, "PROJECT_COMPLETION"),
    (_policy(), True, "PROTECTED_LINEAGE"),
])
def test_disabled_cost_expiry_credential_and_authority_blocks_never_call(policy, credential, authority):
    transport = FakeTransport()
    result = JevDecisionAdvisor(
        policy, transport, credential_available=lambda: credential,
    ).advise(_request(authority_class=authority))
    assert result.status == DecisionStatus.UNAVAILABLE
    assert transport.calls == []


def test_null_advisor_has_no_scheduler_effect():
    result = NullDecisionAdvisor().advise(_request())
    assert result.status == DecisionStatus.UNAVAILABLE
    assert result.recommended_option_ids == ()


@pytest.mark.parametrize("response", [
    {"model": "jev-latest", "answers": {}},
    {"model": "jev-1.13.0", "answers": {}},
    {"model": "jev-1.13.0", "answers": {
        "route": {"choice": "paid", "probabilities": {"local": 0.5, "free": 0.5}, "confidence": 0.9},
        "fit": {"score": 1, "confidence": 0.9}, "keep": {"yes_probability": 0.5},
    }},
    {"model": "jev-1.13.0", "answers": {
        "route": {"choice": "local", "probabilities": {"local": 0.8, "free": 0.8}, "confidence": 0.9},
        "fit": {"score": 1, "confidence": 0.9}, "keep": {"yes_probability": 0.5},
    }},
])
def test_model_drift_missing_answers_out_of_set_and_bad_probabilities_fail_closed(response):
    transport = FakeTransport(response)
    result = JevDecisionAdvisor(_policy(), transport, credential_available=lambda: True).advise(_request())
    assert result.status == DecisionStatus.UNAVAILABLE
    assert result.recommended_option_ids == ()


def test_low_confidence_abstains_and_does_not_recommend():
    response = FakeTransport().response
    response["answers"]["route"]["confidence"] = 0.59
    result = JevDecisionAdvisor(_policy(), FakeTransport(response), credential_available=lambda: True).advise(_request())
    assert result.status == DecisionStatus.ABSTAINED
    assert result.recommended_option_ids == ()


def test_limits_reject_before_transport():
    transport = FakeTransport()
    advisor = JevDecisionAdvisor(_policy(), transport, credential_available=lambda: True)
    assert advisor.advise(_request(max_context_tokens=32_001)).status == DecisionStatus.UNAVAILABLE
    too_many = tuple(f"o{i}" for i in range(256))
    questions = {"route": DecisionQuestion(DecisionPrimitive.CHOICE, "Choose", too_many)}
    assert advisor.advise(_request(questions=questions, eligible_option_ids=too_many)).status == DecisionStatus.UNAVAILABLE
    assert transport.calls == []


def test_ledger_records_only_sanitized_decision_metadata(tmp_path):
    ledger = ResourceLedger(tmp_path / "ledger.jsonl")
    advisor = JevDecisionAdvisor(
        _policy(), FakeTransport(), credential_available=lambda: True,
        ledger=ledger, monotonic=iter((1.0, 1.01)).__next__,
    )
    result = advisor.advise(_request())
    assert result.status == DecisionStatus.ADVISED
    assert [event.event_type for event in ledger.events()] == ["DECISION_REQUESTED", "DECISION_RESULT"]
    serialized = json.dumps([event.to_dict() for event in ledger.events()])
    assert "PRIVATE_STATE_SHOULD_NOT_PERSIST" not in serialized
    assert "Choose" not in serialized
    assert "jev-1.13.0" in serialized
