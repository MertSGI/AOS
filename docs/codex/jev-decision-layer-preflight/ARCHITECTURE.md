# Jev decision-layer architecture preflight

Track: `AOS-JEV-DECISION-LAYER-PREFLIGHT-20260923-01`
Audit date: `2026-09-23`
Accepted base: `ccdebfd7b5bdc32f7e95634ef2c464293356c872`
Production: `NO_GO`

## Decision

Jev is a viable optional `DECISION_MODEL` for AOS, subject to a later source implementation and evaluation. It must be implemented as `JevDecisionAdvisor`, behind a provider-neutral advisory contract. It is not a `PlannerProvider`, an `ExecutionBackend`, an agent, or a source of authority.

The deterministic scheduler constructs the eligible action/resource set, applies authority and cost gates, and owns the final choice. Jev may rank or recommend only members of that already-eligible set. An absent, unavailable, over-budget, malformed, low-confidence, or expired-free-route Jev result is equivalent to no advice: AOS continues normally.

This fits the accepted Resource OS Phase 10 contract at remote head `c80df4d691c5b08d3b04c8e2190c92828818a580`: Jev follows R3/`ResourceLedger`, remains advisory, has dynamic cost state, and degrades out before the Phase 11 Resource Orchestrator.

## Verified Jev model contract

As of the audit date, TypeSafe's stable model is `jev-1.13.0`; `jev-latest` points to it but may move. Jev accepts text-bearing strings, JSON objects, or arrays and produces typed decisions rather than generated prose or code.

One request contains a shared `state` and a named map of independent questions. Questions are evaluated in parallel against that state:

| Native primitive | Contract | AOS use |
|---|---|---|
| `choice` | Selects one of up to 255 caller-defined keys; returns `choice`, a probability for every key, and `confidence`. | Classification, task class, bounded action/resource ranking. |
| `score` | Rates against 2-10 ordered descriptive levels; returns a probability-weighted `score`, legend, probabilities, and `confidence`. | Relevance, suitability, scarcity pressure, result-quality dimensions. |
| `noul` | Returns the probability of yes from 0 to 1. | Boolean-style keep/drop, retry suitability, escalation recommendation. |

Vercel's evaluation API names the third primitive `boolean`; its TypeSafe-compatible API preserves `noul`. The AOS contract uses the provider-neutral name `BOOLEAN_PROBABILITY` and adapters translate wire names.

Only Choice and Score return TypeSafe's separate distribution-derived `confidence`. Noul returns a yes probability but no separate confidence. AOS must not synthesize the missing value as if TypeSafe supplied it; it records the probability and marks confidence kind `BOOLEAN_PROBABILITY_ONLY`.

The direct TypeSafe limit is 64k tokens across state and all questions combined, with an additional 32k-token limit on state plus the longest single question. Gateway catalogs currently expose 32k. AOS therefore uses the conservative portable ceiling of 32k and a much smaller configured decision-context budget. Long project history is never forwarded wholesale.

TypeSafe reports 70-500 ms end-to-end latency for its service, with published measurements made from the US West Coast. That is vendor-reported, not an AOS/Istanbul measurement. The evaluation plan requires local p50/p95 evidence before activation.

## Provider-neutral contracts

The following are logical contracts, not source added by this audit.

```python
class DecisionRequest:
    request_id: str
    objective_id: str
    task_class: str
    decision_kind: str
    state: JSONValue
    questions: dict[str, DecisionQuestion]
    eligible_option_ids: tuple[str, ...]
    required_evidence_ids: tuple[str, ...]
    data_classification: str
    risk_class: str
    source_sha: str
    context_fingerprint: str
    max_context_tokens: int
    deadline_ms: int

class DecisionConfidence:
    kind: str  # DISTRIBUTION_DERIVED | BOOLEAN_PROBABILITY_ONLY | UNAVAILABLE
    selected_probability: float | None
    distribution_confidence: float | None
    probabilities: dict[str, float]
    policy_threshold: float | None
    band: str  # HIGH | MEDIUM | LOW | UNAVAILABLE
    threshold_policy_id: str
    calibrated_on_aos_dataset: bool

class DecisionCostObservation:
    route_id: str
    cost_class: str  # PROMOTIONAL_FREE | FREE_CREDIT | METERED_PAID | UNKNOWN
    input_usd_per_million: float | None
    output_usd_per_million: float | None
    input_tokens: int | None
    output_tokens: int | None
    observed_cost_usd: float | None
    price_observed_at: str
    price_source: str
    paid_authorized: bool

class DecisionResourceAvailability:
    route_id: str
    model_requested: str
    model_resolved: str | None
    status: str  # AVAILABLE | UNAVAILABLE | RATE_LIMITED | OVERLOADED | COST_BLOCKED | UNKNOWN
    credential_available: bool
    zero_cost_eligible: bool
    next_retry_at: str | None
    observation_id: str | None

class DecisionResult:
    request_id: str
    status: str  # ADVISED | ABSTAINED | UNAVAILABLE | REJECTED | INVALID
    answers: dict[str, TypedDecisionAnswer]
    confidence: dict[str, DecisionConfidence]
    recommended_option_ids: tuple[str, ...]
    model_resolved: str | None
    availability: DecisionResourceAvailability
    cost: DecisionCostObservation
    advisory_only: bool
    sanitized_error_class: str | None
```

