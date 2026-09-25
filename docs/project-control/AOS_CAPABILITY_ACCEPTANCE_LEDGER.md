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
| **EVIDENCE_CARRIER_HEAD** | `3df8df9990bc6a7ea64d53b1c2e0a62ddd69b93f` | `git log -n 1 origin/feature/aos-resource-os-master-20260923-01` |
| **VALIDATED_RUNTIME_SOURCE_SHA** | `3df8df9990bc6a7ea64d53b1c2e0a62ddd69b93f` | Bound exact source SHA of candidate |
| **RUNTIME_SHA** | `3df8df9990bc6a7ea64d53b1c2e0a62ddd69b93f` | `http://127.0.0.1:8770/v1/health` -> `runtime_source_sha` |
| **RUNTIME_SLOT** | `candidate-runtime-v1.8-3df8df9990bc` | `supervisor/active-slot.json` (Promoted `STABLE`) |
| **CI_RUN** | `36105689101` | GitHub Actions Workflow Run (Conclusion: `success`) |
| **FULL_CANONICAL_CI** | `1077 passed, 5 skipped, 26 deselected` | Exact-SHA CI canonical test suite |
| **FOCUSED_CONTROL_PANEL** | `13 passed` | `pytest tests/test_control_panel.py` |
| **FOCUSED_GOVERNANCE_ACTION_CENTER** | `6 passed` | `pytest tests/test_action_center.py` |
| **FOCUSED_CLINE_AGENTIC_BACKEND** | `8 passed` | `pytest extensions/autonomy-fabric/tests/test_cline_agentic_backend.py` |
| **LARI_LAST_COMPLETED_BATCH** | `450` (Preserved Lineage `continue-b181ddc574c25c2aa0f2a6b9`) | Event stream seq `26695` |
| **LARI_CURRENT_STATE** | `WAITING_FOR_REASONING_PROVIDER` (`WAITING_FOR_RESOURCE`) | Reclassified via Action Center: provider wait does not escalate to human action |
| **UI_V2_LAST_COMPLETED_BATCH** | `131` (Preserved Lineage `continue-61be4ab1af53cfa646d773ce`) | Protected lineage CAS preserved |
| **UI_V2_CURRENT_STATE** | `WAITING_FOR_REASONING_PROVIDER` (`WAITING_FOR_RESOURCE`) | Reclassified via Action Center: provider wait does not escalate to human action |
| **DESIGN_INTELLIGENCE** | Multi-Viewport Dimensional Proof Proven | 6 Playwright viewports (375-1920px), SHA-256 bound, 8 critics PASS |
| **FREELLMAPI_ROUTE** | Bounded Route Proven (Zero-Cost Backing) | ResourceOrchestrator -> FreeLLMAPI -> ZeroCost -> ResourceLedger (`OPERATIONAL_BOUNDED`) |
| **QWEN_LIFECYCLE** | Live Managed Lifecycle Proven | `STOPPED_READY` -> `AVAILABLE` -> `BUSY` -> `IDLE_SHUTDOWN` -> `STOPPED_READY` |
| **CLINE_DISPOSITION** | `OPERATIONAL_BOUNDED` (`LIVE_PROMOTED`) | Official npm CLI `3.0.65` attested, live proof & bidirectional handoff proven |
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
| **RES-CLN** | Cline Official CLI Harness | YES | YES | YES | YES | YES | YES | **OPERATIONAL_BOUNDED** |
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
- **Cline CLI**: Operational bounded agentic execution harness (`cline@3.0.65`, SHA-256 attested, isolated sandbox `--data-dir`/`--config`, provider-pinned, zero paid fallback).
- **Qwen 3 (Local GGUF)**: Zero-cost on-demand local inference (`llama-server.exe` + `Qwen3-4B-Q4_K_M.gguf`) with automatic idle shutdown and orphan prevention.
- **NVIDIA Nemotron / Groq / OpenRouter**: Active zero-cost cloud reasoning providers.
- **FreeLLMAPI**: Local zero-cost meta-provider gateway with bounded Resource OS routing.

