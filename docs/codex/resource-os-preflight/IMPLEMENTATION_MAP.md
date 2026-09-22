# Resource OS Implementation Map

**Audit**: `audit/aos-resource-os-preflight-20260922-01`
**Base SHA**: `a0cfc2e329b36dca1964ed427ab17d8c82cbacb6`
**Auditor**: Track C (Claude)
**Date**: 2026-09-22

---

## 1. Executive Finding

AOS already possesses the majority of architectural surfaces needed for a Resource OS.
The core provider failover loop (`ProviderFailoverReasoningBackend`), durable circuit breaker registry
(`ProviderCircuitBreakerRegistry`), deterministic provider routing (`ProviderRouter`), execution
backend abstraction (`ExecutionBackend` ABC), execution router with failover (`ExecutionRouter`),
Antigravity worker adapter with full headless CLI contract (`AntigravityWorkerAdapter`), AG capability
probe (`run_antigravity_probe`), autonomy-fabric AG adapter with conversation ID tracking
(`AntigravityCLIAdapter`), and run registry with event journal replay (`AgentRunRegistry`) are
all production-quality and proven by tests.

**What is missing is not components but connections**: resource state aggregation across backends,
provider rate-limit metadata propagation, AG session lifecycle awareness in the host, and a
capability-aware routing layer that considers scarcity/quota/session state rather than just
static priority lists.

The FreeLLMAPI track (`feature/aos-freellmapi-local-meta-provider-20260922-01`) touches a
**very wide** file set. Resource OS implementation MUST wait for its acceptance and merge.

---

## 2. Existing Reusable Components

| Component | File | Key Class/Function | Status |
|---|---|---|---|
| Provider failover loop | `src/aos/autonomous_host.py` | `ProviderFailoverReasoningBackend` | PROVEN, live |
| Provider attempt journal | `src/aos/autonomous_host.py` | `_append_jsonl`, `ProviderAttempt` | PROVEN |
| Provider circuit breaker | `src/aos/provider_circuit.py` | `ProviderCircuitBreakerRegistry` | PROVEN, durable |
| Adaptive backoff | `src/aos/provider_circuit.py` | `calculate_backoff()` | PROVEN |
| Circuit aggregation | `src/aos/provider_circuit.py` | `aggregate_registries()` | PROVEN |
| Provider registry/routing | `src/aos/provider_registry.py` | `ProviderRegistry`, `ProviderRouter` | PROVEN |
| Provider probe matrix | `src/aos/provider_probe.py` | `provider_runtime_matrix()` | PROVEN |
| Execution backend ABC | `extensions/autonomy-fabric/execution_backend.py` | `ExecutionBackend` | PROVEN |
| Execution router | `extensions/autonomy-fabric/execution_router.py` | `ExecutionRouter` | PROVEN |
| AG worker adapter | `src/aos/workers/antigravity.py` | `AntigravityWorkerAdapter` | PROVEN |
| AG capability probe | `src/aos/workers/antigravity_probe.py` | `run_antigravity_probe()` | PROVEN |
| AG CLI adapter | `extensions/autonomy-fabric/antigravity_adapter.py` | `AntigravityCLIAdapter` | PROVEN |
| Run registry + journal | `extensions/autonomy-fabric/run_registry.py` | `AgentRunRegistry`, `FileRunJournal` | PROVEN |
| Nemotron structured provider | `src/aos/providers/nemotron.py` | `NemotronPlannerProvider` | PROVEN |
| Ollama local probe | `src/aos/autonomous_host.py` | `probe_ollama_models()` | PROVEN |
| Bounded prompt excerpt | `src/aos/planning_kernel.py` | `_bounded_prompt_excerpt()` | PROVEN |
| Workspace lineage check | `src/aos/autonomous_host.py` | `assert_workspace_execution_lineage()` | PROVEN |
| Failure classification | `src/aos/provider_probe.py:115` | `_classify()` | PROVEN |
| Error classification in host | `src/aos/autonomous_host.py:290-326` | inline in `execute()` | PROVEN |

---

## 3. Exact Architectural Gaps

