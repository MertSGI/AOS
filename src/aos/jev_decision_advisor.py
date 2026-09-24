"""Advisory-only Jev adapter with closed validation and no authority."""
from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any, Callable, Dict

from aos.decision_advisor import (
    DecisionConfidence, DecisionCostObservation, DecisionPrimitive, DecisionRequest,
    DecisionResourceAvailability, DecisionResult, DecisionStatus, unavailable_result,
)
from aos.decision_policy import DecisionPolicy
from aos.decision_transport import DecisionTransport
from aos.resource_ledger import ResourceEventType, ResourceLedger


class JevDecisionAdvisor:
    def __init__(
        self,
        policy: DecisionPolicy,
        transport: DecisionTransport,
        *,
        credential_available: Callable[[], bool] = lambda: False,
        ledger: ResourceLedger | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy
        self.transport = transport
        self.credential_available = credential_available
        self.ledger = ledger
        self.monotonic = monotonic

    def _record(self, event_type: ResourceEventType, request: DecisionRequest, suffix: str, payload: Dict[str, Any]) -> None:
        if self.ledger is None:
            return
        self.ledger.append(
            event_type,
            idempotency_key=f"decision:{request.request_id}:{suffix}",
            payload={
                "attempt_id": request.request_id,
                "objective_id": request.objective_id,
                "task_class": request.task_class,
                "decision_kind": request.decision_kind,
                "model_requested": self.policy.document.get("model", "jev-1.13.0"),
                "advisory_only": True,
                **payload,
            },
        )

    @staticmethod
    def _valid_probability(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1

    def advise(self, request: DecisionRequest) -> DecisionResult:
        route_decision = self.policy.select_route(
            risk_class=request.risk_class,
            authority_class=request.authority_class,
            credential_available=self.credential_available(),
        )
        if not route_decision.eligible:
            return unavailable_result(request.request_id, f"JEV_{route_decision.reason}")
        if request.max_context_tokens <= 0 or request.max_context_tokens > 32_000:
            return unavailable_result(request.request_id, "JEV_CONTEXT_LIMIT")
        serialized = json.dumps({
            "state": request.state,
            "questions": {
                name: {
                    "primitive": question.primitive.value,
                    "prompt": question.prompt,
                    "options": question.options,
                    "levels": question.levels,
                }
                for name, question in request.questions.items()
            },
        }, ensure_ascii=False)
        if len(serialized) // 4 > request.max_context_tokens:
            return unavailable_result(request.request_id, "JEV_CONTEXT_BUDGET")
        if len(request.source_sha) != 40 or len(request.context_fingerprint) != 64:
            return unavailable_result(request.request_id, "JEV_REQUEST_BINDING_INVALID")
        eligible = set(request.eligible_option_ids)
        wire_questions: Dict[str, Any] = {}
        for name, question in request.questions.items():
            if not name or len(question.prompt) > 2000:
                return unavailable_result(request.request_id, "JEV_QUESTION_INVALID")
            if question.primitive == DecisionPrimitive.CHOICE:
                if not 2 <= len(question.options) <= 255 or not set(question.options).issubset(eligible):
                    return unavailable_result(request.request_id, "JEV_OPTIONS_INVALID")
                wire_questions[name] = {"type": "choice", "question": question.prompt, "options": list(question.options)}
            elif question.primitive == DecisionPrimitive.SCORE:
                if not 2 <= len(question.levels) <= 10:
                    return unavailable_result(request.request_id, "JEV_LEVELS_INVALID")
                wire_questions[name] = {"type": "score", "question": question.prompt, "levels": list(question.levels)}
            else:
                wire_questions[name] = {"type": "boolean", "question": question.prompt}
        route = route_decision.route or {}
        started = self.monotonic()
        self._record(ResourceEventType.DECISION_REQUESTED, request, "requested", {
            "route_id": str(route["route_id"]), "status": "STARTED",
            "zero_cost_eligible": True,
        })
        try:
            response = self.transport.evaluate({
                "model": self.policy.document["model"], "state": request.state,
                "questions": wire_questions,
            }, deadline_ms=min(max(request.deadline_ms, 1), 5000))
            if response.get("model") != self.policy.document["model"]:
                raise ValueError("model mismatch")
            raw_answers = response.get("answers")
            if not isinstance(raw_answers, dict) or set(raw_answers) != set(request.questions):
                raise ValueError("answer set mismatch")
            answers: Dict[str, Any] = {}
            confidences: Dict[str, DecisionConfidence] = {}
            recommended = []
            threshold = float(self.policy.document.get("confidence_threshold", 0.6))
            low = False
            for name, question in request.questions.items():
                answer = raw_answers[name]
                if not isinstance(answer, dict):
                    raise ValueError("answer object required")
                if question.primitive == DecisionPrimitive.CHOICE:
                    choice = answer.get("choice")
                    probabilities = answer.get("probabilities")
                    confidence = answer.get("confidence")
                    if choice not in question.options or choice not in eligible or not isinstance(probabilities, dict):
                        raise ValueError("invalid choice")
                    if set(probabilities) != set(question.options) or not all(self._valid_probability(v) for v in probabilities.values()) or abs(sum(probabilities.values()) - 1) > 0.01:
                        raise ValueError("invalid probability distribution")
                    if not self._valid_probability(confidence):
                        raise ValueError("invalid confidence")
                    answers[name] = {"choice": choice}
                    recommended.append(choice)
                    selected = float(probabilities[choice])
                    low = low or float(confidence) < threshold
                    confidences[name] = DecisionConfidence("DISTRIBUTION_DERIVED", selected, float(confidence), {k: float(v) for k, v in probabilities.items()}, threshold, "LOW" if confidence < threshold else "HIGH")
                elif question.primitive == DecisionPrimitive.SCORE:
                    score, confidence = answer.get("score"), answer.get("confidence")
                    if not isinstance(score, (int, float)) or not 0 <= score <= len(question.levels) - 1 or not self._valid_probability(confidence):
                        raise ValueError("invalid score")
                    answers[name] = {"score": float(score)}
                    low = low or float(confidence) < threshold
                    confidences[name] = DecisionConfidence("DISTRIBUTION_DERIVED", None, float(confidence), {}, threshold, "LOW" if confidence < threshold else "HIGH")
                else:
                    probability = answer.get("yes_probability")
                    if not self._valid_probability(probability):
                        raise ValueError("invalid boolean probability")
                    answers[name] = {"yes_probability": float(probability)}
                    confidences[name] = DecisionConfidence("BOOLEAN_PROBABILITY_ONLY", float(probability), None, {"yes": float(probability), "no": 1-float(probability)}, None, "UNAVAILABLE")
        except Exception:
            self._record(ResourceEventType.DECISION_RESULT, request, "result", {
                "route_id": str(route["route_id"]), "status": "INVALID",
                "latency_ms": max(0.0, (self.monotonic() - started) * 1000),
                "cost_actual_usd": 0.0,
            })
            return unavailable_result(request.request_id, "JEV_SANITIZED_TRANSPORT_OR_CONTRACT_FAILURE")
        cost = DecisionCostObservation(
            route_id=str(route["route_id"]), cost_class="PROMOTIONAL_FREE",
            input_usd_per_million=0.0, output_usd_per_million=0.0,
            observed_cost_usd=0.0, price_observed_at=str(route["price_observed_at"]),
            price_source="VERIFIED_POLICY_OBSERVATION", paid_authorized=False,
        )
        availability = DecisionResourceAvailability(
            route_id=str(route["route_id"]), model_requested=self.policy.document["model"],
            model_resolved=response["model"], status="AVAILABLE",
            credential_available=True, zero_cost_eligible=True,
            observation_id=hashlib.sha256(f"{request.request_id}|{route['route_id']}".encode()).hexdigest(),
        )
        final_status = "ABSTAINED" if low else "ADVISED"
        self._record(ResourceEventType.DECISION_RESULT, request, "result", {
            "route_id": str(route["route_id"]), "model_resolved": response["model"],
            "status": final_status,
            "latency_ms": max(0.0, (self.monotonic() - started) * 1000),
            "cost_actual_usd": 0.0, "zero_cost_eligible": True,
        })
        return DecisionResult(
            request_id=request.request_id,
            status=DecisionStatus(final_status),
            answers=answers, confidence=confidences,
            recommended_option_ids=tuple(dict.fromkeys(recommended)) if not low else (),
            model_resolved=response["model"], availability=availability, cost=cost,
            sanitized_error_class="LOW_CONFIDENCE" if low else None,
        )
