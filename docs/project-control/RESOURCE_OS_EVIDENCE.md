# Resource OS Evidence

Track: `AOS-RESOURCE-OS-MASTER-20260923-01`

## Phase 0 source evidence

- `git fetch origin --prune`: passed on 2026-09-23.
- `git ls-remote origin refs/heads/feature/aos-freellmapi-local-meta-provider-20260922-01`: `ccdebfd7b5bdc32f7e95634ef2c464293356c872`.
- Accepted `docs/codex/freellmapi-integration/STATE.md`: `STATUS=CONTROLLER_REVIEW_READY`.
- Accepted `docs/codex/freellmapi-integration/STATE.json`: `CONTROLLER_REVIEW_READY=true`, full canonical suite `982 passed, 8 skipped, 26 deselected`.
- Isolated worktree branch created directly at the accepted SHA.
- Mandatory runtime preflight read from `origin/audit/aos-resource-os-runtime-preflight-20260923-01` at `d0ea8064f2d349e0b63c1fe5009a4901981464a8`.
- Mandatory Codex CLI preflight read from `origin/audit/aos-codex-cli-backend-preflight-20260923-01` at `66d38917f60d17eedc1fa370649db1ddf8045681`.
- All six required preflight documents were read completely from those exact remote heads before implementation.
- `origin/audit/aos-jev-decision-layer-preflight-20260923-01` was absent at Phase 0; the local placeholder branch had no Jev implementation state, so Jev remains optional and unconsumed.
- Existing Antigravity worker/probe/adapter and autonomy-fabric registry/supervisor/coordinator/backend/router surfaces were inventoried for reuse.

## Safety evidence

- The original `C:\Projects\AOS` checkout contains unrelated user work and was not modified by this track.
- No runtime, provider, Antigravity, Codex, Qwen, or Jev live execution has been performed.
- No protected command ID or protected workspace has been read for mutation or changed.
- No credential, API key, paid endpoint, deployment, or production action has been used.

## Phase checkpoints

### Phase 0 — canonical program controls

- Verified the accepted FreeLLMAPI remote head and both mandatory audit remote heads after a fresh origin fetch.
- Verified the accepted FreeLLMAPI durable state reports `CONTROLLER_REVIEW_READY`.
- Verified the isolated Resource OS branch starts exactly at the accepted SHA and did not modify that source branch.
- Parsed `RESOURCE_OS_STATE.json` successfully with PowerShell `ConvertFrom-Json`.
- `git diff --check`: PASS.
- Live/provider/paid/production actions: none; `PAID_API_FALLBACK=DISABLED`; `PRODUCTION=NO_GO`.

Later phase checkpoint commits, focused commands/results, remote equality, and disposable proofs are appended here as they are accepted.

### R1 — provider observations and task-class health

- Added immutable, validated, sanitized `RateLimitObservation` plus closed task, source, failure-family, and contract-subtype enums.
- `Retry-After` delta/date and allowlisted numeric rate metadata survive adapter, attempt journal, circuit, and probe boundaries without raw headers/bodies or provider text.
- Provider contract failures preserve one of eight typed subtypes with bounded allowlisted details; known failure messages remain absent from attempt evidence.
- Circuit schema `2.0.0` stores task/model health records and independent family streaks; quota/rate/credit does not increment health streaks.
- Actual requests and probes declare a task class; `small_reasoning` evidence cannot clear or wake `structured_planning`, `repo_ui_planning`, `large_context`, or `agentic_execution` work.
- Focused provider/runtime suite: `166 passed, 1 deselected in 14.68s`.
- Planning-kernel/worker regression: `57 passed in 31.41s`.
- Python compileall: PASS. `git diff --check`: PASS.
- Live/provider/paid/production actions: none; `PAID_API_FALLBACK=DISABLED`; `PRODUCTION=NO_GO`.

### R1B — content-aware read deduplication

- Successful `FILE/read_file` results now persist a bounded, redacted, worker-observed content hash, normalized path, source generation, character count, and deterministic read identity in the checksummed coordinator checkpoint and host receipt.
- Workspace source generation binds project id, canonical source SHA, and canonical execution base SHA.
- The planning kernel reuses an excerpt and rejects a read only when path, current byte hash, and source generation match the durable worker observation.
- Changed bytes or source generation create a new legal read identity; legacy receipts without hash/generation are `LEGACY_UNBOUND` and permit one fresh upgrade read.
- Generic lifetime task signatures no longer include `FILE/read_file`; confinement and sensitive-path exclusions remain unchanged.
- Focused coordinator/host/planning suite: `71 passed in 30.45s` using `python -m pytest` so the worktree extension package is authoritative.
- Python compileall: PASS. `git diff --check`: PASS.
- Live/provider/paid/production actions: none; `PAID_API_FALLBACK=DISABLED`; `PRODUCTION=NO_GO`.

