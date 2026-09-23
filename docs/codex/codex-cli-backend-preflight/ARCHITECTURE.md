# Codex CLI execution backend architecture

Track: `AOS-CODEX-CLI-EXECUTION-BACKEND-PREFLIGHT-20260923-01`

Status: architecture preflight complete; implementation is intentionally absent; production remains `NO_GO`.

## Decision

Implement Codex CLI as a first-class agentic execution resource, not as a `PlannerProvider` or an OpenAI API inference provider.

The proposed backend is:

- class: `CodexCliExecutionBackend`
- backend class: `AGENTIC_EXECUTION_BACKEND`
- backend id: `codex_cli`
- cost class: `SUBSCRIPTION_INCLUDED`
- trust zone: `RESTRICTED_WORKSPACE` by default
- paid OpenAI API fallback: hard-disabled
- proven capabilities on the tested installation: repository read/write, shell execution, test execution, Git inspection, multi-turn session identity, exact-id resume, and structured quota observation

The backend must use the locally authenticated Codex CLI and must reject API-key-backed operation. It must never translate a Codex quota outage into project failure or silently switch to paid OpenAI API use.

## Existing AOS mechanisms to reuse

| Existing component | Reuse | Required extension or caveat |
|---|---|---|
| `src/aos/workers/antigravity.py` | executable discovery, executable hash/version pinning, capability attestation, sanitized environment, fail-closed JSON stream parsing, bounded summaries | Generalize the patterns; do not reuse the Antigravity wire schema or prompt restrictions verbatim. |
| `src/aos/workers/antigravity_probe.py` | disposable Git probe, outside-workspace sentinel, exact byte verification, Git invariant checks, atomic attestation | Add a Codex-specific probe. Keep probe artifacts machine-local and never persist credentials. |
| `extensions/autonomy-fabric/antigravity_adapter.py` | explicit conversation identity, terminal-event requirement, workspace existence check, deterministic fake | Codex uses `thread_id` and `turn.*` events. Do not persist raw response/transcript fields. |
| `extensions/autonomy-fabric/run_registry.py` | `RunIdentity`, state machine, append-only JSONL journal, fsync, replay/restart recovery | Add typed durable backend/session/checkpoint fields and replay them. |
| `extensions/autonomy-fabric/supervisor.py` | bounded concurrency, workspace and branch collision locks, leases, stale-run interruption, journal recovery | The requested `parallel_supervisor.py` does not exist at the base SHA. `supervisor.py` is the actual ParallelSupervisor implementation. Add a fingerprint gate before agent-session resume. |
| `src/aos/autonomous_host.py` | execution-router construction, provider-attempt journal, bounded redacted errors, atomic JSON | Register Codex as an execution backend, not in `_PROVIDER_FACTORIES`. Preserve `production=NO_GO`. |
| `src/aos/runtime_worker.py` | structured runtime IPC, workspace lock, restart recovery, checkpoint watcher, `WAITING_FOR_REASONING_PROVIDER`, preserved lineage | Add a Codex-specific waiting/re-entry disposition without marking the project failed. |
| `src/aos/provider_registry.py` | deterministic free-first policy and explicit paid gating | Codex belongs in an execution-resource registry/policy. Do not model subscription capacity as a normal inference provider credential. |
| `src/aos/provider_circuit.py` | durable state, adaptive backoff, half-open probes, quota/capacity-aware retry, aggregation | Store Codex availability separately from health and use service-provided reset timestamps when available. |
| `src/aos/process_utils.py` | `popen_headless`, owned descendant tree, timeouts, cancellation, hidden Windows processes | Reuse unchanged unless a small line-stream callback wrapper proves necessary. |
| `src/aos/planning_kernel.py` | durable batch reconstruction, completed-task IDs and signatures, fresh bounded read context, exact checkpoint re-entry | Extend ContextPack assembly; retain its completed-work ledger as authoritative. |
| `extensions/autonomy-fabric/persistent_coordinator.py` | checksum-bound checkpoints and completed-node recovery | Persist agentic session metadata and do not redispatch completed nodes. |
| `extensions/autonomy-fabric/execution_router.py` | capability routing and degraded-backend failover | Add subscription cost ordering and structured availability handling. |

The legacy `AntigravityExecutionBackend` in `native_workers.py` is not a sufficient template by itself: it exposes only the synthetic `ANTIGRAVITY` capability, stores a raw response prefix, and conflates health/quota. The stronger worker/probe/journal patterns above are the accepted infrastructure to reuse.

## Proposed contracts

### Backend and availability