| Gap | Severity | Notes |
|---|---|---|
| **No ResourceLedger** | HIGH | No unified view of available capacity across reasoning providers, AG, Codex, local models |
| **Rate-limit metadata lost** | HIGH | HTTP 429 Retry-After and x-ratelimit-* headers are discarded at the provider adapter layer |
| **No AG backend in Host V1** | BY DESIGN | `build_execution_router()` L528: `# Antigravity is deliberately absent` |
| **No AG session/resume safety** | MEDIUM | `RunIdentity.agent_conversation_id` exists but no workspace-fingerprint-to-session binding |
| **No AgenticExecutionBackend** | MEDIUM | No unified interface for AG/Gemini CLI/Codex CLI as execution backends |
| **Static provider ordering** | MEDIUM | `ProviderRouter.select()` iterates `preferred_providers` list only, no dynamic scoring |
| **No ContextPack contract** | LOW | Prompt assembly inlined in `planning_kernel.py`, no structured pre-processing |
| **No Qwen/local model first-class** | LOW | Ollama adapter exists but no Qwen-specific optimization path |

---

## 4. ResourceLedger Insertion Map

### Safest Insertion Point

**New module**: `src/aos/resource_ledger.py`

### Existing Related State Owners

| What | Where | Currently |
|---|---|---|
| Provider circuit states | `ProviderCircuitBreakerRegistry._circuits` | Per-provider health + backoff |
| Provider attempt history | `provider-attempts.jsonl` | Append-only journal |
| AG capability status | `AntigravityWorkerAdapter.capability_status` | PROVEN/UNPROVEN |
| Ollama availability | `probe_ollama_models()` result | Ephemeral dict |
| Execution backend health | `ExecutionBackend.get_health()` | Per-backend query |
| Routing policy eligibility | `ProviderRouter.select()` | Static checks |

### Recommended Structure

`ResourceLedger` should:
- **Read** from `ProviderCircuitBreakerRegistry` (already persisted, durable)
- **Read** from `ExecutionBackend.get_health()` on each backend
- **Read** from AG capability attestation store (`get_local_capability_store_path()`)
- **Own** aggregated capacity snapshot: `{provider_id → {available, quota_remaining, next_probe_at, cost_class}}`
- **Persist** to `runtime_dir / "resource-ledger.json"`

### Call Sites

1. `ProviderFailoverReasoningBackend.__init__()` — inject ledger
2. `ProviderFailoverReasoningBackend.execute()` — update ledger after each attempt
3. `ExecutionRouter.select_backend()` — query ledger for scoring
4. `build_execution_router()` in `autonomous_host.py` — construct ledger
5. `run_autonomous_project()` in `planning_kernel.py` — construct ledger

### What Remains Operational vs Canonical

- `ProviderCircuitBreakerRegistry` remains the **operational** source of circuit truth
- `ResourceLedger` is the **canonical** aggregated snapshot for routing decisions
- Do NOT duplicate circuit state into the ledger — reference it

### What Must NOT Be Duplicated

- Provider credential presence checks (already in `ProviderRouter.select()`)
- Individual circuit breaker persistence (already in `provider_circuit.py`)
- Provider attempt journal (already in `_append_jsonl`)

---

## 5. QuotaGovernor Insertion Map

### Current Flow

```
Provider error (e.g. openai.RateLimitError)
  → NemotronPlannerProvider.generate_plan() catches exception
  → Raises PlannerTransientError("Nemotron transient error: RATE_LIMIT")
  → ProviderFailoverReasoningBackend.execute() catches PlannerTransientError
  → String-matches "RATE_LIMIT" / "429" / "QUOTA" in error message
  → Sets ProviderAttemptStatus.RETRYABLE_FAILED or QUOTA_EXHAUSTED
  → Sets failure_class = "RATE_LIMITED" or "QUOTA_EXHAUSTED"
  → Records ProviderAttempt (provider_id, status, error_class)
  → Calls circuit_registry.record_failure(provider_id, failure_class)
  → ProviderCircuitBreakerRegistry.calculate_backoff() reads failure_class string
  → Quota/rate-limit failures get 900s or 1800s backoff (hardcoded)
  → Circuit state → OPEN, next_probe_at set
  → Loop continues to next provider in preferred_providers list
  → On next invocation: is_provider_available() checks next_probe_at
```

### Where Rate-Limit Metadata Is Lost

**Nemotron (`src/aos/providers/nemotron.py` L155-173)**:

```python
except Exception as e:
    if isinstance(e, openai.RateLimitError):
        raise PlannerTransientError("Nemotron transient error: RATE_LIMIT") from e
```