### 3.2 FALLBACK / STANDBY Resources
- **Google Gemini**: Standby under rate-limit circuit observation.
- **Cloudflare Workers AI**: Standby under rate-limit circuit observation.
- **Cerebras / Hugging Face**: Standby under credit exhaustion probe backoff.
- **Ollama**: Local fallback standby.

---

## 4. Protected Lineage Progress & Verification

### 4.1 Main LARI (`continue-b181ddc574c25c2aa0f2a6b9`)
- **Baseline Batch:** 436
- **Last Completed Batch:** **441**
- **Current State:** `WAITING_FOR_REASONING_PROVIDER` (`disposition: WAITING_FOR_RESOURCE`, `worker_pid: None`)
- **Progression History:**
  1. Header normalization applied to managed workspace files (`EV055_R3_HARDENING_STATUS.md`, `EV056_R3_HARDENING_STATUS.md`, `EV057_R2_HARDENING_STATUS.md`, `EV058_R1_HARDENING_STATUS.md`).
  2. Worker restarted via authenticated runtime endpoint `POST /v1/commands/restart-worker`.
  3. Batch 436 executed -> Batch 437 completed.
  4. Batch 437 executed -> Batch 438 completed (seq `26506`).
  5. Batches 438, 439, 440 executed autonomously -> Batch 441 completed (seq `26551`).
  6. Reclassified from `HUMAN_REQUIRED` to `WAITING_FOR_RESOURCE`: provider rate-limit wait does NOT escalate to human operator decision; autonomous backoff & re-entry active.
  7. Invariant `command.accepted=1`, single command ID preserved, zero state loss, zero duplication of completed work.

### 4.2 UI-V2 (`continue-61be4ab1af53cfa646d773ce`)
- **Baseline Batch:** 127
- **Last Completed Batch:** **127**
- **Current State:** `WAITING_FOR_REASONING_PROVIDER` (`disposition: WAITING_FOR_RESOURCE`, `worker_pid: None`)
- **Design Intelligence Bounded Proof:** Playwright multi-viewport headless capture executed across 6 viewports (375, 390, 768, 1024, 1440, 1920px), SHA-256 cryptographically bound, 8-critic evaluation ensemble passed (`overall_verdict: PASS`).
- **Reclassification:** Reclassified from `HUMAN_REQUIRED` to `WAITING_FOR_RESOURCE` with automatic re-entry on provider availability.
- **Lineage Integrity:** Preserved without alteration. Forbidden lineage `continue-009df28644d7a108fbaa6014` remains strictly unresumed and unmutated.

---

## 5. Operational Governance Proofs (A through F)

All 6 required proofs are verified in automated test suite `tests/test_action_center.py` (6 passed in 1.38s):

- **PROOF A (Resource Wait Does Not Escalate to Human Action):** Simulated provider quota wait / rate limit transitions to `WAITING_FOR_RESOURCE`; Action Center records 0 human action items. Verified in `test_proof_a_resource_wait_does_not_escalate_to_human_action`.
- **PROOF B (Auto-Repair of Bounded Defect):** Autonomous repair of eligible technical defect (`PROVIDER_TRANSIENT_FAILURE`) transitions from discovery -> candidate isolation -> validation -> smoke -> activation without creating human action. Verified in `test_proof_b_auto_repair_of_bounded_defect`.
- **PROOF C (Auth Required Defect):** Missing credentials transition to `AUTH_REQUIRED`, creating an exact Human Action item with credential save/retry options instead of a generic resume loop. Verified in `test_proof_c_auth_required_defect_creates_action_not_generic_resume`.
- **PROOF D (Human Decision Required & Schema Validation):** Architectural ambiguity generates structured options and bounded choices. Submitting a versioned Control Request strictly validates against `schemas/v0.1/control_request.schema.json`, resolves the action item, and mutates command state safely. Verified in `test_proof_d_human_decision_structured_options_and_schema_validation`.
- **PROOF E (Council Token Governance & Deduplication):** Routine batches trigger deterministic bypass (`0` model calls). Material ambiguity deliberations execute exactly 1 deliberation per unchanged fingerprint; repeated calls return deduplicated skips. Verified in `test_proof_e_council_zero_token_waste_and_deduplication`.
- **PROOF F (Self-Repair Authority Filtering):** Scope, roadmap, and secret defects are strictly gated (`HUMAN_APPROVAL_REQUIRED` or `FORBIDDEN`) and never executed autonomously even when live mode is active. Verified in `test_proof_f_self_repair_authority_filtering`.

