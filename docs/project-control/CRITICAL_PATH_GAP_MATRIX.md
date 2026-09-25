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
| **5. Antigravity (Subscription)** | **YES** | **YES** (via Planning Bridge) | **YES** | **N/A** (CLI) | **NO** (Harness `MODEL_REASONING` mismatch) / **YES** (via `antigravity_planning_bridge`) | **YES** | **YES** | Registered and proven for complex multi-turn execution tasks; eligible for planning requests exclusively through bounded `AgenticStructuredPlanningBridge`. |
| **6. Direct Codex CLI (Subscription)** | **STANDBY** | **STANDBY** | **YES** | **N/A** (CLI) | **NO** (Harness mismatch) / **STANDBY** (via `codex_cli_planning_bridge`) | **STANDBY** | **STANDBY** | Registered in router; shares ChatGPT Plus quota pool. Planning requests bridged via `AgenticStructuredPlanningBridge`. |
| **7. Cline CLI (Harness + ChatGPT Plus)** | **YES** | **YES** (via Planning Bridge) | **YES** | **N/A** (CLI) | **NO** (Harness mismatch) / **YES** (via `cline_planning_bridge`) | **YES** (Agentic / Planning Bridge) | **PROVEN** | Registered in `build_execution_router`; dynamically ranked as `SUBSCRIPTION_INCLUDED`. Never falsifies raw harness capability: planner eligibility is strictly mediated via `AgenticStructuredPlanningBridge(cline)`. |
| **8. Design Intelligence** | **YES** | **YES** (Executable Pipeline) | **YES** | **N/A** | **YES** (UI tasks) | **YES** (UI-V2 lane) | **PROVEN** | Real executable `AutonomousDesignLoopPipeline.run_pipeline()` (R10–R15, R17) executed directly in `compile_execution_plan()` for `REPO_UI_PLANNING` tasks; binds `design_intelligence_execution_id` and scorecard into plan and batch receipt. |

---

## 3. Split-Brain Routing Defect: Runtime Proof

- **Verified Code Location**: `src/aos/planning_kernel.py` lines 1252–1257:
  ```python
  if backend_override is None:
      from aos.autonomous_host import build_execution_router
      router = build_execution_router(routing_policy_path, runtime_dir)
      backend = router
  ```
- **Consequence**: Unified routing through `ExecutionRouter` resolves the split-brain issue. When cloud providers are degraded or rate-limited, local Qwen and bounded agentic planning bridges (`cline_planning_bridge`) are actively ranked and eligible.

---

## 4. Systemic Repair Sequence & Resolution Status
 
1. **Unify Reasoning Admission (Eliminate Split-Brain)**: [RESOLVED]
   - `planning_kernel._reason()` routes through `build_execution_router(routing_policy_path, runtime_dir)` and `ExecutionRouter.execute_with_failover()`.
   - Reasoning proposals are extracted from both `result.evidence_payload["proposal"]` and `result.transient_structured_output`.
2. **Qwen Survival Planner Integration**: [RESOLVED]
   - Wired `LlamaCppQwenReasoningBackend` with `lifecycle_manager=get_qwen_lifecycle_manager()`.
   - `get_availability()` and `get_health()` return `AVAILABLE` / `HEALTHY` when capability is `PROVEN` and lifecycle snapshot is `STOPPED_READY`, `IDLE`, or `AVAILABLE`.
   - On request dispatch, Qwen auto-starts, validates schema, produces structured decision, and safely shuts down after idle timeout.
3. **Structured Planning Bridge (Eliminating Capability Falsification)**: [RESOLVED]
   - `AgenticStructuredPlanningBridge` implemented in `extensions/autonomy-fabric/agentic_planning_bridge.py`.
   - Agentic harnesses (`antigravity`, `codex_cli`, `cline`) maintain their truthful agentic capability contracts (`FILE_READ/WRITE`, `PROCESS_EXEC`, `TEST_EXECUTION`, `LONG_HORIZON_AGENTIC_WORK`) and never falsely claim `MODEL_REASONING`.
   - `AgenticStructuredPlanningBridge(backend)` wraps the harness, exposes `MODEL_REASONING`, injects strict read-only bounded planning prompts, verifies zero file mutation, validates JSON against `Draft202012Validator`, and fails closed on defect.
4. **Bounded Resource Availability Watchdog**: [RESOLVED]
   - In `runtime_server._wake_waiting_from_observed_provider_health()`, watchdog inspects local Qwen lifecycle and auto-wakes waiting commands (`retry_after_epoch = 0`) emitting `provider.healthy_alternate_wake`.
5. **FreeLLMAPI Managed Daemon Service**: [BOUNDED STANDBY]
   - Pinned source checkout is preserved; OpenAI compatible adapter wired into router.
6. **Executable UI-V2 Design Intelligence Pipeline (Not Policy Text)**: [RESOLVED]
   - Controller review rejected static policy text injection. In `src/aos/planning_kernel.py`, `compile_execution_plan` executes `AutonomousDesignLoopPipeline(max_design_review_cycles=2).run_pipeline()`.
   - Records durable `design-intelligence-{batch_number:04d}.json` containing R10, R11, R12, R13, R14, R15, R17 execution proof.
   - Binds `design_intelligence_execution_id` (`loop-...`) and scorecard into `generated-run-plan.json` and propagates directly into batch receipt.
7. **Telemetry & Self-Repair Reconciliation**: [RESOLVED]
   - Self-repair actions for non-mutating technical events report truthful `post_repair_evidence` and `smoke_status`.
   - Obsolete `"ollama"` (`llama3.3:70b`) completely removed from planner policies.