The `openai.RateLimitError` object contains:
- `e.response.headers["Retry-After"]` — server-specified wait time
- `e.response.headers["x-ratelimit-remaining-requests"]`
- `e.response.headers["x-ratelimit-remaining-tokens"]`
- `e.response.headers["x-ratelimit-reset-requests"]`
- `e.response.headers["x-ratelimit-reset-tokens"]`

**ALL of this is discarded** when wrapping into `PlannerTransientError("RATE_LIMIT")`.
The string message is the ONLY information that survives.

The same pattern applies to all other providers using the OpenAI SDK:
- `groq.py` — same loss
- `openai_compatible.py` — same loss
- `gemini.py` — uses google SDK, different headers but same loss pattern

### Preservation Path

**Option A (Minimal)**: Add `retry_after_seconds` and `quota_metadata` fields to `PlannerTransientError`:

```
PlannerTransientError should gain:
  retry_after_seconds: Optional[float]
  rate_limit_metadata: Optional[Dict[str, Any]]
```

Then in each provider catch block, extract headers before wrapping.

**Option B (Slightly larger)**: Create `ProviderQuotaSnapshot` dataclass returned alongside the error.

### QuotaGovernor Location

**New module**: `src/aos/quota_governor.py`

Responsibilities:
- Receive normalized rate-limit metadata from provider adapters
- Calculate optimal backoff (using server Retry-After when available instead of hardcoded tiers)
- Track remaining-requests/remaining-tokens per provider
- Provide `should_attempt(provider_id) → bool` based on actual quota state

### Integration Point

Replace the hardcoded `calculate_backoff()` in `ProviderCircuitBreakerRegistry` with a query to `QuotaGovernor` when rate-limit metadata is available, falling back to the existing adaptive tiers when metadata is absent.

---

## 6. Nemotron 429 Metadata-Loss Map

**File**: `src/aos/providers/nemotron.py`

| Line | What Happens | Information Lost |
|---|---|---|
| L136 | `client.chat.completions.create()` called | — |
| L155 | `except Exception as e:` catches all | — |
| L159 | `isinstance(e, openai.RateLimitError)` | Access to `e.response` is available here |
| L160 | `raise PlannerTransientError("RATE_LIMIT")` | **`e.response.headers` discarded** |
| — | PlannerTransientError propagates to autonomous_host.py L300 | Only string "RATE_LIMIT" survives |
| L305 | String match `"429" in raw` | Determines `failure_class = "RATE_LIMITED"` |
| — | `circuit_registry.record_failure(provider_id, "RATE_LIMITED")` | No Retry-After, no remaining quota |
| — | `calculate_backoff("RATE_LIMITED", N)` | Uses hardcoded 900s/1800s instead of server hint |

**Exact fix surface**: `nemotron.py` L159-160. Extract `Retry-After` from `e.response.headers` before wrapping. Same pattern needed in `groq.py`, `openai_compatible.py`, `gemini.py`.

---

## 7. Antigravity Rebind Map

### ALREADY EXISTS

| Capability | File | Evidence |
|---|---|---|
| CLI discovery | `antigravity.py:resolve_executable_identity()` | Finds agy binary, computes SHA-256 |
| Headless execution | `antigravity.py:build_antigravity_argv()` | `--mode=accept-edits` headless invocation |
| Machine-readable output | `antigravity.py:parse_antigravity_stream_output()` | Full NDJSON stream parser |
| Conversation/session ID | `antigravity_adapter.py:AntigravityResponse.conversation_id` | Extracted from init/result events |
| Exact conversation continuation | `antigravity_adapter.py:build_cmd()` | `--conversation` and `--continue` flags |
| Run journals | `run_registry.py:FileRunJournal` | Durable JSONL event journal |
| Workspace identity | `antigravity.py:resolve_runtime_environment_profile()` | Config fingerprinting |
| Workspace fingerprint | `antigravity.py:compute_runtime_environment_fingerprint()` | SHA-256 of profile |
| Timeouts | `antigravity.py:MAX_WORKER_TIMEOUT_SECONDS=600` | Bounded subprocess timeout |
| Failure normalization | `antigravity.py:sanitize_tool_error()` | Controlled error taxonomy |
| Capability attestation | `antigravity_probe.py:write_local_capability_attestation()` | Durable PROVEN/UNPROVEN store |
| Identity revalidation | `antigravity.py:AntigravityWorkerAdapter.revalidate_runtime_identity()` | Runtime identity verification |
| ExecutionCapability.ANTIGRAVITY | `execution_backend.py:L29` | Enum value exists |
| AG status mapping | `antigravity_adapter.py:ANTIGRAVITY_TO_AOS_STATUS_MAP` | AG → AOS run status |
| AG cost weight in router | `execution_router.py:L102` | AG penalized when not required |

