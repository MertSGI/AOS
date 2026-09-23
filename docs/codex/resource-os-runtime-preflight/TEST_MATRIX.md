# Resource OS Runtime Deterministic Test Matrix

All tests are offline/unit or isolated temporary-runtime tests. No live provider, production runtime, protected command lineage, paid API, or LARI workspace may be used.

| ID | Requirement | Deterministic setup | Required assertion | Target test file/stage |
|---|---|---|---|---|
| RM-01 | `Retry-After` delta respected | Build a synthetic SDK `RateLimitError`/HTTP response with `Retry-After: 120`; inject `now=1000` and zero jitter. | Observation source is `PROVIDER_METADATA`; `retry_at_epoch=1120`; circuit/governor cannot schedule earlier. | `test_provider_observation.py`, R1/R2 |
| RM-02 | `Retry-After` HTTP-date respected | Header is a fixed RFC 7231 date 90 seconds after injected wall clock. | Exact epoch is parsed; adaptive estimate does not overwrite it. | `test_provider_observation.py`, R1 |
| RM-03 | Rate metadata sanitized | Synthetic headers include allowed numeric rate fields plus authorization, cookie, URL/query, arbitrary vendor text and a fake secret. | Serialized observation contains only allowlisted typed fields; forbidden header names/values and secret are absent from observation, attempt journal, circuit snapshot, events, and ledger. | `test_provider_observation.py`, `test_autonomous_host.py`, R1/R3 |
| RM-04 | Truth order | Merge adaptive estimate, documented limit, runtime status, then provider header in varying arrival order. | Per-field result is provider metadata > runtime evidence > documentation > adaptive estimate; weaker evidence never shortens an active stronger deadline. | `test_provider_observation.py`, R1 |
| RM-05 | Metadata survives adapter boundary | Each adapter raises a fixture error carrying headers/typed retry detail. | Caught `PlannerTransientError` contains equivalent sanitized `RateLimitObservation`, never raw headers/body. | `test_providers.py`, `test_nemotron_fabric.py`, R1 |
| FH-01 | Quota is not health | Record task-class health success, then a 429 quota observation; separately record network failure with quota available. | First case: health remains healthy and quota blocks. Second: quota remains available and health blocks. | `test_quota_governor.py`, `test_provider_circuit.py`, R2 |
| FH-02 | Failure-family counter isolation | With deterministic clock/jitter, record `SERVER_CAPACITY`, `NETWORK_UNAVAILABLE`, then first `CONTRACT_FAILURE`. | Each family streak is 1; contract uses first contract tier, not third provider-wide tier. | `test_provider_circuit.py`, R1 |
| FH-03 | Same-family backoff progression | Record three network failures with no intervening network success. | Network streak/tier advances 1→2→3; other family streaks stay zero. | `test_provider_circuit.py`, R1 |
| FH-04 | Family-specific recovery | Record capacity and contract failures; then a task-class success under the explicit reset policy. | Only the documented health buckets reset; quota evidence is unchanged. | `test_provider_circuit.py`, R1 |
| CT-01 | Contract subtype preserved | Parameterize all eight subtypes through a fake provider and `ProviderFailoverReasoningBackend`. | Attempt journal contains `failure_class=CONTRACT_FAILURE` plus exact subtype and no raw message. | `test_autonomous_host.py`, R1 |
| CT-02 | Contract detail safe | Use invalid JSON containing a fake secret and schema failure containing a sensitive rejected value. | Telemetry contains only parser position or schema keyword/pointer; secret/value/raw content absent. | `test_providers.py`, R1 |
| CT-03 | Unknown provider contract fallback | Raise an unrecognized SDK shape/error with sensitive text. | Subtype is `PROVIDER_CONTRACT_ERROR`, safe detail is empty/allowlisted, raw text not persisted. | `test_provider_observation.py`, R1 |
| RD-01 | Changed file can be reread | Complete read of `ui.tsx`, persist hash/generation, then change bytes without changing path and compile the next plan. | New identity differs; `read_file(ui.tsx)` is accepted and generic signature dedupe does not reject it. | `test_planning_kernel.py`, R1B |
| RD-02 | Source generation permits reread | Keep bytes/path equal but change the canonical workspace source generation. | Old observation is not treated as current; a new read is legal. | `test_planning_kernel.py`, R1B |
| RD-03 | Unchanged file reuses cache | Persist a completed read observation, keep file/generation unchanged, and invoke replanning. Instrument worker executor. | Cached hash-bound redacted excerpt is supplied; duplicate read is pruned/rejected; worker read count remains zero. | `test_planning_kernel.py`, R1B |
| RD-04 | Restart reconstruction | Persist coordinator/host receipt, create a new planning-kernel process/object, and reconstruct history. | Same read identity/cache is recovered and unchanged read is reused after restart. | `test_persistent_coordinator.py`, `test_planning_kernel.py`, R1B |
| RD-05 | Legacy receipt upgrade | Reconstruct an old receipt with only task ID/path and no hash/generation. | It is marked `LEGACY_UNBOUND`, does not permanently ban path, and one fresh read upgrades it. | `test_planning_kernel.py`, R1B |
| RD-06 | Sensitive read cache exclusion | Complete a read whose path or content triggers existing sensitive/redaction rules. | No secret/raw sensitive content is persisted or prompted; path confinement remains enforced. | `test_planning_kernel.py`, R1B |
| RC-01 | No infinite same-batch respawn | Fake a dead worker repeatedly with constant batch, source generation, completed count, and validation family; call `_recover_one` more times than the limit. | Spawn count is bounded; final state is durable hold; further recovery-loop passes spawn nothing. | `test_runtime_recovery.py`, R1C |
| RC-02 | Strategy escalation before hold | Repeat the same fingerprint twice. | Second repeat increments `strategy_generation`, emits one escalation event, and bypasses recovered objective/repair artifacts. | `test_runtime_recovery.py`, `test_planning_kernel.py`, R1C |
| RC-03 | Progress resets guard | After repeated fingerprint, advance completed-batch count or source generation. | Same-fingerprint counter resets and one normal recovery is permitted. | `test_runtime_recovery.py`, R1C |
| RC-04 | Failure family changes | Keep batch/count fixed but change failure family. | A new fingerprint/streak starts; the old family's repeats do not contaminate it. | `test_runtime_recovery.py`, R1C |
| RC-05 | In-process validation bounded | Make every planner proposal and repair fail with the same local validation family in a continuous command. | The worker does not loop indefinitely; it escalates once then enters durable hold at the defined bound. | `test_runtime_worker_failure.py`, `test_planning_kernel.py`, R1C |
| TH-01 | Small probe not large-context health | Record `small_reasoning` probe success and an existing `large_context` failure/unknown record. | Large-context health remains failed/unknown; its command is not woken. | `test_provider_circuit.py`, `test_runtime_recovery.py`, R1 |
| TH-02 | Task-compatible wake | Two waiting commands require `small_reasoning` and `repo_ui_planning`; observe only small success. | Only the small command becomes eligible; repo/UI deadline/state is unchanged. | `test_runtime_provider_source_resilience_20260922.py`, R1 |
| TH-03 | Actual workload evidence scoped | Successful repo/UI request follows a failed small probe. | Repo/UI record becomes healthy without fabricating small/large/agentic health. | `test_autonomous_host.py`, R1 |
| QG-01 | Exact quota deadline | Feed a provider observation with remaining zero and exact reset epoch. | Governor returns `EXHAUSTED`, blocks selection until exact epoch, then becomes eligible without altering health. | `test_quota_governor.py`, R2 |
| QG-02 | Adaptive fallback only last | Feed a 429 without safe metadata; provide deterministic adaptive estimator. | Estimate is used and labeled `ADAPTIVE_ESTIMATE`; later provider metadata replaces it. | `test_quota_governor.py`, R2 |
| QG-03 | Restart preserves retry truth | Persist governor snapshot with exact provider deadline, instantiate a fresh governor/runtime, and use an earlier injected clock. | Same deadline/source survives; no early probe/spawn occurs. | `test_quota_governor.py`, `test_runtime_recovery.py`, R2 |
| RL-01 | Ledger restart/replay | Append rate/quota/usage events, recreate ledger/governor, and rebuild snapshot. | Rebuilt quota/retry/accounting state equals pre-restart state byte-for-byte except generated snapshot timestamp if explicitly excluded. | `test_resource_ledger.py`, R3 |
| RL-02 | Idempotent retry accounting | Append the same provider-attempt event twice with one idempotency key. | One logical attempt/usage charge is counted; duplicate event is rejected or no-op. | `test_resource_ledger.py`, R3 |
| RL-03 | Truncated tail recovery | End JSONL with an incomplete final line after valid fsynced events. | Valid prefix replays; incomplete tail is ignored/quarantined; no quota becomes available by default. | `test_resource_ledger.py`, R3 |
| RL-04 | Earlier corruption fails closed | Corrupt a non-final event or sequence/hash link. | Affected resource is unavailable/hold; project state and completed-work records remain intact. | `test_resource_ledger.py`, R3 |
| PAY-01 | Paid provider remains disabled | Configure a healthy paid provider and abundant quota but leave each existing paid gate false/zero in a parameterized matrix. | Router/probe never selects or invokes paid provider. | `test_provider_fabric_v2.py`, `test_provider_reliability_audit.py`, R2/R3 |
| INV-01 | Completed work not duplicated | Crash after ledger/read completion persistence but before worker terminal update, then recover. | Idempotency/read identity prevents duplicate action and resource charge. | `test_resource_ledger.py`, `test_runtime_recovery.py`, R3 |
| INV-02 | Project state independent | Corrupt/expire provider quota and health state while retaining a completed project checkpoint. | Project checkpoint/history is unchanged and recoverable; resource state enters fail-closed hold independently. | `test_runtime_recovery.py`, `test_planning_kernel.py`, R2/R3 |

## Determinism requirements

- Inject wall and monotonic clocks; do not sleep.
- Inject jitter or set it to zero.
- Use temporary directories and fresh in-memory/fake provider clients.
- Assert exact serialized fields and explicitly assert forbidden data is absent.
- Do not depend on provider documentation, network responses, environment credentials, or current date/time.
- For respawn tests, replace process creation/PID checks with fakes and call one recovery iteration directly.
- For restart tests, instantiate new objects from persisted files; do not reuse in-memory state.

## Stage gates

1. R1: RM-01 through RM-05, FH-02 through FH-04, CT-01 through CT-03, TH-01 through TH-03.
2. R1B: RD-01 through RD-06 plus existing >60-batch history tests.
3. R1C: RC-01 through RC-05 plus existing pause-safe and provider-wait tests.
4. R2: FH-01, QG-01 through QG-03, PAY-01.
5. R3: RL-01 through RL-04, INV-01, INV-02, and PAY-01 again.

Full regression is required after every stage. A pass does not authorize production or live-runtime mutation.