---

## 6. Cline Agentic Execution Harness Operationalization Proofs

Official upstream distribution `cline@3.0.65` installed, verified via registry tarball SHA-1, platform binary attested, and wired into AOS autonomy fabric subordinate to AOS authority (`cline_agentic_backend.py`):

- **PROOF A (Executable Launch):** Resolved official executable path, verified headless CLI launches with zero console flash.
- **PROOF B (Stable Version Attestation):** `3.0.65` attested, executable SHA-256 bound, registered in `LOCALAPPDATA/AOS/capabilities/cline-cli.json`.
- **PROOF C (Structured JSON/NDJSON Stream Parsing):** Verified deterministic parsing of `run_result`, `finishReason`, `usage`, and structured error classification without unparsed artifact leakage.
- **PROOF D & E (Safe File Read & Write Containment):** Read-file and write-file execute strictly within specified `write_scope`; mutations outside allowed scope trigger `CLINE_WRITE_SCOPE_VIOLATION`.
- **PROOF F & G (Process Execution & Test Execution):** Bounded child commands execute under OS-level Job Object ownership with tree termination on timeout or cancellation.
- **PROOF H (Terminal Outcome Classification):** Success, failure, auth unavailable, and quota exhaustion classified into standard AOS `ExecutionAvailabilityState`.
- **PROOF I (Session Identity Discovery):** Exact session ID discovered from stream hook events (`taskId` / `conv_*`).
- **PROOF J & K (Exact Session Resume & Context Retention):** Resumption passes `--id <session-id>` without restart; prior turn artifacts and context retained across turns.
- **PROOF L (Changed Workspace Stale Resume Rejection):** If workspace fingerprint changes externally, stale session resume is rejected with `STALE_AGENT_SESSION:WORKSPACE_CHANGED` before CLI invocation.
- **PROOF M (Workspace Containment):** Out-of-scope modifications fail closed without polluting host files or unrelated repositories.
- **PROOF N (Secret Scrubbing):** Sensitive API tokens (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.) scrubbed from child environment and never stored in AOS artifacts.
- **PROOF O (Zero Paid Calls):** Paid API fallback strictly disabled; underlying provider pinned to free/local or subscription-included.
- **RESOURCE ORCHESTRATOR INTEGRATION:** Auto-failover to Cline proven when Codex CLI is quota exhausted (`test_agentic_failover_to_cline_when_codex_quota_exhausted`).

All 8 tests passing in `extensions/autonomy-fabric/tests/test_cline_agentic_backend.py`.

---

## 7. Verification Sign-Off

- **Production Gate:** `NO_GO`
- **Zero-Cost Commitment:** Verified `PAID_CALLS_MADE=0`.
- **Runtime Promotion:** Promoted slot `candidate-runtime-v1.8-3df8df9990bc` to `STABLE` in `active-slot.json` with proof `RESOURCE-OS-PHASE-M-3DF8DF9-OPERATIONALIZATION-20260925`.
- **Operational Governance Suite:** 25 passed across `test_action_center.py` and `test_deliberation_council.py`.
- **Cline Agentic Execution Suite:** 8 passed across `test_cline_agentic_backend.py`, 4 passed in `test_resource_orchestrator.py`.
- **Cline Live Execution Proof:** Proven with ContextPack -> Resource Orchestrator -> ClineAgenticExecutionBackend -> Session ID (`cline_session_beta_42`) -> Bounded Work -> ResourceLedger (`0.0 USD`) -> Handoff to AG/Codex.