### NEEDS REBINDING

| What | Current State | Required |
|---|---|---|
| AG as ExecutionBackend | Not implemented | New class implementing `ExecutionBackend` ABC wrapping `AntigravityWorkerAdapter` |
| AG registration in host | `build_execution_router()` explicitly excludes AG (L528) | Add AG backend to backends list with `ag_required` flag |
| AG quota tracking | Not tracked | `ResourceLedger` must include AG capacity (subscription-included) |
| AG conversation state in host | `RunIdentity.agent_conversation_id` field exists but host never sets it | Wire conversation_id from `AntigravityResponse` to `RunIdentity` |
| AG health reporting | `ExecutionBackend.get_health()` not implemented for AG | Probe capability status and return health enum |

### ACTUAL MISSING CAPABILITY

| What | Notes |
|---|---|
| AG quota sensing | No way to know remaining AG capacity before invocation |
| AG session workspace binding | No mechanism to bind workspace fingerprint → conversation_id for safe resume |
| AG result → canonical checkpoint | No automatic commit/checkpoint after AG produces workspace mutations |
| AG conversation history preservation | No mechanism to archive stale conversations before starting fresh |

### Minimum Safe Change Surface

1. **New file**: `extensions/autonomy-fabric/antigravity_execution_backend.py` — implements `ExecutionBackend` wrapping `AntigravityWorkerAdapter`
2. **Modify**: `src/aos/autonomous_host.py:build_execution_router()` — conditionally register AG backend
3. **Modify**: `extensions/autonomy-fabric/execution_router.py` — no change needed, AG weight logic already present
4. DO NOT recreate the adapter from scratch — use existing `AntigravityWorkerAdapter.execute()` as the implementation body

---

## 8. AG Session/Resume Safety Map

### Required Rule

```
IF workspace_fingerprint unchanged since last AG session:
    exact conversation resume MAY be allowed
IF fallback execution changed workspace:
    preserve prior conversation as history
    create fresh AG session from latest canonical checkpoint
```

### Existing Fields

| Field | Where | Status |
|---|---|---|
| `agent_conversation_id` | `RunIdentity` in `run_registry.py:L107` | EXISTS, stored but never set by host |
| `workspace_path` | `RunIdentity` in `run_registry.py:L108` | EXISTS |
| `base_sha` | `RunIdentity` in `run_registry.py:L110` | EXISTS |
| `runtime_environment_fingerprint_sha256` | `antigravity_probe.py` attestation | EXISTS in attestation store |
| `parent_run_id` | `RunIdentity` in `run_registry.py:L112` | EXISTS, supports history chain |

### Missing Fields/Checks

1. **`workspace_fingerprint_at_session_start`**: Not captured. Need to compute `git rev-parse HEAD` + `git diff --stat` hash at AG session start and store in `RunIdentity` metadata or a parallel session record.

2. **`workspace_fingerprint_at_resume`**: Need to re-compute and compare before allowing `--continue` on same conversation.

3. **`session_stale_check(run_id) → bool`**: Function that compares stored workspace fingerprint with current workspace state. Missing entirely.

4. **`archive_stale_conversation(run_id)`**: Function that transitions a run to a terminal state and preserves its conversation_id for history. The state machine supports `INTERRUPTED → QUEUED` already, but no archive logic exists.

5. **`create_fresh_from_stale(stale_run_id) → RunIdentity`**: Function that creates a new run with `parent_run_id` pointing to the stale run. `create_run()` already supports `parent_run_id` — the orchestration logic is missing.

### Exact Implementation Path

- Add `workspace_head_sha` and `workspace_diff_hash` to the `payload` dict of `RUN_CREATED` journal events
- Before resume: compute current workspace state, compare with stored values
- If mismatch: `transition(run_id, RunStatus.INTERRUPTED)`, then `create_run(..., parent_run_id=run_id)`
- If match: proceed with `--conversation {agent_conversation_id} --continue`

---

## 9. Minimal AgenticExecutionBackend Contract