### R1C — deterministic recovery churn guard

- Added deterministic recovery fingerprints over batch, completed-count baseline, canonical failure family, objective id, and workspace source generation.
- Command state persists fingerprint fields, same-fingerprint count, strategy generation, last exit code, last recovery time, and recovery disposition.
- First identical recovery resumes normally; the second increments strategy generation and bypasses recovered objective/completion/repair artifacts; the third enters durable same-lineage `HUMAN_REQUIRED` with `RECOVERY_CHURN_GUARD` and no respawn.
- The same bound applies to in-process continuous planner-validation exhaustion; completed batch history is preserved and no replacement command/lineage is created.
- Focused churn/planning/worker suite: `66 passed in 37.37s`.
- Pause-safe/provider-wait/recovery regression: `20 passed in 10.84s`.
- Python compileall: PASS. `git diff --check`: PASS.
- Live/provider/paid/production actions: none; `PAID_API_FALLBACK=DISABLED`; `PRODUCTION=NO_GO`.

### R2 — QuotaGovernor

- Added a durable `QuotaGovernor` keyed by provider, model, quota scope, and task class with `UNKNOWN`, `AVAILABLE`, `CONSTRAINED`, and `EXHAUSTED` states.
- Provider metadata outranks runtime/documentation/adaptive evidence per field; weaker observations cannot shorten an active stronger deadline.
- Quota-blocked resources are skipped without provider invocation; quota does not mutate health and health failures do not fabricate quota state.
- Governor snapshots are atomic, versioned, lock-protected, restart-stable, and fail closed on corruption while leaving project state intact.
- Waiting state persists the exact quota deadline/key; compatible health wakes only when the corresponding quota decision is eligible.
- Existing four-part paid-provider policy remains fail closed and unchanged. Paid fallback remains disabled.
- Concurrent runtime atomic JSON writes now use unique temp files, closing a deterministic create/probe race found by focused testing.
- Focused quota/provider/runtime suite: `68 passed in 10.85s`; isolated concurrency regression: PASS.
- Python compileall: PASS. `git diff --check`: PASS.
- Live/provider/paid/production actions: none; `PAID_API_FALLBACK=DISABLED`; `PRODUCTION=NO_GO`.

### R3 — ResourceLedger

- Added an append-only, fsynced, lock-protected, hash-chained JSONL `ResourceLedger` with typed event kinds, deterministic event IDs, bounded safe identifiers, and atomic derived snapshots.
- Idempotency keys prevent duplicate attempt/usage accounting. Reusing a key with different type or normalized payload fails closed instead of silently changing history.
- Replay accepts only a valid hash-linked sequence. An incomplete final line is ignored and repaired from the valid prefix on the next append; any earlier corruption makes the affected resource ledger and quota state unavailable without mutating project state.
- The ledger allowlists typed identifiers, numeric usage/cost fields, quota decisions, rate-limit observations, and recovery fingerprints. Prompts, generated content, raw headers/bodies, arbitrary messages, credentials, and unknown usage fields are discarded.
- `QuotaGovernor` records typed rate observations in the ledger and reconstructs its mutable snapshot when corrupt or inconsistent with the latest rate event.
- Provider reasoning attempts record starts, finishes, normalized usage, and quota decisions. Runtime recovery records normal resume, strategy escalation, and churn-guard dispositions; runtime status exposes only the bounded derived summary.
- Planning reasoning request IDs are content-sensitive and deterministic, so an identical restarted request reuses accounting identity while a materially changed prompt/schema/source generation does not.
- Focused R3/resource/provider/runtime/planning suite: `120 passed in 48.14s`.
- Python compile checks: PASS. `git diff --check`: PASS.
- Live/provider/paid/production actions: none; protected lineages/workspaces untouched; `PAID_API_FALLBACK=DISABLED`; `PRODUCTION=NO_GO`.

### Jev preflight availability checkpoint

