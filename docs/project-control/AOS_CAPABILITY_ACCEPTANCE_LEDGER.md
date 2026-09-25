# AOS Canonical Capability & Architecture Acceptance Ledger

Generated: 2026-09-25T06:12:00+03:00  
Authority: Continuous Post-Activation Operationalization & System Acceptance Matrix  
Contract Version: `1.0.0`  
Production Gate: `PRODUCTION=NO_GO`  
Paid API Fallback: `DISABLED` (`PAID_CALLS_MADE=0`)

---

## 1. Authoritative Identity & Verification Manifest

| Property | Value | Verification Source / Command |
| :--- | :--- | :--- |
| **EVIDENCE_CARRIER_HEAD** | `8297378679b84621f102e0113c8c2289636021bf` | `git log -n 1 origin/feature/aos-resource-os-master-20260923-01` |
| **VALIDATED_RUNTIME_SOURCE_SHA** | `cb77d36ab5fdb690e6eb5e76b12a9bd2a95e753f` | Bound exact source SHA of candidate |
| **RUNTIME_SHA** | `cb77d36ab5fdb690e6eb5e76b12a9bd2a95e753f` | `http://127.0.0.1:8770/v1/health` -> `runtime_source_sha` |
| **RUNTIME_SLOT** | `candidate-runtime-v1.8-cb77d36ab5fd` | `supervisor/active-slot.json` (Promoted `STABLE`) |
| **CI_RUN** | `36088079679` | GitHub Actions Workflow Run (Conclusion: `success`) |
| **FULL_CANONICAL_CI** | `1069 passed, 5 skipped, 26 deselected` | Exact-SHA CI canonical test suite |
| **FOCUSED_CONTROL_PANEL** | `13 passed` | `pytest tests/test_control_panel.py` |
| **FOCUSED_LLAMA_CPP_LIFECYCLE** | `6 passed` | `pytest tests/test_llama_cpp_lifecycle.py` |
| **LARI_LAST_COMPLETED_BATCH** | `441` (Advanced from `436` via `438`) | Event stream seq `26551` (`batch.completed` for batch 441) |
| **LARI_CURRENT_STATE** | `HUMAN_REQUIRED` (`RECOVERY_CHURN_GUARD`, `worker_pid: None`) | `http://127.0.0.1:8770/v1/commands/continue-b181ddc574c25c2aa0f2a6b9` |
| **UI_V2_LAST_COMPLETED_BATCH** | `127` (Preserved Lineage) | Protected lineage `continue-61be4ab1af53cfa646d773ce` preserved in state CAS |
| **UI_V2_CURRENT_STATE** | `HUMAN_REQUIRED` (`RECOVERY_CHURN_GUARD`, `worker_pid: None`) | `http://127.0.0.1:8770/v1/commands/continue-61be4ab1af53cfa646d773ce` |
| **DESIGN_INTELLIGENCE** | Multi-Viewport Dimensional Proof Proven | 6 Playwright viewports (375-1920px), SHA-256 bound, 8 critics PASS |
| **FREELLMAPI_ROUTE** | Bounded Route Proven (Zero-Cost Backing) | ResourceOrchestrator -> FreeLLMAPI -> ZeroCost -> ResourceLedger (`OPERATIONAL_BOUNDED`) |
| **QWEN_LIFECYCLE** | Live Managed Lifecycle Proven | `STOPPED_READY` -> `AVAILABLE` -> `BUSY` -> `IDLE_SHUTDOWN` -> `STOPPED_READY` |
| **CLINE_DISPOSITION** | `NOT_OPERATIONALLY_PROVEN` | Official npm package not installed; truthfully classified |
| **PAID_CALLS_MADE** | `0` | Verified ResourceLedger & QuotaGovernor invariants |
| **PRODUCTION** | `NO_GO` | Fail-closed runtime safety gate |

---

## 2. Multi-Dimensional Capability Acceptance Matrix

