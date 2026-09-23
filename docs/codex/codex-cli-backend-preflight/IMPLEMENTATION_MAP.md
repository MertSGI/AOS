# Post-Track-A implementation map

This is a proposed source-touch matrix only. No implementation is authorized on this branch. All work must begin from the independently accepted final FreeLLMAPI Track A SHA, not from `a0cfc2e329b36dca1964ed427ab17d8c82cbacb6`.

## Files to add

| File | Purpose |
|---|---|
| `extensions/autonomy-fabric/codex_cli_backend.py` | `CodexCliExecutionBackend`; exact argv construction; JSONL parsing; app-server quota read; availability classification; start/resume/interrupt; sanitized `ExecutionResult`. |
| `src/aos/workers/codex_cli_probe.py` | Disposable behavioral probe and machine-local capability attestation bound to executable hash/version, auth mode, contract version, and structured capabilities. |
| `src/aos/workspace_fingerprint.py` | Versioned content-sensitive Git workspace fingerprint implementation, including index, tracked worktree, untracked binary files, symlinks, deletions, modes, and initialized submodules. |
| `schemas/v0.1/agentic_execution_checkpoint.schema.json` | Durable identity, ContextPack, completed-work ledger, superseded-session history, and availability snapshot schema. |
| `schemas/v0.1/execution_resource_policy.schema.json` | Execution-resource policy separate from inference-provider policy; includes `SUBSCRIPTION_INCLUDED`, `api_key_fallback_enabled=false`, sandbox, thresholds, and production gate. |
| `descriptors/codex-cli.execution-resource-policy.json` | Initial non-production Codex resource declaration with API fallback disabled. |

## Files to modify

| File | Exact change |
|---|---|
| `extensions/autonomy-fabric/execution_backend.py` | Add `BackendClass`, `SUBSCRIPTION_INCLUDED`, `ExecutionAvailabilityState`, `ExecutionAvailabilitySnapshot`, minimal new capabilities, and typed optional agentic identity/checkpoint fields on request/result. Preserve compatibility for existing backends. |
| `extensions/autonomy-fabric/execution_router.py` | Prefer free local, then subscription-included agentic capacity, then existing free-tier resources; filter on structured availability; reroute `DEGRADED`/quota results; never make paid API fallback implicit. |
| `extensions/autonomy-fabric/run_registry.py` | Add the mandatory durable session fields to `RunIdentity`; replay every field; journal session supersession/checkpoint events; prevent terminal completed work from re-entry. |
| `extensions/autonomy-fabric/persistent_coordinator.py` | Persist agentic execution checkpoints and completed work-unit signatures/artifact hashes; resume only remaining nodes; map Codex quota loss to a non-terminal waiting/reroute path. |
| `extensions/autonomy-fabric/supervisor.py` | Add exact resume compatibility checks, workspace fingerprint validation, session supersession, and owned interrupt integration. This is the real file; do not create the nonexistent `parallel_supervisor.py`. |
| `src/aos/autonomous_host.py` | Register `CodexCliExecutionBackend` through the execution router; add attempt/availability evidence; keep it out of `_PROVIDER_FACTORIES`; preserve production human gate. |
| `src/aos/runtime_worker.py` | Add Codex availability/re-entry disposition and events; persist same-objective lineage and retry time; recover from the AOS checkpoint rather than assuming the CLI rollout is readable. |
| `src/aos/provider_registry.py` | Either load the new execution-resource policy or expose a sibling execution-resource registry. Keep paid-provider fields explicit and disabled; do not represent Codex subscription auth as `credential_env_var`. |
| `src/aos/provider_circuit.py` | Preserve structured Codex failure class and service reset time; store quota separately from binary/network health; schedule half-open probes without retry storms. |
| `src/aos/planning_kernel.py` | Build the durable bounded ContextPack from canonical state, completed IDs/signatures, fresh read context, and artifact hashes; prohibit completed work in fresh-session handoff. |
| `src/aos/validate.py` | Register the two new schemas using the existing validation mechanism. |
| `pyproject.toml` | Include new modules/schemas/descriptors in packaged artifacts if current package discovery does not already cover them. |

`src/aos/process_utils.py` should be reused unchanged initially. Modify it only if implementation demonstrates that a generic, tested line-stream/cancellation helper is needed; `popen_headless` and `OwnedProcess` already provide process-tree ownership.

`src/aos/workers/antigravity.py`, `src/aos/workers/antigravity_probe.py`, and `extensions/autonomy-fabric/antigravity_adapter.py` are reference implementations and should not be edited merely to share code. Extract a common helper only after tests demonstrate real, stable duplication.

## Interfaces reused without semantic duplication

- `ExecutionBackend`, `ExecutionRequest`, and `ExecutionResult`
- `ExecutionRouter.execute_with_failover`
- `AgentRunRegistry` and `FileRunJournal`
- `PersistentCoordinator` checksum-bound checkpoint pattern
- `PlanningArtifactWatcher`/RuntimeStore structured state and event publication
- `ProviderCircuitBreakerRegistry` persistence/backoff/half-open logic
- `popen_headless`, `OwnedProcess`, `run_headless`, and `terminate_process_tree`
- `redact_secrets`
- planning-kernel completed task IDs, task signatures, batch reconstruction, and bounded completed-read context
- authority router production/destructive/payment human gates

## Interfaces that require extension

