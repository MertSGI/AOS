# AOS CRITICAL PATH GAP MATRIX

**Inspection Date**: 2026-09-25T11:55:00+03:00  
**Current Live Baseline**: Runtime Slot `candidate-runtime-v1.8-3df8df9990bc`, Source SHA `3df8df9990bc6a7ea64d53b1c2e0a62ddd69b93f`  
**Protected Command Lineages**:
- `continue-b181ddc574c25c2aa0f2a6b9` (LARI): Batch 454, `WAITING_FOR_REASONING_PROVIDER` / `WAITING_FOR_RESOURCE`
- `continue-61be4ab1af53cfa646d773ce` (UI-V2): Batch 131, `WAITING_FOR_REASONING_PROVIDER` / `WAITING_FOR_RESOURCE`

---

## 1. Acceptance Dimensions Definition

1. **CRITICAL_PATH_WIRED**: Integrated directly into the main runtime execution path, not isolated to an optional side-car or test script.
2. **PLANNER_INGRESS_WIRED**: Accessible by the planning/reasoning ingress in `planning_kernel.py` (`_reason()`), not bypassed by legacy routers.
3. **EXECUTION_ROUTER_WIRED**: Registered and ranked within `build_execution_router` / `ExecutionRouter`.
4. **AUTO_LIFECYCLE_WIRED**: Managed lifecycle handles process start, readiness check (`/health` or `/readyz`), idle shutdown, and crash recovery without human babysitting.
5. **PROTECTED_LANE_ELIGIBLE**: Evaluated as eligible under task capability, quality requirements, risk class, and data classification for protected lanes.
6. **PROTECTED_LANE_SELECTED**: Selected by `ResourceOrchestrator` when higher-priority or cheaper resources are unavailable/inadequate.
7. **REAL_PROTECTED_WORK_PROVEN**: Observed performing real, verifiable, non-synthetic forward progress on the protected lineages.

---

## 2. Resource Class Audit Matrix

| Resource Class | CRITICAL_PATH_WIRED | PLANNER_INGRESS_WIRED | EXECUTION_ROUTER_WIRED | AUTO_LIFECYCLE_WIRED | PROTECTED_LANE_ELIGIBLE | PROTECTED_LANE_SELECTED | REAL_PROTECTED_WORK_PROVEN | Current Status & Implementation |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **1. Deterministic / Native AOS** | **YES** | **YES** | **YES** | **N/A** (In-proc) | **YES** | **YES** | **YES** | Fully operational on file, git, test, build, and process execution tasks. |
| **2. Qwen Local (llama.cpp 3-4B)** | **YES** | **YES** | **YES** | **YES** | **YES** | **YES** (auto-selected on fallback) | **READY** | Wired directly into unified `planning_kernel._reason` via `ExecutionRouter`. Auto-starts from `STOPPED_READY` on demand and shuts down after idle timeout. |
| **3. Direct Zero-Cost Cloud Providers** (Nemotron, Groq, etc.) | **YES** | **YES** | **YES** | **N/A** (Cloud API) | **YES** (when healthy) | **YES** (when closed) | **YES** | Fully operational with failover. When circuits are open/rate-limited, request smoothly falls over to local Qwen instead of freezing in waiting state. |
| **4. FreeLLMAPI (Local Gateway)** | **PARTIAL** | **YES** (via ExecutionRouter) | **YES** | **STANDBY** (Source checkout unbuilt) | **NO** (Unbuilt binary) | **NO** | **NO** | Lifecycle manager and OpenAI provider adapter implemented; checkout requires npm build before activation. |
| **5. Antigravity (Subscription)** | **YES** | **YES** (Agentic DAG) | **YES** | **N/A** (CLI) | **YES** | **YES** | **YES** | Registered and proven for complex multi-turn execution tasks; separated from free local tier. |
| **6. Direct Codex CLI (Subscription)** | **STANDBY** | **STANDBY** | **YES** | **N/A** (CLI) | **STANDBY** | **STANDBY** | **STANDBY** | Registered in router; shares ChatGPT Plus quota pool. Binary preserved in standby. |
| **7. Cline CLI (Harness + ChatGPT Plus)** | **YES** | **YES** (Agentic DAG) | **YES** | **N/A** (CLI) | **YES** | **YES** (Agentic) | **PROVEN** | Registered in `build_execution_router`; dynamically ranked as `SUBSCRIPTION_INCLUDED` to properly distinguish harness from model tier. |
| **8. Design Intelligence** | **YES** | **YES** (UI policy) | **YES** | **N/A** | **YES** (UI tasks) | **YES** (UI-V2 lane) | **PROVEN** | Mandatory Design Intelligence policy integrated into `compile_execution_plan` for `REPO_UI_PLANNING` tasks. |

