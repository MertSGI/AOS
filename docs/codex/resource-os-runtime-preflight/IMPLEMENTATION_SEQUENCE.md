# Resource OS Runtime Minimal Safe Implementation Sequence

Track: `AOS-RESOURCE-OS-RUNTIME-P0-PREFLIGHT-20260923-01`
Base: `a0cfc2e329b36dca1964ed427ab17d8c82cbacb6`
Production remains `NO_GO` throughout.

## Ordering rule

Implement and merge in the exact order below. Each stage must pass its focused tests plus the existing provider, planning-kernel, and runtime-recovery suites before the next stage starts. Do not combine these stages with AG, Codex, Qwen, production enablement, or paid-fallback work.

## R1 — preserve provider observations and correct health semantics

R1 contains three inseparable internal steps. They should be reviewable commits in this order but land before R1B.

### R1.1 — typed sanitized observation/error contract

1. Add `src/aos/provider_observation.py` with:
   - `TaskClass`, `ObservationSource`, `FailureFamily`, and `ContractFailureSubtype` closed enums;
   - immutable `RateLimitObservation` with strict validation and `to_dict`/`from_dict`;
   - case-insensitive allowlisted header/SDK-field extraction;
   - `Retry-After` delta/date parsing using an injected clock;
   - per-field truth-rank merge; and
   - safe contract-detail builders that never accept raw response content.
2. Extend `PlannerTransientError` with optional canonical failure family and `RateLimitObservation` and extend `PlannerContractError` with required subtype and optional sanitized detail. Preserve ordinary `str(exc)` compatibility, but stop interpolating raw provider exception text in newly persisted/propagated known failures.
3. Update every provider adapter to attach observations before re-raising. Use raw-response SDK access only where supported and tested; parse successful response rate headers as observations without persisting raw headers. Continue fail-closed behavior where a provider SDK exposes no safe metadata.

### R1.2 — propagate observation and contract subtype

1. Add `task_class`, `contract_subtype`, `safe_detail`, and `rate_limit_observation` to `ProviderAttempt`.
2. Pass the typed data from the caught planner exception into the attempt journal and circuit/quota-facing result. Known failures keep `message=None`.
3. Add `task_class` to planning-kernel requests and to synthetic probes. The synthetic activation probe is always `small_reasoning`; planning DAG compilation is at least `structured_planning`, with repo/UI plans explicitly `repo_ui_planning`. Persist the class required by a waiting command.
4. Preserve contract subtype in probe telemetry. Never infer subtype by reparsing persisted message text.

### R1.3 — failure-family isolation and task-class health

1. Version `provider-circuits.json` and replace the scheduling authority of provider-wide `consecutive_failure_count` with records keyed by `(provider_id, model_id, task_class)` containing per-family streaks.
2. Rate/quota/credit observations do not increment health streaks. Until R2, exact rate deadlines may be stored as a separate compatibility gate beside the health record; they must not close/open health.
3. Calculate adaptive fallback from only the current family streak. Exact `Retry-After`/reset evidence wins over that estimate.
4. Make successes update only the observed task class. A small probe cannot close or wake structured, repo/UI, large-context, or agentic work.
5. Update cross-command aggregation and wake logic to preserve newest evidence per provider/model/task class and to require compatibility with the waiting command's class.
6. Migrate legacy provider-wide records to `UNKNOWN`/`LEGACY_UNKNOWN` without seeding a specific family streak. Keep read compatibility; write only the new schema.

Contract subtype telemetry and failure-family-aware backoff therefore land inside R1, after typed observation capture and before content-deduplication work. This prevents R2 from consuming contaminated counters or string-parsed errors.

### R1 files

Production files:

- `src/aos/provider_observation.py` (new)
- `src/aos/planner.py`
- `src/aos/providers/openai_compatible.py`
- `src/aos/providers/groq.py`
- `src/aos/providers/gemini.py`
- `src/aos/providers/nemotron.py`
- `src/aos/providers/ollama.py`
- `src/aos/autonomous_host.py`
- `src/aos/provider_circuit.py`
- `src/aos/provider_probe.py`
- `src/aos/planning_kernel.py`
- `src/aos/runtime_worker.py`
- `src/aos/runtime_server.py`

Focused tests:

