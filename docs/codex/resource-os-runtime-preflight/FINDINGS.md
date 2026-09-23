# Resource OS Runtime P0 Preflight Findings

Track: `AOS-RESOURCE-OS-RUNTIME-P0-PREFLIGHT-20260923-01`
Audit base: `a0cfc2e329b36dca1964ed427ab17d8c82cbacb6`
Scope: read-only source audit and implementation planning
Production: `NO_GO`

## Executive finding

All six reported P0 defects are present at the audit base. They share one architectural cause: provider outcomes are reduced too early to provider-wide strings and counters, while completed read and recovery identities are too coarse. The safe correction is to preserve sanitized typed evidence at the adapter boundary, keep quota separate from health, key health by task class and failure streaks by family, and give reads and recoveries durable content/progress identities.

No source, test, live-runtime, protected-lineage, or LARI workspace mutation was performed by this track.

## P0-A — HTTP rate-limit metadata loss

### Exact loss locations

| Location | Loss |
|---|---|
| `src/aos/planner.py:68-77` | OpenAI SDK exceptions, including `RateLimitError`, are re-raised as message-only planner exceptions. The SDK response/status/headers are not retained. |
| `src/aos/providers/openai_compatible.py:181-202` | Responses-protocol exceptions are collapsed to string-only transient/credential/contract exceptions. |
| `src/aos/providers/openai_compatible.py:282-303` | Chat-completions exceptions are classified partly by message text; `response.headers`, status and reset fields are discarded. |
| `src/aos/providers/groq.py:150-173` | Groq/OpenAI-compatible errors lose response headers when wrapped. |
| `src/aos/providers/gemini.py:99-117` | Gemini failures are classified from `str(e)`; structured retry/quota metadata is not extracted. |
| `src/aos/providers/nemotron.py:122-160` | The adapter explicitly emits bounded labels and intentionally drops raw headers, but has no sanitized typed replacement for safe rate metadata. |
| `src/aos/providers/ollama.py:54-61` | `HTTPError` is caught through `URLError` and reduced to text; status and safe response headers are not retained. |
| `src/aos/autonomous_host.py:297-335` | All recognized failures set `message=None`; no typed rate observation or HTTP status reaches `ProviderAttempt`. |
| `src/aos/autonomous_host.py:364-375` | The attempt journal and circuit receive only `error_class`/`failure_class`; exact retry evidence is lost before scheduling. |
| `src/aos/provider_circuit.py:57-107,229-257` | Durable circuit state has no rate observation fields. `next_probe_at` is always calculated locally. |
| `src/aos/provider_probe.py:116-147,240-245,445-455` | Probe classification and sanitized probe projection omit retry/rate/quota metadata. |
| `src/aos/runtime_worker.py:551-567` | Waiting state derives its deadline only from the circuit's adaptive schedule, so it cannot respect an upstream `Retry-After`. |

The adapters also do not consistently request or inspect raw response wrappers on successful SDK calls, so safe `x-ratelimit-*` remaining/reset evidence present on successful HTTP responses is normally unavailable to AOS.

### Sanitized `RateLimitObservation` contract

The contract should live in a provider-neutral module (proposed: `src/aos/provider_observation.py`) and be carried by planner exceptions, provider attempts, the quota governor, and the resource ledger.

```json
{
  "schema_version": "1.0.0",
  "provider_id": "groq",
  "model_id": "model-name-or-null",
  "task_class": "repo_ui_planning",
  "observed_at": "2026-09-23T00:00:00+00:00",
  "http_status": 429,
  "classification": "RATE_LIMITED",
  "retry_after_seconds": 42.0,
  "retry_at_epoch": 1790121642.0,
  "request_limit": 30,
  "request_remaining": 0,
  "request_reset_epoch": 1790121642.0,
  "token_limit": 6000,
  "token_remaining": 0,
  "token_reset_epoch": 1790121642.0,
  "quota_scope": "MODEL",
  "evidence_source": "PROVIDER_METADATA",
  "field_sources": {
    "retry_at_epoch": "PROVIDER_METADATA",
    "token_reset_epoch": "PROVIDER_METADATA"
  }
}
```

All numeric fields are optional. The persisted form must contain only:

- an allowlisted provider/model identifier already present in policy;
- a canonical task class;
- normalized numeric limits, remaining counts, durations, and UTC epochs;
- a canonical classification/scope/source enum; and
- observation time and schema version.