Keep `ExecutionBackend.execute(ExecutionRequest) -> ExecutionResult` as the router-compatible entry point and add an agentic specialization:

```python
class AgenticExecutionBackend(ExecutionBackend):
    backend_class = BackendClass.AGENTIC_EXECUTION_BACKEND

    def get_availability(self) -> ExecutionAvailabilitySnapshot: ...
    def start(self, request, context_pack) -> ExecutionResult: ...
    def resume(self, request, identity, context_pack) -> ExecutionResult: ...
    def interrupt(self, execution_id: str) -> None: ...

class CodexCliExecutionBackend(AgenticExecutionBackend):
    backend_id = "codex_cli"
    resource_id = "local_codex_cli_chatgpt_subscription"
    cost = ExecutionCost.SUBSCRIPTION_INCLUDED
```

`execute()` selects `start()` or `resume()` only after validating the durable identity. The router does not call `codex exec resume --last`; it always supplies an exact UUID.

Reuse existing capability names where they are semantically exact:

- `FILE_READ`
- `FILE_WRITE`
- `PROCESS_EXEC`
- `GIT_READ`

Add only the distinctions routing actually needs:

- `TEST_EXECUTION`
- `LONG_HORIZON_AGENTIC_WORK`

Do not introduce duplicate `REPO_READ`, `REPO_WRITE`, `SHELL_EXECUTION`, or `GIT_INSPECTION` enum members merely to rename existing capabilities.

### Durable execution identity

The following fields are mandatory and journaled after each accepted state change:

| Field | Meaning |
|---|---|
| `resource_id` | Stable resource identity, initially `local_codex_cli_chatgpt_subscription`. |
| `backend_id` | `codex_cli`. |
| `session_or_thread_id` | Exact Codex UUID from `thread.started`; never a `--last` selector. |
| `workspace_fingerprint` | Versioned content fingerprint described below. |
| `source_sha` | Canonical source/control SHA bound to the objective. |
| `checkpoint_id` | AOS-owned immutable checkpoint identifier. |
| `last_successful_turn` | Monotonic AOS turn ordinal advanced only by `turn.completed`. |
| `last_successful_artifact` | Last verified artifact id/hash record; nullable until one exists. |
| `started_at` | UTC creation timestamp. |
| `updated_at` | UTC timestamp of the last journaled mutation. |

Recommended compatibility metadata is also persisted: adapter contract version, Codex CLI version and executable SHA-256, auth mode, objective id, last terminal event, completed work-unit ids/signatures, artifact hashes, and superseded session ids.

### Availability is not health

`ExecutionAvailabilitySnapshot` contains at least `state`, `observed_at`, `retry_after_epoch`, `source`, and sanitized evidence. Its states are:

| State | Classification rule |
|---|---|
| `AVAILABLE` | Executable/auth/contract healthy and the Codex rate-limit bucket is below the configurable scarcity threshold. |
| `LOW_OR_SCARCE` | Structured rate-limit data shows high usage or a near-term scarcity policy threshold, but execution remains possible. |
| `QUOTA_EXHAUSTED` | `rateLimitReachedType` is non-null, a relevant window is exhausted, or a structured execution error explicitly reports quota exhaustion. |
| `TEMPORARILY_UNAVAILABLE` | Network, service capacity, transient app-server, or bounded timeout failure. |
| `AUTH_UNAVAILABLE` | No usable ChatGPT auth, wrong auth mode, expired login, or workspace access denied. |
| `CONTRACT_FAILURE` | Unsupported/changed JSON schema, missing terminal event after a normal exit, invalid executable attestation, or other fail-closed adapter contract violation. |
| `UNKNOWN` | No fresh observation or ambiguous evidence. |

Health describes the local binary/process/contract. Quota describes subscription capacity. A healthy binary may be `QUOTA_EXHAUSTED`; a broken binary has `CONTRACT_FAILURE` or unavailable health even if prior quota was abundant.

## Invocation and parsing contract

1. Resolve and pin the executable path, binary SHA-256, and `codex --version` using the attestation pattern.
2. Run `codex doctor --json` and require structured evidence of ChatGPT auth. Reject stored API-key mode.
3. Remove `OPENAI_API_KEY`, `CODEX_API_KEY`, and other unrelated provider secrets from the child environment. Do not create, display, or configure keys.
4. Read quota using the stable app-server JSONL handshake and `account/rateLimits/read`.
5. Compute and persist the workspace fingerprint immediately before launch.
6. Launch with an argv array and piped stdin, never a shell command string. Use explicit workspace, approval, and sandbox settings. Never use bypass flags.
7. Consume stdout strictly as JSONL. Persist only allowlisted, redacted fields from `thread.started`, `turn.started`, `item.*`, `turn.completed`, `turn.failed`, and `error`.
8. Treat success as process exit `0` plus a valid `turn.completed`. `thread.started` alone is not a checkpoint.
9. Verify the post-run workspace against allowed scope and record changed paths/artifact hashes before completing the AOS work unit.