1. `ExecutionHealth` alone is insufficient. Add an availability snapshot rather than adding more ambiguous health values.
2. `ExecutionCost` needs `SUBSCRIPTION_INCLUDED` so routing can prefer subscription capacity without calling it free-tier API inference.
3. `ExecutionCapability` needs only `TEST_EXECUTION` and `LONG_HORIZON_AGENTIC_WORK`; existing file/process/Git capabilities cover the rest.
4. `RunIdentity` and replay need typed session/checkpoint/fingerprint fields.
5. `ExecutionRequest`/`ExecutionResult` need typed ContextPack/session updates instead of burying durable state only in arbitrary `payload`/`evidence_payload` dictionaries.
6. Runtime dispositions need a non-terminal Codex-unavailable/reroute representation. It may normalize into the existing `WAITING_FOR_REASONING_PROVIDER` externally, but persisted failure class must retain `QUOTA_EXHAUSTED`, `AUTH_UNAVAILABLE`, `TEMPORARILY_UNAVAILABLE`, or `CONTRACT_FAILURE`.

## Test files to add

| File | Coverage |
|---|---|
| `extensions/autonomy-fabric/tests/test_codex_cli_backend.py` | argv order, stdin prompt, strict JSONL parser, terminal contract, redaction, exact-id start/resume, availability mapping, API-key rejection. |
| `tests/test_codex_cli_capability.py` | executable identity/attestation, doctor JSON auth parsing, disposable probe, version drift, binary hash drift, capability store. |
| `tests/test_codex_workspace_fingerprint.py` | clean determinism plus staged, unstaged, untracked, deletion, rename, executable bit, symlink, binary/NUL bytes, and initialized-submodule changes. |
| `tests/test_codex_runtime_reentry.py` | quota loss, same-objective reroute, unchanged exact resume, stale-session rejection, fresh ContextPack handoff, restart persistence, no duplicate completed work. |

## Existing test files to extend

| File | Added cases |
|---|---|
| `extensions/autonomy-fabric/tests/test_execution_router.py` | subscription cost order; low/scarce selection; quota/auth/contract failover; paid fallback remains disabled. |
| `extensions/autonomy-fabric/tests/test_run_registry.py` | all mandatory fields round-trip/replay; superseded session history; corrupt/truncated journal fail-closed. |
| `extensions/autonomy-fabric/tests/test_persistent_coordinator.py` | completed Codex work survives restart and is not redispatched; checkpoint update ordering. |
| `extensions/autonomy-fabric/tests/test_supervisor.py` | exact UUID only; fingerprint/source/version compatibility; stale-workspace fresh start; lock cleanup on interrupt. |
| `tests/test_autonomous_host.py` | Codex backend registration as execution resource; no registration as ordinary planner provider; production stays `NO_GO`. |
| `tests/test_planning_kernel.py` | ContextPack completed-work ledger and forbidden signatures across backend change/restart. |
| `tests/test_provider_circuit.py` | service reset timestamps, quota versus health, half-open recovery, no retry storm. |
| `tests/test_runtime_worker_failure.py` | Codex unavailability is non-terminal and preserves lineage/checkpoint. |
| `tests/test_schemas.py` | valid/invalid checkpoint and execution-resource policy fixtures. |

## Failure-injection acceptance matrix

Implementation is incomplete until automated tests prove all of the following:

- executable missing, ambiguous, hash changed, or version changed;
- ChatGPT auth unavailable and API-key-only auth rejected;
- `OPENAI_API_KEY`/`CODEX_API_KEY` present in parent but absent from child;
- app-server quota read unavailable, malformed, stale, scarce, and exhausted;
- JSONL malformed, unknown event, exit `0` without `turn.completed`, `turn.failed`, explicit error, and non-zero exit;
- process timeout and interruption before thread metadata flush;
- interruption after one completed turn, with AOS checkpoint recovery;
- exact-id resume with unchanged fingerprint;
- staged, unstaged, untracked, binary, deletion, mode, symlink, and submodule mismatch each blocks resume;
- fallback backend changes workspace/source and old Codex UUID is archived/superseded, never resumed;
- completed work-unit id/signature/artifact is rejected on retry, restart, fallback, and fresh Codex re-entry;
- unavailable Codex reroutes the same objective; all-backend outage becomes waiting, not project failure;
- paid OpenAI provider factory/executor is never invoked when API fallback is disabled;
- output containing synthetic keys, tokens, home paths, raw prompts, reasoning, or tool parameters is redacted/not persisted;
- write-scope escape, unexpected changed path, forbidden Git operation, production action, and destructive action fail closed;
- corrupt checkpoint or journal fails closed without blindly resuming a CLI session.

## Context handoff

The durable ContextPack should be schema-bound and contain only:

- objective id/text and authority id;
- canonical `source_sha` and current workspace fingerprint;
- checkpoint id and last successful turn;
- completed work-unit ids and signatures;
- verified artifact paths/hashes and last successful artifact;
- bounded fresh-read context already accepted by the planning kernel;
- remaining work and current failure/availability reason;
- explicit write/read/command/network boundaries.

It must not contain raw credentials, auth storage, unrestricted transcript history, hidden reasoning, raw tool parameters, or unredacted stderr.

## Delivery sequence

1. Rebase/recreate the implementation branch from the accepted final Track A SHA and re-audit overlaps.
2. Land contract/schema/fingerprint types with unit tests.
3. Land backend parser, auth/quota probes, and disposable capability test.
4. Wire registry/router/coordinator/runtime re-entry and ContextPack.
5. Run failure injection, restart, workspace mutation, and no-duplicate suites.
6. Run a bounded disposable live proof with ChatGPT auth and API variables absent.
7. Produce evidence for an independent human production decision. Do not switch `PRODUCTION` from `NO_GO` in this track.