It must never contain raw headers, raw bodies, URLs with query strings, request prompts, generated content, cookies, authorization values, arbitrary provider payloads, or arbitrary exception text. Header parsing must be case-insensitive and allowlist only `retry-after` and known numeric `x-ratelimit-*`/quota-reset aliases. `Retry-After` must support both delta-seconds and HTTP-date. Invalid, negative, non-finite, implausibly large, or unparsable values are dropped rather than persisted.

Truth is resolved per field, never per whole object:

1. `PROVIDER_METADATA` — a parsed response header or typed SDK field.
2. `OBSERVED_RUNTIME` — HTTP status and locally measured receive time/duration.
3. `PROVIDER_DOCUMENTATION` — a versioned policy fact, never inferred from an error string.
4. `ADAPTIVE_ESTIMATE` — bounded fallback only when all stronger evidence is absent.

Lower-ranked evidence must not shorten or overwrite a still-valid higher-ranked deadline. A newly observed higher-ranked deadline may replace an estimate. Clock calculations use a captured wall-clock value for durable epochs and monotonic time only for in-process elapsed measurement.

## P0-B — failure-family backoff contamination

### Exact contamination locations

| Location | Contamination |
|---|---|
| `src/aos/provider_circuit.py:57-74` | One `consecutive_failure_count` is stored per provider, not per failure family or task class. |
| `src/aos/provider_circuit.py:188-203` | Backoff selects a tier using the shared count even though the current `failure_class` may be unrelated to prior failures. |
| `src/aos/provider_circuit.py:229-257` | Every failure increments the same count before calculating the current family's deadline. |
| `src/aos/provider_circuit.py:205-227` | Any success resets the provider-wide count and closes the provider-wide circuit. |
| `src/aos/provider_circuit.py:395-502` | Registry aggregation copies only the latest failure's provider-wide count, retaining the same coarse model across commands. |
| `src/aos/autonomous_host.py:297-375` | Contract, credential, quota, capacity, network, timeout, and local-service outcomes all feed the same `record_failure` path. |
| `src/aos/runtime_server.py:418-448` | Synthetic probe failures/successes update the same provider-wide circuit. |

This proves the reported sequence: `SERVER_CAPACITY`, then `NETWORK_UNAVAILABLE`, then a first `CONTRACT_FAILURE` gives the contract family a third-tier count. The inverse is also unsafe: an unrelated success clears a still-relevant quota or workload-specific failure.

### Required behavior

Persist `family_streaks` under a `(provider_id, model_id, task_class)` health record. Canonical families are at least:

- `NETWORK`
- `TIMEOUT`
- `SERVER_CAPACITY`
- `CONTRACT`
- `CREDENTIAL`
- `LOCAL_SERVICE`
- `UNKNOWN`

Quota/rate/credit is not a health family. It is recorded by `QuotaGovernor`. A failure increments only its canonical family's streak and derives fallback backoff only from that streak. A success for the same task class clears health failure streaks only according to an explicit success policy; it never clears a quota deadline. A success in `small_reasoning` does not clear or close `large_context` or `repo_ui_planning` evidence.

For migration, legacy `consecutive_failure_count` is retained as display-only compatibility data or mapped once to the legacy `UNKNOWN` task/family bucket. It must not seed a newly observed specific family with an elevated tier.

## P0-C — contract failure subtype loss

### Exact loss locations

`PlannerContractError` is an empty marker class at `src/aos/planner.py:21-23`. Adapters encode meaning only in strings:

- bad request: `planner.py:74-77`, `openai_compatible.py:199-202,298-303`, `groq.py:168-173`, `nemotron.py:152-160`;
- no choices: `openai_compatible.py:305-306`, `groq.py:175-177`, `nemotron.py:162-163`;
- empty content: `planner.py:100-101`, `openai_compatible.py:229-230,315-320`, `groq.py:186-191`, `gemini.py:144-145`, `nemotron.py:172-177`, `ollama.py:69-74`;
- invalid JSON: `planner.py:103-106`, `openai_compatible.py:333-336`, `groq.py:193-196`, `gemini.py:147-150`, `nemotron.py:180-183`, `ollama.py:76-79`;
- schema validation: `openai_compatible.py:338-341`, `groq.py:198-204`, `gemini.py:152-158`, `nemotron.py:185-191`, `ollama.py:81-86`;
- refusal/bad finish: `planner.py:79-101`, `openai_compatible.py:204-230,308-320`, `groq.py:179-191`, `gemini.py:133-145`, `nemotron.py:165-177`.