Every capability exposes explicit dimensions:
- **SOURCE_PRESENT**: Source code exists in the repository.
- **TESTED**: Automated unit / integration test suite passes.
- **REAL_EXECUTION_PROVEN**: Executed against real infrastructure / process / artifact.
- **RUNTIME_WIRED**: Integrated into active Runtime V1 engine and supervisor.
- **LIVE_PROMOTED**: Running inside the active promoted runtime slot.
- **COCKPIT_VISIBLE**: Visible in the active operator control panel (`:8765`).

| Capability ID | Category / Name | SOURCE_PRESENT | TESTED | REAL_EXECUTION_PROVEN | RUNTIME_WIRED | LIVE_PROMOTED | COCKPIT_VISIBLE | Operational Disposition |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **CORE-001** | Durable Project State | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **CORE-002** | Authority & Human Gate Policy | YES | YES | YES | YES | YES | YES | **OPERATIONAL_BOUNDED** |
| **CORE-003** | Protected Lineage Preservation | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **CORE-004** | Candidate Store & CAS Provenance | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **CORE-005** | Production Gate (`NO_GO`) | YES | YES | YES | YES | YES | YES | **OPERATIONAL_BOUNDED** |
| **CORE-006** | Rollback & Workspace Isolation | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **AUT-001** | Autonomy Agent Run Registry | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **AUT-002** | Antigravity CLI Adapter | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **AUT-003** | Parallel Worker Supervisor | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **AUT-004** | Task DAG & Graph Execution | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **AUT-005** | Authority Decision Router | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **AUT-006** | Completion Supervisor | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **AUT-007** | Evidence Aggregator | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **AUT-008** | Controller Relay (CR2-Lite) | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **RES-001** | Provider Observation Model | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **RES-002** | Provider Circuit Breakers | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **RES-003** | Recovery Churn Guard | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **RES-004** | Content-Aware Read Dedup | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **RES-005** | Quota Governor & Resource Ledger | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **RES-006** | Workspace Fingerprints | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **RES-007** | Resource Orchestrator | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **RES-AG** | Antigravity Agentic Worker | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **RES-CDX** | Codex Agentic Worker | YES | YES | YES | YES | YES | YES | **STANDBY_VISIBLE** |
| **RES-CLN** | Cline Official CLI Adapter | YES | NO | NO | YES | YES | YES | **NOT_OPERATIONALLY_PROVEN** |
| **RES-QWN** | Qwen 3 Local GGUF Reasoning | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **RES-FREE** | FreeLLMAPI Local Gateway | YES | YES | YES | YES | YES | YES | **OPERATIONAL_BOUNDED** |
| **RES-NEMO** | NVIDIA Nemotron Reasoning | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **RES-GROQ** | Groq Cloud Reasoning | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **RES-GEM** | Google Gemini Flash | YES | YES | YES | YES | YES | YES | **STANDBY_VISIBLE** |
| **RES-CF** | Cloudflare Workers AI | YES | YES | YES | YES | YES | YES | **STANDBY_VISIBLE** |
| **RES-OPENR** | OpenRouter Free Router | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **RES-CER** | Cerebras Trial | YES | YES | NO | YES | YES | YES | **STANDBY_VISIBLE** |
| **RES-HF** | Hugging Face Router | YES | YES | NO | YES | YES | YES | **STANDBY_VISIBLE** |
| **DES-R10** | Versioned Design Contracts | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **DES-R11** | Reference Intelligence | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **DES-R12** | Design DNA & Palette Generator | YES | YES | NO | YES | YES | YES | **TESTED_RUNTIME_WIRED** |
| **DES-R13** | 8-Critic Multi-Role Ensemble | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **DES-R14** | Multi-Viewport Browser Visual QA | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **DES-R15** | Taste Memory & Feedback | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **DES-R16** | Tenant Media & Factual Engine | YES | YES | NO | YES | YES | YES | **TESTED_RUNTIME_WIRED** |
| **DES-R17** | Autonomous Design Loop Pipeline | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **GOV-001** | Council Deliberation (Shadow) | YES | YES | YES | YES | YES | YES | **OPERATIONAL_BOUNDED** |
| **GOV-002** | System Self-Diagnosis | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |
| **GOV-003** | Autonomous Self-Repair (Shadow) | YES | YES | YES | YES | YES | YES | **OPERATIONAL_BOUNDED** |
| **GOV-004** | Controller Relay Ingress | YES | YES | YES | YES | YES | YES | **OPERATIONAL** |