- `tests/test_provider_observation.py` (new)
- `tests/test_providers.py`
- `tests/test_nemotron_fabric.py`
- `tests/test_autonomous_host.py`
- `tests/test_provider_circuit.py`
- `tests/test_provider_reliability_audit.py`
- `tests/test_runtime_provider_source_resilience_20260922.py`
- `tests/test_runtime_recovery.py`

## R1B — content-aware read deduplication

1. Extend `CoordinatorState` with bounded `completed_read_observations` and include it in the checksummed checkpoint. On successful `FILE/read_file`, copy only normalized path, worker-observed SHA-256, workspace source generation, character count, and bounded redacted excerpt from `ExecutionResult`. Reject malformed or unconfined observations.
2. Pass a deterministic workspace source generation into `PersistentCoordinator` from the exact canonical binding used by `run_host`; include the observations in `host-receipt.json`.
3. Extend `DurableBatchHistory` to reconstruct read identities, not a lifetime set of paths. Legacy receipts become unbound observations and may not permanently forbid a path.
4. Replace `_bounded_completed_read_context`'s current-path semantics with a comparison against worker-observed identities:
   - current identity matches: reuse the cached redacted excerpt and forbid that identity;
   - hash/generation differs: omit stale context and allow a new read.
5. Exclude `FILE/read_file` from generic `_completed_task_signatures`; keep generic signature dedupe for other action types.
6. Change planner prompts and both validation passes to reject only a repeated read identity. Keep task-ID dedupe and all confinement/sensitive-path checks unchanged.
7. Bound cache entries, excerpt size, and total prompt size exactly as today or more tightly.

### R1B files

Production files:

- `extensions/autonomy-fabric/persistent_coordinator.py`
- `src/aos/autonomous_host.py`
- `src/aos/planning_kernel.py`

Focused tests:

- `extensions/autonomy-fabric/tests/test_persistent_coordinator.py`
- `tests/test_autonomous_host.py`
- `tests/test_planning_kernel.py`

`extensions/autonomy-fabric/native_workers.py` already emits `artifact_hashes` for reads and need not change unless implementation reveals a byte-hash/decoded-content mismatch. If changed, its only allowed R1B change is a typed read observation; no worker capability expansion.

## R1C — deterministic recovery churn guard

1. Add pure helpers in `runtime_worker.py` for canonical failure-family extraction and recovery fingerprint construction from checkpoint/state.
2. Persist in command `state.json`: fingerprint fields, completed-count baseline, `same_fingerprint_respawns`, `strategy_generation`, last worker exit code, and recovery disposition. Update them under the existing state lock.
3. In `_recover_one`, inspect the owned process exit status when available and the durable checkpoint otherwise. Reset the streak only on completed-batch/source-generation/family progress.
4. Enforce the bound:
   - repeat 1: normal resume;
   - repeat 2: persist strategy escalation and spawn once;
   - repeat 3: no spawn; durable `HUMAN_REQUIRED/RECOVERY_CHURN_GUARD` (or a versioned `REPAIR_HOLD`).
5. In `run_autonomous_project`, a strategy-generation change must bypass same-batch recovered objective/completion/repair artifacts and inject sanitized failure context into fresh objective selection. It must not discard completed-batch history.
6. Apply the same bound to in-process continuous `PlannerValidationExhausted` cycles; they must not spin merely because the PID remains alive.
7. Emit structured events for `runtime.recovery_strategy_escalated` and `runtime.recovery_churn_held` without arbitrary exception text.

### R1C files

Production files:

- `src/aos/runtime_server.py`
- `src/aos/runtime_worker.py`
- `src/aos/planning_kernel.py`
- `src/aos/runtime_contract.py` only if `REPAIR_HOLD` is introduced; prefer existing `HUMAN_REQUIRED` for the minimal patch

Focused tests:

- `tests/test_runtime_recovery.py`
- `tests/test_runtime_worker_failure.py`
- `tests/test_planning_kernel.py`
- `tests/test_runtime_contract.py` only if the state vocabulary changes

## R2 — `QuotaGovernor`

