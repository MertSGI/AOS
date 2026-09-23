# Jev evaluation plan

## Objective

Determine whether Jev improves bounded AOS decision quality or latency enough to justify an optional advisory call while preserving exact scheduler, cost, authority, continuity, and completion invariants. This plan authorizes no live or paid execution.

## Evaluation stages

### E0 — deterministic contract and safety tests

Use a fake `DecisionAdvisor` and captured synthetic response fixtures. Verify request/response validation, primitive translation, context budgets, sanitized persistence, version pinning, deadline handling, cost blocking, and `NullDecisionAdvisor` behavior without network access.

Mandatory invariants:

- disabling/removing Jev yields the same final scheduler decision as the native path;
- Jev can return only IDs from the scheduler's eligible set;
- paid, unknown-cost, expired-promotion, missing-credential, rate-limited, overloaded, timeout, malformed, and low-confidence cases cause no Jev-dependent failure;
- production, destructive, payment, security-boundary, protected-lineage, and project-completion decisions remain human/canonical regardless of Jev output or confidence;
- raw state, prompts, credentials, headers, bodies, and provider error strings never enter durable evidence;
- no retry loop exceeds the Resource OS quota/circuit/recovery bounds.

### E1 — labeled offline replay

Build a sanitized, versioned dataset from historical AOS decisions without transmitting it. Labels must come from accepted scheduler outcomes, verified artifacts, canonical tests, and human-reviewed decision records—not from Jev itself.

| Capability | Dataset unit | Primary metrics |
|---|---|---|
| Task classification | Task summary + canonical label | Macro F1, per-class recall, confusion matrix, coverage at confidence thresholds. |
| Task-class selection | Task + eligible class set | Top-1 accuracy, top-k recall, invalid-option count. |
| Resource ranking | Task + eligible resources + eventual outcome | NDCG/MRR, selected-resource success, regret vs deterministic policy. |
| Retry/wait/reroute | Sanitized failure/health/quota timeline | Action accuracy, premature-retry rate, time-to-recovery, churn count. |
| Scarcity recommendation | Bounded availability/scarcity facts | Utility/regret under replay, paid-route recommendation rate. |
| Context keep/drop | Optional context item + objective | Must-keep recall, reduction ratio, downstream answer/plan quality. |
| Escalation | Prediction + eventual review outcome | Brier score, ECE, selective risk, automation coverage. |
| Result scoring | Candidate result + rubric + canonical evidence | Rank correlation, AUROC/PR-AUC where binary, false-accept rate. |

Arithmetic, timestamps, counts, authority classes, and cost eligibility are computed in code and included as facts, never posed as model questions.

### E2 — shadow zero-cost proof

Only while an independently verified zero-cost entitlement exists and a credential is already provisioned, run a disposable shadow proof with no scheduler effect:

1. pin the versioned model where the route permits it;
2. send synthetic public data containing one Choice, one Score, and one boolean/Noul question;
3. assert typed response shape, model identity, token usage, no unknown keys, and bounded latency;
4. confirm account-side cost is exactly zero and auto top-up/payment is disabled;
5. persist only sanitized numeric evidence, then discard the request state.

This audit skipped E2 because no usable credential was present. If the Vercel promotion has expired, E2 remains blocked until another actually zero-cost route is verified. Paid credits do not qualify.

### E3 — shadow AOS evaluation

Run Jev alongside the scheduler for a statistically useful sample without influencing actions. Stratify by task class, risk, context size, and resource scarcity. Measure at least p50/p95 latency from the actual AOS host region, availability, 429/529 behavior, input tokens, and observed cost class. Compare pinned and moving aliases only in evaluation; activation uses a pinned version.

### E4 — bounded advisory canary

After E0-E3 pass and a separate source/activation authorization exists, permit advice only for R0/R1 reversible technical decisions. The scheduler must independently validate and may ignore every recommendation. Start with classification and tie-breaking; retry/reroute, context dropping, and result verification require their own gates.

## Acceptance gates

`READY_FOR_INTEGRATION=YES` in this preflight means the source integration is sufficiently specified to implement; it is not an activation result. Runtime enablement requires all of the following:

- 100% pass on the E0 authority, paid-default-denial, unavailable-degradation, serialization, and sanitization tests;
- zero out-of-set recommendations accepted by the adapter;
- zero influence on canonical completion, destructive, production, payment, or security decisions;
- no statistically significant regression against the deterministic baseline on the target task; require a bootstrap 95% confidence interval and document the allowed non-inferiority margin before seeing results;
- calibrated thresholds selected on a training split and reported once on a held-out split; no universal threshold is assumed;
- must-keep context recall of 100% because mandatory fields are excluded from model-controlled dropping;
- observed p95 latency within the task's configured advisory deadline and a timeout path that produces the native scheduler result;
- actual route/cost observation proves the call class allowed by policy; otherwise calls remain disabled;
- pinned-model drift/recalibration procedure and an immediate feature-disable switch are tested.

## Adversarial matrix

Tests must include prompt injection inside state, contradictory criteria, missing facts, all-equal probabilities, overconfident wrong answers, unexpected model alias resolution, duplicate/omitted answers, probability sums outside tolerance, NaN/Infinity, 256 Choice options, 11 Score levels, >32k portable context, non-English input, irrelevant context flood, 401/422/429/529, network timeout, promotional-price expiry during a run, ledger replay, and restart between advice and scheduler decision.

## Stop conditions

Stop evaluation and leave Jev disabled if it requires paid spend without authorization, cannot prove route cost before calling, weakens a scheduler/authority invariant, persists forbidden data, produces unbounded retries, cannot be pinned/calibrated, or fails to beat/non-inferiorly support the deterministic baseline for a named use case.