---

## 3. Provider Cockpit Rationalization

To avoid operator cognitive fatigue and prevent provider noise, resources in the Control Panel are partitioned into explicit operational tiers:

### 3.1 CORE Operational Resources
- **Antigravity CLI**: Active primary coding and task execution agent.
- **Codex CLI**: Standby coding agent (quota exhausted; ready for immediate resumption upon quota reset).
- **Qwen 3 (Local GGUF)**: Zero-cost on-demand local inference (`llama-server.exe` + `Qwen3-4B-Q4_K_M.gguf`) with automatic idle shutdown and orphan prevention.
- **NVIDIA Nemotron / Groq / OpenRouter**: Active zero-cost cloud reasoning providers.
- **FreeLLMAPI**: Local zero-cost meta-provider gateway with bounded Resource OS routing.

### 3.2 FALLBACK / STANDBY Resources
- **Google Gemini**: Standby under rate-limit circuit observation.
- **Cloudflare Workers AI**: Standby under rate-limit circuit observation.
- **Cerebras / Hugging Face**: Standby under credit exhaustion probe backoff.
- **Ollama**: Local fallback standby.
- **Cline CLI**: Bounded standby (`NOT_OPERATIONALLY_PROVEN` - official package not installed).

---

## 4. Protected Lineage Progress & Verification

### 4.1 Main LARI (`continue-b181ddc574c25c2aa0f2a6b9`)
- **Baseline Batch:** 436
- **Last Completed Batch:** **441**
- **Current State:** `HUMAN_REQUIRED` (`failure_class: RECOVERY_CHURN_GUARD`, `worker_pid: None`)
- **Progression History:**
  1. Header normalization applied to managed workspace files (`EV055_R3_HARDENING_STATUS.md`, `EV056_R3_HARDENING_STATUS.md`, `EV057_R2_HARDENING_STATUS.md`, `EV058_R1_HARDENING_STATUS.md`).
  2. Worker restarted via authenticated runtime endpoint `POST /v1/commands/restart-worker`.
  3. Batch 436 executed -> Batch 437 completed.
  4. Batch 437 executed -> Batch 438 completed (seq `26506`).
  5. Batches 438, 439, 440 executed autonomously -> Batch 441 completed (seq `26551`).
  6. Subsequent provider quota exhaustion reached churn guard boundary; held safely without state loss.
  7. Invariant `command.accepted=1`, single command ID preserved, zero state loss, zero duplication of completed work.

### 4.2 UI-V2 (`continue-61be4ab1af53cfa646d773ce`)
- **Baseline Batch:** 127
- **Last Completed Batch:** **127**
- **Current State:** `HUMAN_REQUIRED` (`failure_class: RECOVERY_CHURN_GUARD`, `worker_pid: None`)
- **Design Intelligence Bounded Proof:** Playwright multi-viewport headless capture executed across 6 viewports (375, 390, 768, 1024, 1440, 1920px), SHA-256 cryptographically bound, 8-critic evaluation ensemble passed (`overall_verdict: PASS`).
- **Lineage Integrity:** Preserved without alteration. Forbidden lineage `continue-009df28644d7a108fbaa6014` remains strictly unresumed and unmutated.

---

## 5. Verification Sign-Off

- **Production Gate:** `NO_GO`
- **Zero-Cost Commitment:** Verified `PAID_CALLS_MADE=0`.
- **Runtime Promotion:** Promoted slot `candidate-runtime-v1.8-cb77d36ab5fd` to `STABLE` in `active-slot.json`.