The prompt is sent through stdin (`-`) so an automation host that itself has piped stdin cannot accidentally append unrelated input. Top-level flags precede `exec`, matching the installed CLI parser.

## Workspace fingerprint

Use a versioned SHA-256 over canonical, length-delimited binary records. The input is:

1. repository format/version and resolved repository root;
2. `HEAD` and canonical `source_sha`;
3. the exact NUL-delimited output of `git ls-files --stage -z` (index modes, stages, paths, and staged blob ids);
4. the exact NUL-delimited output of `git status --porcelain=v2 -z --untracked-files=all`;
5. for every tracked worktree path and every untracked, non-ignored path from `git ls-files -z` and `git ls-files --others --exclude-standard -z`: normalized path bytes, entry type, executable/mode bits, size, and streaming SHA-256 of raw bytes; symlinks hash their link target bytes and missing tracked paths get an explicit tombstone record;
6. recursively computed fingerprints for initialized submodules plus the parent gitlink entry.

Sort by normalized repository-relative byte path before hashing. Never decode file contents, so binary changes are first-class. Never use timestamps as content identity. This detects staged, unstaged, untracked, deleted, renamed, symlink, executable-bit, binary, and initialized-submodule changes. Ignored runtime/cache files remain outside the identity unless a project policy explicitly includes them.

## Checkpoint, failover, and no-duplicate-work rules

The authoritative checkpoint is AOS-owned. Codex local session storage is useful continuation state, not the project source of truth.

On every `turn.completed`:

1. verify allowed mutations and artifacts;
2. append the completed work-unit id/signature and artifact hashes to the run journal;
3. atomically persist the ContextPack and checkpoint;
4. then advance `last_successful_turn` and `last_successful_artifact`.

If quota disappears or Codex becomes transiently unavailable:

1. interrupt/terminate the owned process tree;
2. preserve the exact session id and last completed-turn metadata;
3. persist a checkpoint with a non-terminal waiting/reroute disposition;
4. reroute the same objective and remaining work through the execution router;
5. never mark the project failed merely because Codex is unavailable.

Completed work is not inferred from conversational prose. The registry, coordinator checkpoint, planning-kernel completed IDs/signatures, and verified artifact hashes are authoritative. A fallback ContextPack contains completed-work records plus only the remaining objective. Before dispatch, the coordinator rejects any work unit whose id/signature is already complete.

## Exact resume and safe re-entry

Exact resume is allowed only when all of these hold:

- `session_or_thread_id` is a valid exact UUID and the stored session is loadable;
- current workspace fingerprint exactly equals the saved fingerprint;
- current canonical `source_sha` equals the saved value;
- backend id, adapter contract, executable identity/version compatibility, and auth mode are accepted;
- the journal and checkpoint agree on `last_successful_turn` and completed artifacts;
- the objective is not already terminal.

Then invoke `codex exec resume <UUID>` with the follow-up ContextPack. `--last` is forbidden.

If the workspace/source changed, including changes made by a fallback backend:

1. retain the old UUID in immutable journal history and mark it `SUPERSEDED_STALE_WORKSPACE`;
2. do not resume it;
3. recompute the fingerprint;
4. create a fresh Codex thread from the current durable ContextPack/checkpoint;
5. include completed-work records so nothing is repeated.

An interrupted thread that emitted only `thread.started` may have an empty, unreadable rollout. Such a thread is not resumable evidence; start fresh from the AOS checkpoint.

## Security boundaries

- Workspace and allowed write scope remain AOS-enforced before and after execution.
- Codex may run only in the explicitly bound disposable/managed workspace.
- Child environment is allowlisted/scrubbed; no API-key fallback variables are passed.
- Raw prompts, reasoning, shell output, tool parameters, absolute home paths, auth records, or transcripts are not copied to durable AOS evidence.
- Error text passes through `redact_secrets` and bounded allowlists.
- Process ownership uses `popen_headless`/`OwnedProcess` so cancellation tears down descendants.
- Production/destructive/payment operations remain human-gated by the authority router.

## Production gate

This design authorizes no production use. Implementation begins only from the independently accepted final FreeLLMAPI Track A SHA, followed by unit, failure-injection, restart, and disposable live proofs. `PRODUCTION=NO_GO` remains invariant.