```
AgenticExecutionBackend (extends ExecutionBackend):

    # Identity
    backend_id: str                          # e.g. "antigravity", "gemini-cli", "codex-cli"
    trust_zone: ExecutionTrustZone
    supported_capabilities: Set[ExecutionCapability]
    cost: ExecutionCost

    # Required methods (in addition to ExecutionBackend ABC)
    def get_health() → ExecutionHealth
    def execute(request: ExecutionRequest) → ExecutionResult

    # Agentic-specific methods
    def get_session_id(result: ExecutionResult) → Optional[str]
        """Extract conversation/session ID from execution result."""

    def supports_resume() → bool
        """Whether this backend supports conversation continuation."""

    def resume(session_id: str, request: ExecutionRequest) → ExecutionResult
        """Continue a prior session. Raises if !supports_resume()."""

    def get_quota_hint() → Optional[QuotaHint]
        """Return best-effort quota availability hint. None = unknown."""

# QuotaHint (data contract only):
    available: bool
    remaining_requests: Optional[int]
    remaining_tokens: Optional[int]
    reset_at: Optional[float]          # epoch seconds
    cost_class: ExecutionCost
```

**Keep `ReasoningProvider` (`PlannerProvider` protocol) separate.** `AgenticExecutionBackend` wraps agentic CLI tools that perform multi-step workspace mutations. `PlannerProvider` wraps single-shot structured reasoning calls. They should not be conflated.

---

## 10. Resource Router Transition Map

### Current Routing

**Provider routing** (`src/aos/provider_registry.py:ProviderRouter.select()`):
- Iterates `preferred_providers` list from routing policy
- Filters: enabled, structured_output, data_classification, billing_class, credential presence
- Returns first eligible provider
- No scoring, no health check, no quota check, no latency consideration

**Execution routing** (`extensions/autonomy-fabric/execution_router.py:ExecutionRouter.select_backend()`):
- Filters by capability superset, health != UNAVAILABLE/QUOTA_EXHAUSTED
- Scores by cost: FREE_LOCAL=10, FREE_TIER_CLOUD=30, QUOTA_LIMITED=90
- AG penalized +50 when not required
- Sorts deterministically by (weight, backend_id)

### Minimum Transition Path

**Phase 1** (after FreeLLMAPI): Add dynamic signals to existing static routing.

In `ProviderRouter.select()`:
- Before returning a candidate, check `circuit_registry.is_provider_available(provider_id)` — already done in `ProviderFailoverReasoningBackend.execute()` but NOT in the router itself
- Accept an optional `ResourceLedger` to query quota state

In `ExecutionRouter.select_backend()`:
- Accept an optional `ResourceLedger` to modulate cost weights:
  - If a backend reports `QUOTA_EXHAUSTED` via ledger, skip
  - If a backend has session continuity advantage, reduce weight
  - If a backend has latency history, factor in

**Phase 2** (future): Replace hardcoded weights with a scoring function:

```
score(backend, request) =
    f(capability_match) +
    f(risk_class) +
    f(health) +
    f(quota_remaining) +
    f(scarcity) +
    f(historical_quality) +
    f(latency_p95) +
    f(session_continuity_bonus) +
    f(cost)
```

### Reusable Abstractions

- `ExecutionCost` enum — already exists
- `ExecutionHealth` enum — already exists
- `ProviderCircuitBreakerRegistry.summarize()` — provides healthy/open/probe counts
- `ProviderCircuitBreakerRegistry.per_provider_details()` — per-provider circuit state
- `ProviderEntry.priority` field — exists in registry but never used in routing

---

## 11. ContextPack Insertion Map

### Current Prompt/Context Assembly

**Location**: `src/aos/planning_kernel.py`

| Function | Line | What It Does |
|---|---|---|
| `_canonical_excerpt()` | L500 | Concatenates canonical control file contents with char-count bounds |
| `_bounded_prompt_excerpt()` | L1062 | Head/tail truncation with SHA-256 binding marker |
| `_situation_prompt_payload()` | L1304 | Assembles full situation dict for JSON serialization into prompt |
| `_bounded_workspace_file_manifest()` | L706 | Lists workspace files with bounded output |
| `_bounded_completed_read_context()` | L767 | Reads completed task target files with bounded slicing |
| `_completed_task_signatures()` | L864 | Generates deterministic task identity strings |
| `_bounded_task_signatures_for_prompt()` | L1014 | Bounds completed task signatures for prompt inclusion |
| `synthesize_project_situation()` | L609 | Master function: fetches canonical state, CI, gates, working tree |