The decisive second loss occurs at `src/aos/autonomous_host.py:297-301`: every subtype becomes `CONTRACT_FAILURE`, and `message` is deliberately set to `None`. `ProviderAttempt` at `autonomous_host.py:105-122` has no subtype field. `provider_probe.py:129-130` also collapses every contract error to the same family.

### Safe taxonomy

`PlannerContractError` should require a canonical subtype:

| Subtype | Meaning | Safe detail allowed |
|---|---|---|
| `BAD_REQUEST` | Provider rejected the request/schema. | SDK exception class, HTTP status, bounded provider error code from an allowlist. |
| `NO_CHOICES` | Completion response contains no choices/candidates. | Count `0`; no payload. |
| `EMPTY_CONTENT` | Expected textual/structured content is absent. | Response shape label only. |
| `INVALID_JSON` | Returned content cannot be decoded as JSON. | Parser class and line/column only; never content or parser message containing content. |
| `SCHEMA_VALIDATION` | Decoded object fails the canonical schema. | Validator keyword and bounded JSON Pointer; never rejected value. |
| `REFUSAL` | Provider/model explicitly refused. | Canonical refusal code/category only; no raw refusal text. |
| `BAD_FINISH_REASON` | Finish/status is not accepted and is not the separately classified token-capacity condition. | Allowlisted normalized finish reason. |
| `PROVIDER_CONTRACT_ERROR` | Provider/SDK shape or protocol contract is otherwise incompatible. | SDK exception class and allowlisted provider code only. |

`ProviderAttempt` should persist `failure_class="CONTRACT_FAILURE"`, `contract_subtype`, and optional structured `safe_detail`; `message` remains absent for known failures. The circuit family remains `CONTRACT`, while subtype telemetry remains available for diagnosis and recovery strategy. Unknown or unallowlisted details become `PROVIDER_CONTRACT_ERROR` with no detail.

## P0-D — content-aware read deduplication

### Root cause

The worker already computes a SHA-256 for a successful `read_file` (`extensions/autonomy-fabric/native_workers.py:162-187`) but that result is dropped from durable coordinator state and the host receipt (`extensions/autonomy-fabric/persistent_coordinator.py:254-280`; `src/aos/autonomous_host.py:594-608`). The planning kernel later reconstructs only task IDs, exact payload signatures, and paths:

- `_bounded_completed_read_context` (`planning_kernel.py:767-853`) reopens the current file and computes a current hash, but does not compare it to the hash observed by the completed worker action;
- `_completed_task_signatures` (`planning_kernel.py:864-890`) makes the exact `FILE/read_file` payload lifetime-global;
- `reconstruct_batch_history` (`planning_kernel.py:893-1011`) stores completed read paths and signatures without content identity;
- `compile_execution_plan` builds unconditional forbidden path/signature sets (`planning_kernel.py:1775-1788`), instructs the planner never to reread them (`1821-1827`), and rejects them twice (`1943-1973`, `2064-2097`);
- `run_autonomous_project` unions all historical paths/signatures (`planning_kernel.py:2441-2444,2587-2615`).

Therefore changing a file does not make its read legal: both the path set and the unchanged payload signature still reject it. The current `content_sha256` is informational only.

### Required identity and cache behavior

Use this identity:

```text
ReadIdentity = normalized_case_path
             + content_sha256_of_bytes_observed_by_worker
             + workspace_source_generation
```

`workspace_source_generation` must be a deterministic digest of the project identity and the exact canonical source/execution generation bound by the batch (at minimum `project_id`, `canonical_source_sha`, and `canonical_execution_base_sha`; include repository HEAD if it is independently authoritative). Do not persist an absolute workspace path as identity.

On a successful worker read, persist a bounded, redacted `completed_read_observation` in the coordinator checkpoint and host receipt: identity, normalized path, byte hash, generation, character count, and bounded redacted excerpt. Never persist raw content beyond the existing bounded/redacted policy.

Before planning:

1. Hash the current confined file locally.
2. If path, current hash, and current generation match a durable observation, place its cached excerpt in `completed_read_context` and forbid only that exact identity. No new worker action is spent.
3. If the hash or generation differs, do not forbid the read and do not present the stale excerpt as current. A new `read_file` is legitimate.
4. Exclude `FILE/read_file` from generic lifetime task signatures; its specialized identity owns deduplication.
5. Treat legacy receipts without an observed hash/generation as `LEGACY_UNBOUND`: they may cause one fresh bounded read to upgrade evidence, never a permanent path ban.