- Fresh origin fetch found `audit/aos-jev-decision-layer-preflight-20260923-01` at exact SHA `d28a1350ecf270b94eb3f48014201cc53ab9b517`.
- Durable Jev state reports `STATUS=COMPLETE`, `READY_FOR_INTEGRATION=YES`, `SOURCE_MUTATION_COUNT=0`, `PAID_CALLS_MADE=0`, and `PRODUCTION=NO_GO`.
- The four authoritative Jev documents remain scheduled for complete reading immediately before the optional Jev phase. No Jev call or activation occurred in R3.

### Shared agentic contract and workspace fingerprint

- Added a shared `AGENTIC_EXECUTION_BACKEND` contract with `SUBSCRIPTION_INCLUDED` cost, long-horizon/test capabilities, structured availability states, typed session identity, and typed request/result handoff fields while preserving existing backend compatibility.
- Extended the durable run registry with resource/backend/session identity, source and workspace bindings, checkpoint/turn metadata, completed work-unit IDs/signatures, artifact hashes, and immutable superseded-session history. Journal replay restores every field; completed work IDs/signatures are rejected on re-entry.
- Added strict Draft 2020-12 schemas for agentic checkpoints and execution-resource policy. Both enforce `PRODUCTION=NO_GO`/paid fallback disabled at the policy boundary and are registered with the deterministic validator.
- Added content-sensitive Git workspace fingerprints over repository/object format, resolved root, `HEAD`, canonical source SHA, exact NUL-delimited index/status records, tracked and non-ignored untracked raw bytes, tombstones, file type/mode/size, symlink targets, and recursive initialized-submodule fingerprints.
- Fingerprint tests prove clean determinism and changes for source binding, staged/unstaged/untracked/binary/deleted/renamed/executable/symlink/submodule states; ignored cache content remains excluded.
- Source-checkout extension bootstrap now prefers this worktree's `src` tree, preventing detached restart tests from importing an unrelated installed AOS package.
- Focused contract/schema/fingerprint/registry/coordinator restart suite: `109 passed in 29.48s`; focused host/planning/runtime/router/supervisor regression: `87 passed in 34.80s`.
- Python compile/schema parse checks: PASS. `git diff --check`: PASS.
- No external agent was invoked, no live runtime was changed, protected lineages/workspaces remain untouched, paid fallback remains disabled, and production remains `NO_GO`.

### Antigravity first-class agentic backend

- Added `AntigravityAgenticExecutionBackend` as a `SUBSCRIPTION_INCLUDED`, `RESTRICTED_WORKSPACE`, availability-gated execution resource. It reuses the existing executable identity, machine-local capability attestation, CLI adapter, stream terminal contract, and capability probe infrastructure.
- The accepted `AntigravityWorkerAdapter` and probe were not rebuilt. Real execution remains prohibited unless the existing machine-local proof matches executable hash/version, adapter contract, and runtime environment; test doubles require explicit injection.
- New and resumed turns bind exact conversation identity to canonical source SHA, content-sensitive workspace fingerprint, adapter/executable identity, subscription auth class, AOS checkpoint, and completed-work records. Changed workspaces return typed `STALE_AGENT_SESSION` without invoking the old conversation.
- Successful turns verify post-run changed paths against AOS write scope, stream-hash artifacts, persist only sanitized terminal/usage evidence, and advance completed work only after terminal success. Raw responses, reasoning, tool parameters, prompts, and arbitrary errors are not persisted.
- Quota loss maps to structured non-terminal `QUOTA_EXHAUSTED`; the router preserves the degraded result for reroute/wait rather than converting resource loss into project failure. Supervisor recovery archives stale sessions as `SUPERSEDED_STALE_WORKSPACE`.
- The real CLI adapter now owns the full descendant process tree and exposes bounded interrupt; malformed/multiple/missing terminal events and nonzero exits fail closed.
- Antigravity is registered in the autonomous execution router, never in planner-provider factories. The legacy runtime activation flag remains closed; registration is not production activation.
- Execution-resource policy declares Antigravity enabled only as an attested subscription resource with API-key fallback false and production false.
- Focused first-class backend/router/supervisor/schema/host/coordinator suite: `134 passed in 24.78s`; existing Antigravity capability/probe suite: `78 passed in 73.23s`.
- Python compile/schema parse checks: PASS. `git diff --check`: PASS.
- No Antigravity live invocation or probe was performed in this phase; protected lineages/workspaces remain untouched; `PAID_API_FALLBACK=DISABLED`; `PRODUCTION=NO_GO`.