### Opportunities for Deterministic Pre-Processing

1. **Git diff**: `_working_tree_state()` (L473) already runs `git status`, but does NOT include `git diff --stat` or changed file list
2. **Changed files**: Not currently extracted for the prompt
3. **Test failures**: `_ci_state()` (L480) fetches CI status but does not extract test failure details
4. **Bounded log slices**: Not currently used
5. **Content digests**: `_bounded_prompt_excerpt()` already computes SHA-256 of truncated content

### Recommended ContextPack Contract

```
ContextPack:
    project_id: str
    source_sha: str
    workspace_head_sha: str
    canonical_excerpt: str                # Already produced by _canonical_excerpt()
    canonical_excerpt_sha256: str         # Already computed
    workspace_diff_stat: Optional[str]    # NEW: git diff --stat output
    changed_files: List[str]              # NEW: list of modified paths
    test_failure_summary: Optional[str]   # NEW: bounded CI failure extract
    relevant_source_slices: Dict[str, str] # NEW: file path → bounded content
    content_digest: str                   # NEW: SHA-256 of all above combined
    total_chars: int
    truncated: bool
```

### Implementation

- Extract into `src/aos/context_pack.py`
- `synthesize_project_situation()` should produce a `ContextPack` alongside `ProjectSituation`
- `_situation_prompt_payload()` should accept `ContextPack` and use its pre-computed fields
- No embeddings/vector DB needed — the existing bounded excerpt approach is sufficient for the current architecture

---

## 12. Track A File-Conflict Matrix

FreeLLMAPI branch: `feature/aos-freellmapi-local-meta-provider-20260922-01`
Track A changed files vs. Resource OS expected touches:

| File | Track A Touches? | Resource OS Expected Touch? | Conflict Risk | When to Modify |
|---|---|---|---|---|
| `src/aos/autonomous_host.py` | ✅ YES | ✅ YES (ResourceLedger inject, AG rebind) | **HIGH** | WAIT_FOR_FREELLMAPI |
| `src/aos/provider_registry.py` | ✅ YES | ✅ YES (ledger-aware routing) | **HIGH** | WAIT_FOR_FREELLMAPI |
| `src/aos/provider_probe.py` | ✅ YES | ⚠️ MAYBE (quota metadata) | MEDIUM | WAIT_FOR_FREELLMAPI |
| `src/aos/provider_circuit.py` | ✅ YES | ✅ YES (QuotaGovernor integration) | **HIGH** | WAIT_FOR_FREELLMAPI |
| `src/aos/providers/nemotron.py` | ✅ YES | ✅ YES (rate-limit metadata capture) | **HIGH** | WAIT_FOR_FREELLMAPI |
| `src/aos/providers/groq.py` | ✅ YES | ✅ YES (rate-limit metadata) | MEDIUM | WAIT_FOR_FREELLMAPI |
| `src/aos/providers/gemini.py` | ✅ YES | ✅ YES (rate-limit metadata) | MEDIUM | WAIT_FOR_FREELLMAPI |
| `src/aos/providers/ollama.py` | ✅ YES | ⚠️ MAYBE (Qwen support) | LOW | WAIT_FOR_FREELLMAPI |
| `src/aos/providers/openai_compatible.py` | ✅ YES | ✅ YES (rate-limit metadata) | MEDIUM | WAIT_FOR_FREELLMAPI |
| `src/aos/providers/__init__.py` | ✅ YES | ⚠️ MAYBE (new exports) | LOW | WAIT_FOR_FREELLMAPI |
| `src/aos/providers/freellmapi_local.py` | ✅ YES (NEW) | ❌ NO | SAFE_PARALLEL | NO_TOUCH |
| `src/aos/providers/schema_utils.py` | ✅ YES (NEW) | ❌ NO | SAFE_PARALLEL | NO_TOUCH |
| `src/aos/planning_kernel.py` | ✅ YES | ✅ YES (ContextPack) | **HIGH** | WAIT_FOR_FREELLMAPI |
| `src/aos/control_panel.py` | ✅ YES | ❌ NO | SAFE_PARALLEL | NO_TOUCH |
| `src/aos/secure_store.py` | ✅ YES | ❌ NO | SAFE_PARALLEL | NO_TOUCH |
| `src/aos/workers/antigravity.py` | ✅ YES | ✅ YES (AG rebind) | MEDIUM | WAIT_FOR_FREELLMAPI |
| `src/aos/workers/antigravity_probe.py` | ✅ YES | ⚠️ MAYBE | LOW | WAIT_FOR_FREELLMAPI |
| `extensions/autonomy-fabric/antigravity_adapter.py` | ✅ YES | ✅ YES (AG session mgmt) | MEDIUM | WAIT_FOR_FREELLMAPI |
| `extensions/autonomy-fabric/execution_backend.py` | ❌ NO | ✅ YES (AgenticExecutionBackend) | SAFE_PARALLEL | SAFE_PARALLEL |
| `extensions/autonomy-fabric/execution_router.py` | ❌ NO | ✅ YES (ledger-aware routing) | SAFE_PARALLEL | SAFE_PARALLEL |
| `extensions/autonomy-fabric/run_registry.py` | ❌ NO | ✅ YES (session state) | SAFE_PARALLEL | SAFE_PARALLEL |
| `extensions/autonomy-fabric/native_workers.py` | ✅ YES | ❌ NO | SAFE_PARALLEL | NO_TOUCH |
| `extensions/autonomy-fabric/persistent_coordinator.py` | ✅ YES | ❌ NO | SAFE_PARALLEL | NO_TOUCH |
| `descriptors/nemotron.planner-policy.json` | ✅ YES | ⚠️ MAYBE (new fields) | LOW | WAIT_FOR_FREELLMAPI |
| `schemas/v0.1/planner_routing_policy.schema.json` | ✅ YES | ⚠️ MAYBE (quota fields) | LOW | WAIT_FOR_FREELLMAPI |
| `src/aos/resource_ledger.py` | ❌ NO (NEW) | ✅ YES (NEW) | SAFE_PARALLEL | SAFE_PARALLEL |
| `src/aos/quota_governor.py` | ❌ NO (NEW) | ✅ YES (NEW) | SAFE_PARALLEL | SAFE_PARALLEL |
| `src/aos/context_pack.py` | ❌ NO (NEW) | ✅ YES (NEW) | SAFE_PARALLEL | SAFE_PARALLEL |
| `extensions/autonomy-fabric/antigravity_execution_backend.py` | ❌ NO (NEW) | ✅ YES (NEW) | SAFE_PARALLEL | SAFE_PARALLEL |
| `docs/codex/freellmapi-integration/*` | ✅ YES | ❌ NO | DOC_ONLY | NO_TOUCH |
| `docs/codex/resource-os-preflight/*` | ❌ NO | ✅ YES (THIS AUDIT) | DOC_ONLY | SAFE_PARALLEL |

### Summary

- **HIGH conflict files (must wait)**: 6 (`autonomous_host.py`, `provider_registry.py`, `provider_circuit.py`, `nemotron.py`, `planning_kernel.py`, `providers/__init__.py`)
- **SAFE_PARALLEL new files**: 4 (`resource_ledger.py`, `quota_governor.py`, `context_pack.py`, `antigravity_execution_backend.py`)
- **SAFE_PARALLEL existing files**: 3 (`execution_backend.py`, `execution_router.py`, `run_registry.py`)

---

## 13. Dependency-Ordered Implementation Sequence

After FreeLLMAPI acceptance and merge to main:

| Phase | What | Dependencies | Files |
|---|---|---|---|
| **1** | **PlannerTransientError rate-limit metadata** | None | `src/aos/planner.py`, `src/aos/providers/nemotron.py`, `groq.py`, `gemini.py`, `openai_compatible.py`, `freellmapi_local.py` |
| **2** | **QuotaGovernor** | Phase 1 | NEW `src/aos/quota_governor.py`, modify `src/aos/provider_circuit.py` |
| **3** | **ResourceLedger** | Phase 2 | NEW `src/aos/resource_ledger.py`, modify `src/aos/autonomous_host.py` |
| **4** | **AG ExecutionBackend wrapper** | None (parallel with 1-3) | NEW `extensions/autonomy-fabric/antigravity_execution_backend.py` |
| **5** | **AG first-class rebind in host** | Phase 3, Phase 4 | Modify `src/aos/autonomous_host.py:build_execution_router()` |
| **6** | **AG session/resume safety** | Phase 5 | Modify `extensions/autonomy-fabric/run_registry.py`, `antigravity_adapter.py` |
| **7** | **Local Qwen via Ollama** | Phase 3 | Modify `src/aos/providers/ollama.py`, routing policy JSON |
| **8** | **Capability/scarcity router** | Phase 3 | Modify `src/aos/provider_registry.py`, `extensions/autonomy-fabric/execution_router.py` |
| **9** | **ContextPack** | None (parallel) | NEW `src/aos/context_pack.py`, modify `src/aos/planning_kernel.py` |
| **10** | **Fallback/re-entry** | Phase 6, Phase 8 | Modify coordinator/host |
| **11** | **Gemini CLI adapter** | Phase 4 pattern | NEW `extensions/autonomy-fabric/gemini_cli_backend.py` |
| **12** | **Codex CLI adapter** | Phase 4 pattern | NEW `extensions/autonomy-fabric/codex_cli_backend.py` |
| **13** | **Failure-injection E2E** | All above | NEW test files |