## P0-E — recovery churn guard

### Root cause

`RuntimeEngine._recover_one` (`src/aos/runtime_server.py:560-589`) decides to respawn from only terminal state, retry time, and PID liveness. It does not compare batch number, completed-batch progress, failure family, prior respawns, or exit status. The recovery loop repeats this check every three seconds (`runtime_server.py:656-661`). `_spawn_worker` records an unstructured respawn event (`512-558`) but no durable recovery fingerprint or bound.

The worker increments a total `recovery_count` (`src/aos/runtime_worker.py:396-419`), but that value is not used as a guard. Planner validation exhaustion returns `BOUNDED_RUN_EXHAUSTED` without advancing a batch (`planning_kernel.py:2628-2643`), and continuous execution immediately starts another cycle (`runtime_worker.py:534-549`). Thus neither in-process repeated validation nor post-exit recovery has a same-batch/no-progress bound.

### Deterministic guard

Persist a recovery fingerprint in command state:

```text
fingerprint = batch_number
            + completed_batch_count_baseline
            + canonical_failure_family
            + objective_id_or_NONE
            + workspace_source_generation
```

Maintain `same_fingerprint_respawns`, `strategy_generation`, `last_worker_exit_code`, and `last_recovery_at`. Reset the streak only when completed-batch count advances, the source generation changes, or the failure family changes. PID changes alone are not progress.

Bounded policy:

- first matching recovery: permit one normal lineage-preserving resume;
- second matching recovery with no progress: increment `strategy_generation`, invalidate same-batch recovered objective/repair artifacts, and force a fresh objective/strategy selection using the failure subtype/family as context;
- third matching recovery with no progress: do not spawn. Persist `HUMAN_REQUIRED` (or a new explicitly nonterminal `REPAIR_HOLD` if the runtime contract is versioned for it) with reason `RECOVERY_CHURN_GUARD`, the sanitized fingerprint fields, and `retry_after_epoch=0`.

No step may create a new command ID, blindly resume a stale agent session, enable AG/Codex/Qwen, or enable paid fallback. An operator may resume the same lineage only through an explicit state transition that increments a durable operator/repair generation.

## P0-F — task-class-aware provider health

### Gap locations

| Location | Gap |
|---|---|
| `src/aos/provider_probe.py:49-89,228-239` | The live probe is a small fixed synthetic structured response and emits no task class. |
| `src/aos/provider_circuit.py:57-74` | One provider-wide circuit stores all health evidence. |
| `src/aos/provider_circuit.py:205-227` | Any success closes that provider-wide circuit. |
| `src/aos/autonomous_host.py:210-212,216-293` | Backend health is always reported healthy and actual requests do not supply a canonical task class to circuit decisions. |
| `src/aos/planning_kernel.py:1093-1121` | Reasoning requests distinguish only task IDs/operation class; they do not declare workload class or context-size band. |
| `src/aos/runtime_server.py:394-463` | Any successful small activation probe clears waiting deadlines for all lineages. |
| `src/aos/runtime_server.py:604-654` | Any provider-wide `CLOSED` record wakes every waiting reasoning command, regardless of required workload. |

### Required evidence model

Use the closed enum:

- `small_reasoning`
- `structured_planning`
- `repo_ui_planning`
- `large_context`
- `agentic_execution`

Every request and probe must declare exactly one task class. Health evidence is keyed by provider, model, and task class and includes observed context/output size bands. Evidence can satisfy only the same class, except for an explicit conservative compatibility table. Initially that table should have no upward implications: `small_reasoning` success proves only `small_reasoning`.

A waiting command persists `required_task_class`. Wake-up requires compatible fresh health evidence and a non-blocking quota decision for that same provider/model/class. Provider health, provider quota, credential availability, local service availability, and policy eligibility remain distinct axes.

## Invariant check

The proposed design preserves the North Star invariants:

- backend and provider state remain outside durable project state;
- quota deadlines survive provider/worker restart without implying project loss;
- provider failures cause failover, bounded wait, or hold, never project failure by default;
- no AG/Codex/Qwen layer is introduced;
- content-bound completed work is reused, while changed content is eligible for reread;
- stale sessions and same-fingerprint recoveries are never resumed without a deterministic bound; and
- paid APIs remain behind the existing explicit policy and positive-budget gates.