### Codex CLI ChatGPT-subscription agentic backend

- Added `CodexCliExecutionBackend` as a separately registered `SUBSCRIPTION_INCLUDED`, `RESTRICTED_WORKSPACE` agentic resource. It is not an inference-provider factory and has no OpenAI API-key fallback.
- Launch construction fixes approval and workspace sandbox flags before `exec`, supplies bounded prompt input only through stdin `-`, uses `--ignore-user-config --json`, and resumes only an exact validated UUID. `--last` and sandbox-bypass options are prohibited.
- Success requires exit zero, exactly one valid `thread.started`, and exactly one `turn.completed`. Unknown/malformed/multiple/missing terminal streams, `turn.failed`, nonzero exit, timeout, or mismatched resume UUID fail closed with typed sanitized evidence.
- Machine-local capability proof binds the executable path/hash/version, adapter contract, redacted `doctor --json` ChatGPT-token evidence, absence of stored API-key auth, and optional structured app-server quota evidence. Child processes strip OpenAI/Codex API credentials and access-token overrides.
- Added a bounded owned app-server `account/rateLimits/read` client. Quota observations map to `AVAILABLE`, `LOW_OR_SCARCE`, `QUOTA_EXHAUSTED`, or `UNKNOWN` with reset timing; unavailable/unknown capability never invokes the CLI.
- Start/resume binds exact thread identity to source SHA, content-sensitive workspace fingerprint, executable identity, ChatGPT auth class, AOS checkpoint, and completed-work ledger. Workspace drift and completed-work replay reject before invocation.
- Post-turn scope and artifact verification run before checkpoint advancement. Raw prompts, agent messages, transcripts, reasoning, tool parameters, stderr, doctor details, and credentials are not persisted.
- The zero-cost execution-resource policy declares Codex as subscription-included, API-key fallback disabled, production false, and exact UUID/fingerprint resume required.
- Focused Codex backend/capability suite: `11 passed in 13.85s`; shared agentic/router/schema/fingerprint regression: `118 passed in 78.59s`.
- Execution-resource policy schema validation and `git diff --check`: PASS.
- A read-only redacted `codex doctor --json` diagnostic verified parser compatibility with the installed CLI; no Codex model turn, quota-consuming execution, paid API call, deployment, or production action occurred. Protected lineages/workspaces remain untouched; `PAID_API_FALLBACK=DISABLED`; `PRODUCTION=NO_GO`.

### Qwen3-4B Q4_K_M bounded local reasoning

- Added `LlamaCppQwenReasoningBackend` as a `FREE_LOCAL`, loopback-only, `MODEL_REASONING` resource ahead of cloud reasoning. It is deliberately limited to classification, triage, and schema-bounded decisions and has no file, process, agentic, production, or authorization capability.
- The fixed CPU profile caps context at 4096 tokens, output at 512 tokens, threads at 8, batch/ubatch at 128, parallelism at one, request timeout at 120 seconds, and response bytes at 2 MiB. The server argv binds only `127.0.0.1` and rejects non-Qwen3-4B/Q4_K_M GGUF artifacts.
- Capability proof binds the llama.cpp executable hash/version, exact model hash/size/quantization, adapter/profile version, and a successful structured classification benchmark under 12 GiB peak RSS and 120-second latency. Missing or drifted proof fails closed.
- Runtime completion is deterministic (`temperature=0`, fixed seed), requires exactly one choice and JSON Schema validation, and persists only usage plus an output hash. Prompt and generated decision content remain transient and are excluded from serialized `ExecutionResult` evidence.
- The execution-resource policy declares the exact model/quantization/runtime and bounded CPU/memory profile with paid fallback and production both disabled.
- Current-machine discovery found neither `llama-server` nor a configured/local Qwen3-4B Q4_K_M artifact. No download or benchmark was fabricated; the resource therefore remains `UNPROVEN` and unavailable while scheduler fallback remains intact.
- Focused Qwen adapter/capability suite: `14 passed in 1.48s`; focused router/host/schema regression: `118 passed in 6.00s`.
- A broader native-worker run produced `125 passed, 1 failed`; the isolated failure is the unchanged browser test and reports `Playwright is not installed or accessible in current Python environment`. It is unrelated to Qwen and the complete affected routing/host/schema set passed.
- Execution-resource policy validation, Python compile checks, and `git diff --check`: PASS. No model inference, paid call, download, deployment, protected-lineage mutation, or production action occurred.