`JevDecisionAdvisor.advise(request) -> DecisionResult` validates the request, verifies that every option is already scheduler-eligible, checks cost authorization before transport, calls one selected transport, validates the typed response, and returns advice. `NullDecisionAdvisor` always returns `UNAVAILABLE` and is the default when Jev is disabled or absent.

## Control flow and authority

1. AOS code derives facts, arithmetic, timestamps, hard constraints, permissions, quotas, health, and policy eligibility deterministically.
2. The scheduler creates a bounded candidate set and a sanitized `DecisionRequest`.
3. Cost policy must establish a current zero-cost entitlement or explicit paid authorization before any call. Unknown cost is blocked.
4. Jev returns advice only. The adapter rejects unknown option IDs, missing answers, invalid distributions, context overflow, or an unexpected model.
5. Confidence policy may accept the advice as a tie-breaker, request more evidence, or ignore it. Thresholds are versioned and learned from AOS-labeled evaluation data.
6. The scheduler independently re-applies eligibility, authority, and cost gates and records its own final decision. Jev never changes those gates.
7. On any Jev failure, timeout, low confidence, or cost block, the same scheduler path runs without advice.

The result and its bounded metadata may be appended to `ResourceLedger` as operational evidence. Raw prompts/state, response bodies, credentials, headers, and unrestricted provider error text are not durable evidence.

## AOS use-case mapping

| Use case | Jev question shape | Hard AOS boundary |
|---|---|---|
| Task classification | Choice over canonical task classes. | Unknown/low-confidence class falls back to deterministic classification or escalation. |
| Task-class selection | Choice over classes compatible with declared capabilities. | Cannot weaken risk, data, context, or capability requirements. |
| Resource ranking | Parallel Score or Choice over scheduler-eligible resource IDs. | Cannot introduce an ineligible resource or call an execution backend itself. |
| Retry vs wait vs reroute | Choice over `RETRY`, `WAIT`, `REROUTE`, `NO_ADVICE` using sanitized observations. | Circuit, `QuotaGovernor`, Retry-After, churn bounds, and scheduler policy remain authoritative. |
| Scarcity-aware recommendations | Score each eligible route from bounded scarcity/quality facts. | Arithmetic and budget calculations stay in code; no paid-route enablement. |
| Context relevance / keep-drop | Noul per optional context item, batched. | Objective, authority, source SHA, checkpoint, completed work, artifact hashes, safety constraints, and required evidence are always retained. |
| Confidence-based escalation | Choice/Score confidence or Noul probability feeds a versioned threshold policy. | Low confidence can only reduce automation; it cannot bypass a human gate. |
| Result scoring/verification | Independent Scores/Nouls against explicit rubrics and evidence. | Canonical validators, tests, artifact hashes, and project-completion rules decide acceptance. |

## Explicit prohibitions

Jev must never become a code-generation backend, agentic backend, production authority, destructive-action authority, payment authority, security-boundary override, or canonical project-completion authority. It cannot write files, execute commands, call tools, create/close project work, authorize deployment, alter credentials, expand scopes, or convert advisory confidence into standing authority.

Jev is also unsuitable for arithmetic, counting, date comparison, multi-hop reasoning, or large irrelevant state. TypeSafe documents these as current `jev-1.13` jagged edges; AOS must compute such facts in code and send only relevant, named fields.

## Sources

- TypeSafe: [System One](https://docs.typesafe.ai/concepts/system-one), [models and limits](https://docs.typesafe.ai/models), [API reference](https://docs.typesafe.ai/api), [confidence](https://docs.typesafe.ai/confidence), and [Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13).
- TypeSafe: [launch and latency/cost disclosure](https://typesafe.ai/blog/introducing-system-one-models-and-jev).
- Vercel: [Jev model page](https://vercel.com/ai-gateway/models/jev) and [TypeSafe-compatible/HTTP access](https://vercel.com/changelog/ai-gateway-now-supports-typesafe-clients-and-http-api-for-jev).
