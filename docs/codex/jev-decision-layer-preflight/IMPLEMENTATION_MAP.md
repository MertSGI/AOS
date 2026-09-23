# Jev implementation map

## Dependency position

Implement only in Resource OS Phase 10, after R1/R1B/R1C, `QuotaGovernor` (R2), and `ResourceLedger` (R3). Do not cherry-pick this audit as implementation. The current accepted source has a planner-provider registry and execution-backend router but no decision-resource abstraction.

Jev must not be registered in `src/aos/autonomous_host.py::_PROVIDER_FACTORIES` and must not implement `extensions/autonomy-fabric/execution_backend.py::ExecutionBackend`. Those surfaces are for planner inference and executable workers respectively.

## New source surfaces for a later authorized implementation

| File | Responsibility |
|---|---|
| `src/aos/decision_advisor.py` | Provider-neutral enums, typed contracts, `DecisionAdvisor` protocol, validators, and `NullDecisionAdvisor`. |
| `src/aos/jev_decision_advisor.py` | Jev primitive mapping, response validation, version check, conservative context budget, sanitized failures, and `JevDecisionAdvisor`. No scheduling authority. |
| `src/aos/decision_transport.py` | Small transport protocol plus TypeSafe-direct, Vercel, OpenRouter, and optional Cloudflare route adapters. A route is selected only after cost policy. |
| `src/aos/decision_policy.py` | Feature enablement, allowed task/risk classes, per-use-case deadlines, versioned confidence thresholds, route order, and fail-closed cost rules. |
| `schemas/v0.1/decision_advisor_policy.schema.json` | Closed schema for the policy; default disabled, paid budget zero, production forbidden. |
| `descriptors/jev.decision-policy.json` | Initial disabled descriptor with Vercel promotional route expiry and no metered fallback. |

Keep vendor wire objects inside `jev_decision_advisor.py`/transports. Core scheduling consumes only `DecisionRequest`, `DecisionResult`, `DecisionConfidence`, `DecisionCostObservation`, and `DecisionResourceAvailability`.

## Existing surfaces to integrate

| Existing/planned file | Bounded change |
|---|---|
| `src/aos/provider_observation.py` (R1 planned) | Reuse sanitized failure/rate observations; do not persist raw Jev payloads. Add a decision-resource subtype only if the R1 contract cannot represent it cleanly. |
| `src/aos/quota_governor.py` (R2 planned) | Track route/model/task-class quota separately from health and cost. Honor Retry-After. |
| `src/aos/resource_ledger.py` (R3 planned) | Append idempotent request/result availability, latency, token and cost observations; never store state/questions/raw answers containing project data. |
| `src/aos/provider_registry.py` | Do not overload `ProviderEntry`. Add/consume a separate Resource OS decision-resource registry once Phase 11 defines it. |
| `src/aos/planning_kernel.py` | Optional hooks for task classification, task-class recommendation, optional-context scoring, and post-result scoring. Native planning and completion logic remain authoritative. |
| `src/aos/runtime_worker.py` | May request retry/wait/reroute advice after deterministic observations exist. Existing waiting/recovery/churn bounds own the outcome. |
| `src/aos/runtime_server.py` | Surface sanitized decision-resource availability; Jev recovery must not wake unrelated task classes. |
| `extensions/autonomy-fabric/execution_router.py` | Phase 11 may use advice only as a tie-breaker among already-eligible backends. Capability, trust, health, quota, cost, and authority filtering runs first and again after advice. |
| `extensions/autonomy-fabric/authority_router.py` and `src/aos/human_gate_policy.py` | Remain final authority gates. Add explicit tests that Jev cannot change their category or resolution. |
| `src/aos/verification.py` and canonical validators | Jev scores are supplementary evidence only; deterministic verification and completion rules are unchanged. |

## Initial policy shape

The descriptor should express:

```json
{
  "enabled": false,
  "advisory_only": true,
  "model": "jev-1.13.0",
  "portable_context_limit_tokens": 32000,
  "paid_budget_usd": 0.0,
  "allow_paid_fallback": false,
  "allowed_risk_classes": ["R0", "R1"],
  "forbidden_authorities": [
    "PRODUCTION", "DESTRUCTIVE", "PAYMENT", "SECURITY_OVERRIDE",
    "PROJECT_COMPLETION", "PROTECTED_LINEAGE"
  ],
  "routes": [
    {
      "route_id": "vercel_jev_promotional",
      "cost_class": "PROMOTIONAL_FREE",
      "not_after": "2026-09-25T23:59:59Z"
    }
  ]
}
```

The timestamp is a hard maximum, not proof that the offer remains active until that instant. A fresh route observation can disable it earlier. After expiry, the descriptor yields `COST_BLOCKED` and the null advisor path.

## Implementation sequence

1. Land the provider-neutral contracts, policy schema, null advisor, and unit tests with Jev disabled.
2. Add cost/availability checking and fake transports; prove unknown/metered/expired routes never reach transport.
3. Add the Jev adapter and fixture-based Choice/Score/Noul plus Vercel Boolean translation tests.
4. Connect sanitized observations to `QuotaGovernor` and `ResourceLedger` without making the ledger canonical project truth.
5. Add shadow-only call sites for one use case at a time. Begin with task classification; do not enable all eight uses together.
6. Run E0/E1. Run E2/E3 only under then-current zero-cost and credential conditions.
7. Permit an advisory canary only through a separate reviewed policy change. Keep an instant disable switch and `PRODUCTION=NO_GO`.

## Test map

Add `tests/test_decision_advisor.py`, `tests/test_jev_decision_advisor.py`, `tests/test_decision_policy.py`, and `tests/test_jev_authority_boundaries.py`. Extend `tests/test_planning_kernel.py`, `tests/test_runtime_recovery.py`, `tests/test_quota_governor.py`, `tests/test_resource_ledger.py`, and `extensions/autonomy-fabric/tests/test_execution_router.py` only where the corresponding integration hook is added.

Acceptance test IDs should cover:

- `JEV-CONTRACT-01..08`: typed primitives, probabilities, aliases, limits, malformed data;
- `JEV-COST-01..07`: promotion, expiry, unknown cost, zero budget, paid denial, no auto top-up, ledger cost evidence;
- `JEV-AUTH-01..08`: every forbidden authority and out-of-set recommendation;
- `JEV-DEGRADE-01..10`: missing key, disabled, 401/422/429/529, timeout, overload, quota, low confidence, restart;
- `JEV-DATA-01..05`: redaction and durable-evidence exclusions;
- `JEV-USE-01..08`: one gate for each requested AOS use case.

The full canonical suite and `git diff --check` are required after implementation. No test should depend on a live paid endpoint.

## Architecture evidence read

- Accepted FreeLLMAPI base: `origin/feature/aos-freellmapi-local-meta-provider-20260922-01` at `ccdebfd7b5bdc32f7e95634ef2c464293356c872`.
- Resource OS master: `origin/feature/aos-resource-os-master-20260923-01` at `c80df4d691c5b08d3b04c8e2190c92828818a580`.
- Resource OS runtime preflight: `origin/audit/aos-resource-os-runtime-preflight-20260923-01` at `d0ea8064f2d349e0b63c1fe5009a4901981464a8`.
- Codex CLI backend preflight: `origin/audit/aos-codex-cli-backend-preflight-20260923-01` at `66d38917f60d17eedc1fa370649db1ddf8045681`.