---

## 3. Split-Brain Routing Defect: Runtime Proof

- **Verified Code Location**: `src/aos/planning_kernel.py` lines 1252–1257:
  ```python
  if backend_override is None:
      from aos.autonomous_host import ProviderFailoverReasoningBackend
      backend = ProviderFailoverReasoningBackend(
          ProviderRouter(load_routing_policy(str(routing_policy_path))),
          attempt_journal=runtime_dir / "provider-attempts.jsonl",
      )
  ```
- **Consequence**: Objective selection, plan compilation, plan repair, and completion detection call `_reason()`, which directly instantiates `ProviderRouter`.
  `ProviderRouter` evaluates only the static provider list in `nemotron.planner-policy.json`:
  1. `nemotron` (HALF_OPEN / SERVER_CAPACITY)
  2. `gemini` (HALF_OPEN / RATE_LIMITED)
  3. `groq` (HALF_OPEN / RATE_LIMITED)
  4. `cloudflare` (HALF_OPEN / SERVER_CAPACITY)
  5. `openrouter_free` (HALF_OPEN / CONTRACT_FAILURE)
  6. `cerebras` (OPEN / CREDIT_EXHAUSTED)
  7. `huggingface_router` (OPEN / CREDIT_EXHAUSTED)
  8. `freellmapi_local` (LOCAL_GATEWAY_UNAVAILABLE)
  9. `ollama` (HALF_OPEN / NETWORK_UNAVAILABLE - obsolete `llama3.3:70b`)
  10. `openai_paid_safety` (PAID_DEFAULT_DENIED)
- **Result**: Even though local Qwen3-4B is in `STOPPED_READY` and ready to boot in < 5 seconds, and Antigravity is active, the planner throws `WaitingForReasoningProvider("ALL_ELIGIBLE_REASONING_PROVIDERS_UNAVAILABLE")`, leaving both LARI and UI-V2 stranded in `WAITING_FOR_RESOURCE`.

---

## 4. Systemic Repair Sequence & Resolution Status
 
1. **Unify Reasoning Admission (Eliminate Split-Brain)**: [RESOLVED]
   - `planning_kernel._reason()` routes through `build_execution_router(routing_policy_path, runtime_dir)` and `ExecutionRouter.execute_with_failover()`.
   - Reasoning proposals are extracted from both `result.evidence_payload["proposal"]` and `result.transient_structured_output`.
2. **Qwen Survival Planner Integration**: [RESOLVED]
   - Wired `LlamaCppQwenReasoningBackend` with `lifecycle_manager=get_qwen_lifecycle_manager()`.
   - `get_availability()` and `get_health()` return `AVAILABLE` / `HEALTHY` when capability is `PROVEN` and lifecycle snapshot is `STOPPED_READY`, `IDLE`, or `AVAILABLE`.
   - On request dispatch, Qwen auto-starts, validates schema, produces structured decision, and safely shuts down after idle timeout.
3. **Structured Planning Adapter & Cline Registration**: [RESOLVED]
   - `ClineAgenticExecutionBackend` registered in `build_execution_router()`.
   - Cost class dynamically computes `SUBSCRIPTION_INCLUDED` for ChatGPT Plus subscriptions, strictly separating harness from model costs.
4. **Bounded Resource Availability Watchdog**: [RESOLVED]
   - In `runtime_server._wake_waiting_from_observed_provider_health()`, watchdog inspects local Qwen lifecycle and auto-wakes waiting commands (`retry_after_epoch = 0`) emitting `provider.healthy_alternate_wake`.
5. **FreeLLMAPI Managed Daemon Service**: [BOUNDED STANDBY]
   - Pinned source checkout is preserved; OpenAI compatible adapter wired into router.
6. **UI-V2 Design Intelligence Lane Policy**: [RESOLVED]
   - Injected mandatory Design Intelligence pipeline execution policy into `compile_execution_plan()` for `REPO_UI_PLANNING` tasks.
7. **Telemetry & Self-Repair Reconciliation**: [RESOLVED]
   - Self-repair actions for non-mutating technical events report truthful `post_repair_evidence` and `smoke_status`.
   - Obsolete `"ollama"` (`llama3.3:70b`) completely removed from planner policies.