1. Add `src/aos/quota_governor.py`. Its key is `(provider_id, model_id, quota_scope, task_class)` and its states are `UNKNOWN`, `AVAILABLE`, `CONSTRAINED`, and `EXHAUSTED`.
2. The governor accepts only validated `RateLimitObservation` values, merges fields by the required truth order, and returns a decision containing `eligible`, `retry_at_epoch`, `reason`, and evidence source.
3. Persist an atomic versioned snapshot below the Runtime V1 root (proposed `resource-os/quota-governor.json`) under `exclusive_file_lock`. It is runtime resource state, never project state.
4. Provider selection evaluates, in order: policy/data/credential gates, paid gates, quota eligibility, then task-class health. A quota block must not mark health unhealthy; a health failure must not fabricate quota exhaustion.
5. The failover backend records observations immediately and uses the governor's exact deadline. `runtime_worker` persists the selected retry deadline and key; `runtime_server` wakes only if both quota and compatible health permit it.
6. A provider success without rate headers is not evidence that a prior quota window reset. Expiry or stronger provider metadata is required.
7. Paid providers retain the existing four-part fail-closed gate: billing class paid, `allow_paid_fallback=true`, `paid_fallback_enabled=true`, and both budgets positive. The governor never flips these settings.

### R2 files

Production files:

- `src/aos/quota_governor.py` (new)
- `src/aos/provider_registry.py`
- `src/aos/autonomous_host.py`
- `src/aos/provider_probe.py`
- `src/aos/provider_circuit.py`
- `src/aos/runtime_store.py`
- `src/aos/runtime_worker.py`
- `src/aos/runtime_server.py`

Focused tests:

- `tests/test_quota_governor.py` (new)
- `tests/test_provider_fabric_v2.py`
- `tests/test_provider_circuit.py`
- `tests/test_provider_reliability_audit.py`
- `tests/test_runtime_recovery.py`
- `tests/test_runtime_store.py`

## R3 — operational `ResourceLedger`

1. Add `src/aos/resource_ledger.py` with an append-only, fsynced, lock-protected JSONL event log plus an atomic derived snapshot. Use deterministic event IDs/idempotency keys so worker restart cannot double-count an attempt or completed work.
2. Store below the Runtime V1 root (proposed `resource-os/resource-ledger.jsonl` and `resource-os/resource-ledger-snapshot.json`). Never store it in a project workspace or canonical project control.
3. Event types are bounded and typed: `ATTEMPT_STARTED`, `ATTEMPT_FINISHED`, `RATE_OBSERVED`, `QUOTA_DECISION`, `HEALTH_OBSERVED`, `RESOURCE_USAGE`, `RESERVATION_CREATED`, `RESERVATION_RELEASED`, and `RECOVERY_DISPOSITION`.
4. Persist only identifiers, task class, billing class, token/request counts, bounded cost estimate/actual, typed observations, timestamps, and dispositions. Never persist prompts, generated content, raw headers/bodies, credentials, or arbitrary messages.
5. Make `QuotaGovernor` rebuild/verify its snapshot from ledger events. On mismatch or truncated tail, ignore only the incomplete final line, rebuild deterministically from valid events, and fail closed if earlier corruption breaks sequence/integrity.
6. Record provider usage from the normalized `usage` returned by adapters and recovery outcomes from runtime state. A retry/restart uses the same idempotency key and cannot duplicate accounted work.
7. Expose a bounded sanitized summary in runtime status; it is observational and grants no routing or production authority by itself.

### R3 files

Production files:

- `src/aos/resource_ledger.py` (new)
- `src/aos/quota_governor.py`
- `src/aos/runtime_store.py`
- `src/aos/autonomous_host.py`
- `src/aos/runtime_worker.py`
- `src/aos/runtime_server.py`

Focused tests:

- `tests/test_resource_ledger.py` (new)
- `tests/test_quota_governor.py`
- `tests/test_runtime_store.py`
- `tests/test_runtime_recovery.py`
- `tests/test_provider_fabric_v2.py`

## Cross-stage migration and rollback rules

- Every durable file gets `schema_version`, atomic replace, and backward-compatible read of the immediately preceding format.
- Old circuit files remain readable; no migration may turn unknown evidence into healthy evidence.
- Corrupt resource state fails closed for the affected provider/resource, not for durable project state.
- Rollback must tolerate new fields/files without deleting project work. Do not rewrite historical attempt journals.
- All new clocks are injectable in tests. Jitter is injected or disabled in deterministic tests.
- All new enums reject unknown persisted values or map them to explicit `UNKNOWN`; never silently treat unknown as healthy/available.
- `production=NO_GO`, `ag_backend_enabled=false`, and paid-provider defaults remain unchanged at every stage.

## Definition of implementation-ready

Implementation may start after this track because source locations, contracts, dependency order, migrations, file sets, and deterministic acceptance tests are specified. Promotion or live-runtime activation is not authorized by this document.