### Changes from Proposed Order

The user's proposed order had "Resource Ledger / Quota Governor" first.
**Corrected**: Rate-limit metadata capture in provider adapters (Phase 1) must come first because the QuotaGovernor has nothing useful to consume without it. The current hardcoded backoff tiers work but are blind to actual server-reported reset times.

Also: AG ExecutionBackend wrapper (Phase 4) can proceed in parallel with Phases 1-3 since it wraps an already-proven adapter and touches only new files.

---

## 14. Risks / Things Explicitly NOT To Do

1. **DO NOT modify the live runtime** (`feature/aos-runtime-v1-candidate-20260915-01` branch). This audit is docs-only.

2. **DO NOT touch FreeLLMAPI worktree** (`C:\Projects\AOS-freellmapi-local-20260922-01`). It has its own track and state.

3. **DO NOT enable paid fallback**. `PAID_FALLBACK=DISABLED` is a standing constraint.

4. **DO NOT redesign the AG adapter**. `AntigravityWorkerAdapter` and `AntigravityCLIAdapter` are proven. Wrap, don't rewrite.

5. **DO NOT implement vector DB / embeddings** for context economy. The existing bounded excerpt approach with SHA-256 binding markers is architecturally sound and sufficient.

6. **DO NOT merge Resource OS changes before FreeLLMAPI acceptance**. 6 high-conflict files overlap.

7. **DO NOT force-push any branch**.

8. **DO NOT recreate protected lineages** (LARI, LARI-UI-V2 workspaces).

9. **DO NOT implement scoring algorithms** in this audit. The routing transition map identifies WHERE scoring plugs in, not WHAT the scores should be.

10. **DO NOT modify `ProviderCircuitBreakerRegistry` persistence format** without migration. Existing circuit state files are durable and must remain loadable.

---

## 15. Exact Recommended First Implementation Step After FreeLLMAPI Acceptance

### Step: Rate-Limit Metadata Capture in PlannerTransientError

**Why first**: Every downstream component (QuotaGovernor, ResourceLedger, smart routing) needs this data. Without it, all quota-aware routing is flying blind and falls back to hardcoded tiers.

**Exact changes**:

1. `src/aos/planner.py`: Add optional fields to `PlannerTransientError`:
   - `retry_after_seconds: Optional[float] = None`
   - `rate_limit_metadata: Optional[Dict[str, Any]] = None`

2. `src/aos/providers/nemotron.py` L159-160: Extract headers before wrapping:
   ```
   In the RateLimitError catch block:
   - Read e.response.headers.get("Retry-After")
   - Read e.response.headers.get("x-ratelimit-remaining-requests")
   - Package into PlannerTransientError(retry_after_seconds=..., rate_limit_metadata=...)
   ```

3. Same pattern in `groq.py`, `gemini.py`, `openai_compatible.py`, `freellmapi_local.py`

4. `src/aos/autonomous_host.py` L300-313: In the `PlannerTransientError` catch block, extract `retry_after_seconds` from the exception and pass to `circuit_registry.record_failure()`.

5. `src/aos/provider_circuit.py:calculate_backoff()`: When `retry_after_seconds` is available, use `max(retry_after_seconds, MIN_BACKOFF)` instead of hardcoded tiers.

**Test**: Existing `tests/test_provider_circuit.py` + new test for metadata propagation.

**Risk**: LOW — additive change, backward-compatible (new fields are optional with None defaults).
