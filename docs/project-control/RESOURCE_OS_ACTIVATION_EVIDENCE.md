# Resource OS Activation Evidence

Status: `ACTIVATION_IN_PROGRESS`

## Phase A - exact-SHA CI and pre-activation gate

- Original accepted source: `a91896faa0798ab95238d138be01b3bbdba9e571`.
- Original documentation carrier: `6bdc287e7f3091111f7f7a639e51833dda3076de`.
- Hosted activation preflight exposed case-sensitive completed-read resolution and nondeterministic unit-test startup-thread races. The minimum portability repair and deterministic test isolation were committed without changing live runtime or protected state.
- Validated activation source: `bc816eb37649445d8562e5ac0fbfe3ea56dfab6f`.
- GitHub Actions run: `35961087026`, job `107509612971`, exact head SHA match, conclusion `success`.
- Hosted offline suite: `1057 passed, 5 skipped, 26 deselected in 30.72s`.
- Hosted PostgreSQL integration: `25 passed, 8 deselected in 2.66s`.
- Hosted full canonical suite: `1057 passed, 5 skipped, 26 deselected in 28.05s`.
- Hosted canonical `STATE.json` and `EVIDENCE.jsonl` validation: `PASS`.
- Local full canonical suite: `1054 passed, 8 skipped, 26 deselected in 374.34s`.
- Local focused planning/lifecycle suite: `71 passed`.
- Runtime/provider race regression: `20/20` repeated focused invocations passed; affected files suite `15 passed`.
- `git diff --check`: `PASS`.

Safety posture at the Phase A boundary:

- `PAID_CALLS_MADE=0`
- `PAID_API_FALLBACK=DISABLED`
- `LIVE_RUNTIME_CHANGED=false`
- `PROTECTED_LINEAGES_CHANGED=false`
- `PROTECTED_WORKSPACES_CHANGED=false`
- `UI_V2_STATUS=NOT_WOKEN`
- `JEV_RUNTIME_STATUS=DISABLED`
- `QWEN_RUNTIME_STATUS=SOURCE_COMPLETE / LOCAL_RESOURCE_PROOF_PENDING`
- `PRODUCTION=NO_GO`

Phase B may materialize a new immutable candidate slot from the validated activation source. It may not overwrite or activate the existing live slot.

## Phase B - immutable candidate materialization

- Deployment candidate slot: `candidate-runtime-v1.8-bc816eb37649`.
- Candidate source: `bc816eb37649445d8562e5ac0fbfe3ea56dfab6f`.
- Candidate path: `C:\Users\mozcelikbas\AppData\Local\AOS\runtime-v1\candidate\bc816eb37649445d8562e5ac0fbfe3ea56dfab6f`.
- Candidate tree SHA-256: `a33d98e4fd16bd7db6b64826d33f4417a3c5ef8dc95bdbb6f517a7900f0c0ee2`.
- Inventory: `196` files; exact-SHA provenance, file hashes, required roots, descriptor schemas, routing policy schema, secret exclusion, and isolated import origins all passed.
- Existing stable slot remains `candidate-runtime-v1.8-a0cfc2e329b3` at source `a0cfc2e329b36dca1964ed427ab17d8c82cbacb6`.
- The live slot pointer and live runtime configuration were not changed.

The repository smoke contract refuses the live Runtime V1 home by design. A second exact-source candidate was therefore materialized under the isolated activation-proof home. Its launcher paths differ because launchers are bound to their runtime home; all source assets remain bound to the same exact source and CI run.

- Isolated smoke ID: `2d06bd43065e48ff977a69b066db9b43`.
- Isolated candidate tree SHA-256: `0493d4250d701a61b3fa2809527ddaf85a0f74fa1a7018583833ca7cc148b770`.
- Real supervisor/runtime/panel process-tree smoke: `PASS`.
- Loopback ports: runtime `53605`, panel `53606`.
- Startup health maximum: `2.0552s`; steady health maximum: `0.0180s`.
- Synthetic history: `550` historical commands, one active command, one provider-waiting command.
- Shutdown: `QUIESCED`, no in-flight worker, `PAUSED_SAFE`.
- Orphan processes: `0`; visible windows: `0`.
- `PRODUCTION=NO_GO` throughout.

## Phase C - isolated runtime smoke

- `GET /v1/health`: `HEALTHY`, paused-safe, exact source `bc816eb37649445d8562e5ac0fbfe3ea56dfab6f`, slot `candidate-runtime-v1.8-bc816eb37649`, isolated tree `0493d4250d701a61b3fa2809527ddaf85a0f74fa1a7018583833ca7cc148b770`.
- Authenticated `GET /v1/status`: reachable with `status_state=PAUSED` and `production=NO_GO`. Detailed telemetry intentionally remains paused rather than traversing runtime history while maintenance is active; provenance is carried by the constant-time health contract.
- Candidate supervisor liveness, runtime restart while paused, panel health, recovery-loop startup, relay generation, and authenticated quiesced shutdown: `PASS`.
- Candidate-loaded ResourceLedger append and replay: `PASS`, hash chain intact, corruption false.
- Candidate-loaded QuotaGovernor persistence and replay: `PASS`; exact provider-metadata deadline and exhausted state survived restart.
- Candidate-loaded provider circuit persistence and task-class-aware replay: `PASS`, state `CLOSED` for `structured_planning`.
- Candidate-loaded Resource Orchestrator: `PASS`; `free-local` selected and the healthy paid-cloud candidate remained ineligible with `PAID_DEFAULT_DENIED`.
- Candidate-loaded ContextPack: `PASS`, `649` bytes and content fingerprint `c2a8507fd9d74770306744e22cf97765eb8a4dd54abd4b6f0e06572409999911`.
- Content-sensitive workspace fingerprint: `b09bae83b24ae34dc3a8a07401a58f0d8c0445c93db7cfaebc598f75d7977b0f`, exact clean source head `bc816eb37649445d8562e5ac0fbfe3ea56dfab6f`, `408` entries.
- Runtime proof artifact: `C:\Users\mozcelikbas\AppData\Local\AOS\runtime-v1-resource-os-activation\proofs\phase-c\phase-c-resource-smoke.json`, SHA-256 `1b84e2421634f60f8314fa4e368ee2052753bbdf9b497b1d89a2a5f363fdc8c9`.
- The smoke relay generated two local heartbeat snapshots. Existing authenticated relay transport also refreshed controller issue `#6`; it did not create a lineage, execute project work, call a model, or change production.
- `PAID_CALLS_MADE=0`, `PAID_API_FALLBACK=DISABLED`, `PRODUCTION=NO_GO`.

## Current-reality reconciliation - 2026-09-24

- Branch and remote were both `39b7921bbe302e5ecb27a0aa35bd8535cb95f047`; the worktree was clean before evidence edits.
- The live runtime is currently healthy and unpaused on the retained stable slot `candidate-runtime-v1.8-a0cfc2e329b3`, source `a0cfc2e329b36dca1964ed427ab17d8c82cbacb6`, tree `685a6a1b12a2515ac9674826faca93bbd87ff26bba2b73fc7edf0c87c4ac1203`, bound to `127.0.0.1`, with `PRODUCTION=NO_GO`.
- A temporary live observation of `39b7921bbe302e5ecb27a0aa35bd8535cb95f047` was reported before this checkpoint. It is not present in the current runtime configuration or process command lines. No deployment backup records a committed slot-pointer replacement after the stable `a0cfc2e...` activation, so this is classified as a transient candidate execution rather than an accepted cutover.
- The sole active command reported by the live runtime was the existing protected LARI lineage `continue-b181ddc574c25c2aa0f2a6b9`. Neither UI-V2 `continue-61be4ab1af53cfa646d773ce` nor forbidden stale LARI `continue-009df28644d7a108fbaa6014` was active.
- The protected LARI event log had `26,238` events at the bounded snapshot, a unique contiguous sequence, and exactly one `command.accepted` event. It contained `421` `batch.completed` events with unique contiguous batch numbers `1..421`. Durable state and result both reported batch `421` at the reconciliation boundary. This continues beyond both the transient zero-batch relay reading and the later observed batch `408`.
- The zero-batch reading is therefore classified as a relay/recovery snapshot artifact. Available evidence shows no command recreation, no completed-batch identity duplication, and no accepted-work loss. This statement is bounded to durable command/event/batch identity; it does not claim semantic equivalence beyond recorded receipts.
- Exact-path process inspection found four retained historical watchdog scripts and zero processes executing any of them. They remain non-authoritative and were not restarted.
- UI-V2 remains `OPERATOR_SUSPENDED`; the forbidden stale lineage remains `OPERATOR_CANCELLED_MISBOUND_GOAL`. Their durable timestamps were not advanced by this work.
- Reconciliation proof: `C:\Users\mozcelikbas\AppData\Local\AOS\runtime-v1-resource-os-activation\proofs\phase-0-current-reality-20260924.json`, SHA-256 `f10c45f9bd9bd7a28c3e4e87f4d958a7d77d70e78c91c52221ab6c7fe94d4a43`. The event-file digest inside that artifact is explicitly a non-atomic observation of a running lineage.

## Candidate supersession after activation-only Codex repair

- The real Codex capability probe exposed an activation defect: current Codex CLI sessions use RFC 9562 UUIDv7 identifiers, while the adapter accepted only UUID versions 1-5. The minimum parser repair and UUIDv7 regression test were committed as `39b7921bbe302e5ecb27a0aa35bd8535cb95f047`.
- Exact hosted CI run `35962472969`, job `107513807731`, matched that SHA and concluded `success`.
- A new immutable live-store candidate was materialized without changing the live pointer: slot `candidate-runtime-v1.8-39b7921bbe30`, tree `1b5770994856106359d187d03584e1fa8fafdf43d859565f1d97fe77c6aada2e`, `196` files.
- The isolated proof-store candidate tree is `55ad00080d4c2d16bc1f0b6079aa026e6b7501e4e6326e8ffc19c3b1a5f808f3`.
- Superseding isolated smoke `54579b4c90744e679eeb2dc950cdc70e` passed on runtime port `61997` and panel port `61998`: exact provenance, health, paused restart, quiesced shutdown, zero orphans, and zero visible windows.
- Older `bc816eb...` candidates remain immutable and available as historical evidence; they were not overwritten.

## Phase D - resource capability smoke

Phase D result: `PASS_WITH_TRUTHFUL_RESOURCE_CONSTRAINTS`.

- Deterministic/native execution: `AVAILABLE`, `FREE_LOCAL`. A candidate-loaded `NativeFileWorker` performed one bounded write in a disposable proof repository, and the Resource Orchestrator ranked native execution first for its matching capability.
- Zero-cost cloud reasoning: bounded sanitized probes succeeded for `groq`, `nemotron`, and `openrouter_free`. `gemini` and `cloudflare` were rate-limited; `cerebras` and `huggingface_router` reported credit exhaustion. These failures remained provider/resource failures, not project failures.
- FreeLLMAPI: source adapter and clean pinned checkout `15c30081d2ce832bea16d804d9edac4ed87c7bc3` are present. The local checkout is not built and the actual readiness probe is unavailable, so runtime status is `UNAVAILABLE`; no success is inferred from source presence.
- Antigravity: first-class source backend is present and classified `SUBSCRIPTION_INCLUDED`, but neither `agy` nor `antigravity` resolved to an executable and no current capability attestation could be proven. Runtime status is `UNAVAILABLE`.
- Codex CLI: executable identity `codex-cli 0.146.0-alpha.3`, SHA-256 `6aeaca6a797ed7e5d8163d750e10947f098ceb0f1faff02fedaef487602c2fe2`; ChatGPT subscription auth is proven, API-key presence is false, and cost is `SUBSCRIPTION_INCLUDED`. The quota observation was available with primary usage `73%` and secondary usage `58%`.
- A real candidate-loaded Codex backend read-only turn completed successfully with UUIDv7 session `01a0d20b-faaf-75d0-8067-7e729b043e49`, a durable checkpoint, a content-sensitive workspace fingerprint, and a clean Git workspace. Reusing the completed work ID was rejected with `COMPLETED_WORK_MUST_NOT_BE_DUPLICATED`; changing workspace content caused exact-session resume rejection with `STALE_AGENT_SESSION:WORKSPACE_CHANGED`; neither rejection invoked the CLI.
- Safe Codex writes cannot be accepted in the current nested host: the outer Codex permission envelope forces child sessions to read-only even when the backend requests `workspace-write`. A single disposable, non-protected direct CLI diagnostic using sandbox bypass proved that the subscription CLI itself can write the exact artifact; this deliberately unsafe diagnostic is recorded as classification evidence only and is **not** accepted as backend or protected-canary proof. The production backend continues to prohibit bypass flags.
- Qwen3-4B/llama.cpp: source adapter is present and `FREE_LOCAL`; no `llama-server`, model, benchmark, or capability attestation was found. Status remains `SOURCE_COMPLETE / LOCAL_RESOURCE_PROOF_PENDING`.
- Jev: the advisor contract remains present, advisory-only, disabled, with zero paid budget and no paid fallback. Its current route has `zero_price_observed=false`; no Jev call was made.
- Phase D proof: `C:\Users\mozcelikbas\AppData\Local\AOS\runtime-v1-resource-os-activation\proofs\phase-d\phase-d-resource-capability-smoke-39b7921.json`, SHA-256 `a0037d282b7c2617c436da4095897201868503048a2bfb1d85f4df028f8d6246`.
- `PAID_CALLS_MADE=0`, `PAID_API_FALLBACK=DISABLED`, protected workspaces unchanged, `PRODUCTION=NO_GO`.

## Phase E - quota, failure, failover, restart, and re-entry

Phase E result: `PASS` in a disposable fault-injection runtime using the exact candidate modules.

- The starting checkpoint was the real successful Codex Phase D identity `01a0d20b-faaf-75d0-8067-7e729b043e49`, including its workspace fingerprint and completed work unit `phase-d-readonly-1`.
- Typed observations exercised provider capacity failure, rate limiting, quota exhaustion, and credit exhaustion. Quota decisions were initially `EXHAUSTED` and ineligible with exact provider-metadata deadlines. Failure-family isolation retained independent `SERVER_CAPACITY`, `NETWORK`, and `CONTRACT` streaks of one.
- Injected Codex quota loss caused the actual Resource Orchestrator and Execution Router to exclude Codex without invoking it and select a scarce alternate fault-injection backend. This alternate is not evidence that the currently unavailable Antigravity executable became available.
- Cross-resource handoff preserved the same objective, completed work IDs, artifact map, and superseded the exact prior Codex session rather than resuming it on the wrong backend.
- The agentic checkpoint was serialized, then a new ResourceLedger, QuotaGovernor, provider-circuit registry, router, and backend set were constructed from disk. Ledger hash replay, quota deadlines, circuit family state, and checkpoint identity all survived restart.
- When Codex eligibility returned, the scheduler selected it over the scarce alternate and started fresh context from the handoff pack. It did not blindly resume the alternate session. Completed work became exactly `phase-d-readonly-1`, `phase-e-work-2`, and `phase-e-work-3`.
- Re-submitting `phase-e-work-3` was rejected with `COMPLETED_WORK_MUST_NOT_BE_DUPLICATED`. The real Codex workspace-change gate remained `STALE_AGENT_SESSION:WORKSPACE_CHANGED`.
- Advancing the injected clock beyond all exact quota deadlines made the affected resources eligible with `WINDOW_EXPIRED`; provider success closed the capacity circuit.
- Explicit invariant results: `BACKEND_CHANGE != PROJECT_CHANGE=PASS`, `QUOTA_LOSS != STATE_LOSS=PASS`, `PROVIDER_FAILURE != PROJECT_FAILURE=PASS`, `COMPLETED_WORK_MUST_NOT_BE_DUPLICATED=PASS`, `STALE_AGENT_SESSION_MUST_NOT_BE_BLINDLY_RESUMED=PASS`, `PAID_API != DEFAULT_ESCAPE_HATCH=PASS`.
- Proof: `C:\Users\mozcelikbas\AppData\Local\AOS\runtime-v1-resource-os-activation\proofs\phase-e\phase-e-continuity-proof.json`, SHA-256 `a37c5af57b7cbcd7254a58a27da6abf38f40676f2388d2ce86cbd373e36f951e`.
- `PAID_CALLS_MADE=0`, `PAID_API_FALLBACK=DISABLED`, protected lineages/workspaces unchanged, `PRODUCTION=NO_GO`.

## Phase F - protected LARI bounded canary

Phase F result: `PASS` on the existing protected lineage only.

- The stable runtime was authenticated-paused and then authenticated-quiesced before switching slots. The pre-canary durable checkpoint was batch `426`, event sequence `26,285`, workspace head `80ee72dbaa93d95a742995770c7bd80f69f0aaf2`, and workspace fingerprint `fff9c37d5e1f7fd0c8be24cc6b48531bb43b332feef952c1fb0bbafa01523571`. The existing 11 dirty Git entries were captured, not cleaned or reset.
- Built-in transactional activation `activate-1790244152-a955abce` started exact candidate `39b7921bbe302e5ecb27a0aa35bd8535cb95f047` as `candidate-runtime-v1.8-39b7921bbe30` in `TRIAL_MAINTENANCE`, paused-safe. Runtime health proved tree `1b5770994856106359d187d03584e1fa8fafdf43d859565f1d97fe77c6aada2e`; the stable rollback slot remained `candidate-runtime-v1.8-a0cfc2e329b3`.
- The transaction contains the previous runtime configuration, active-slot pointer, startup authority, and transaction metadata. Automatic rollback remains addressable by the exact transaction ID.
- Candidate resume recovered the same command `continue-b181ddc574c25c2aa0f2a6b9`; it did not create a replacement command. The canary produced one durable increment, batch `426 -> 427`, then returned to paused-safe and quiesced with no in-flight worker.
- Provider selection was `nemotron`; at least one healthy reasoning provider was present, all-provider-unavailable was false, and the runtime reported failover count `5`. Recovery count changed only once, `27 -> 28`, for the controlled slot handoff; no repeated recovery-churn sequence occurred during the canary window.
- Post-canary event sequence remained unique and contiguous through `26,303`, `command.accepted` remained exactly one, and batch-completion identities remained unique and contiguous through `427`. No duplicate completed task ID was found in the bounded recent receipt window.
- Workspace head and content-sensitive fingerprint remained exactly unchanged across the canary. Accepted state therefore advanced without a workspace identity violation.
- Candidate ResourceLedger replayed without corruption: `37` events comprising attempt start/finish, quota decisions, rate observations, resource usage, and one recovery disposition. QuotaGovernor replayed three records without corruption. Paid fallback remained ineligible.
- UI-V2 remained suspended, the forbidden stale lineage remained cancelled/inactive, and exact watchdog-script process count remained zero.
- Proof artifacts:
  - pre-canary: `C:\Users\mozcelikbas\AppData\Local\AOS\runtime-v1-resource-os-activation\proofs\phase-f\pre-canary.json`, SHA-256 `62419ca36b5ab403c40accc510e21d20817e3eedc8e2cfbc706c41c8a17d1f94`;
  - trial activation: `...\phase-f\trial-activation.json`, SHA-256 `a5902d37ac89e6662e8e8e1a669947d3883e3e6fa87b60ba58b6baf92b8c7419`;
  - canary observation: `...\phase-f\canary-observation.json`, SHA-256 `6caa425c14e65c8333108db20b4ef0b4396fabaf362bf195228350f8911ea0ac`;
  - post-canary: `...\phase-f\post-canary.json`, SHA-256 `181a1ba6c4e4b15f3261efb6e103c822526fb7e301d65f5a33942205b1891eae`.
- Current boundary after Phase F: candidate trial remains active but `PAUSED_SAFE`; it is not yet promoted. `PAID_CALLS_MADE=0`, `PAID_API_FALLBACK=DISABLED`, `PRODUCTION=NO_GO`.

## Phase G - controlled reversible cutover

Phase G result: `PASS` on exact validated source `27e67c9db359df8ea3e512beac15db25974d7ec8`.

- Two activation defects were found and repaired before final promotion. A repeated content-identical planning request reused a ResourceLedger attempt identity, so differing real usage collided with immutable accounting and terminalized LARI after batch 428. Actual provider invocations now receive distinct ledger attempt identities while the semantic request identity remains content-sensitive. The regression suite proved two repeated real calls produce two immutable accounting records.
- The detailed status/relay path selected the lexicographically last command ID instead of the most recently active command and excluded a lineage held by `RECOVERY_CHURN_GUARD` from command-local resource evidence. Status now orders by durable `updated_at`, retains guarded resource context, and reports the protected LARI lineage rather than an unrelated historical failure.
- The final source passed local canonical validation (`1056 passed, 8 skipped, 26 deselected in 288.66s`) and exact-SHA hosted CI run `35989481257`, job `107599873702`, conclusion `success`. Hosted full canonical pytest reported `1059 passed, 5 skipped, 26 deselected in 26.54s`; PostgreSQL integration and both canonical validators passed.
- Exact-source isolated smoke `e989fd99edad4386ad2ccacb9e1e982e` passed on ports `62835/62836`, with tree `bd461b45b475196ede1eb3de48085f5b258b277f078e420dd93190c60052fa1b`, zero orphans, zero visible windows, and `PRODUCTION=NO_GO`.
- Transaction `activate-1790247477-0dab9fd9` started slot `candidate-runtime-v1.8-27e67c9db359` in reversible trial maintenance. The retained rollback slot is `candidate-runtime-v1.8-39b7921bbe30`; transaction configuration, pointer, and startup-authority backups remain present.
- Before recovery, LARI was truthfully held at batch 428 as `HUMAN_REQUIRED / RECOVERY_CHURN_GUARD`. Corrected candidate telemetry found zero healthy providers but five legitimate zero-cost `HALF_OPEN` resources. Exactly one authenticated bounded recovery was authorized; no state or circuit file was rewritten.
- The same protected command `continue-b181ddc574c25c2aa0f2a6b9` advanced from batch 428 through batch 430. Authenticated pause-safe then reached `PAUSED_SAFE` with zero in-flight workers.
- At the quiesced acceptance boundary, event sequence `1..26359` and `batch.completed` identities `1..430` were unique and contiguous, `command.accepted` remained exactly one, recent completed task IDs had no duplicates, and the original collision at event 26316 had not recurred.
- ResourceLedger replay passed at 134 events with a valid hash chain and no corrupt/truncated state. QuotaGovernor replay was non-corrupt and bound to the current ledger rate head. Paid usage/cost remained zero.
- The protected workspace path and Git head `80ee72dbaa93d95a742995770c7bd80f69f0aaf2` were preserved. Its content-sensitive fingerprint changed because the existing 11-entry working set advanced during accepted LARI work; this content change is recorded and was not reset or misrepresented as identity stability.
- Promotion proof `RESOURCE-OS-PHASE-G-27E67C9-B430-1FF7421C60CD` atomically promoted the exact slot to `STABLE`. After authenticated resume, the same LARI lineage remained the sole active command. Relay sequence `64226` reported exact source/slot provenance `PROVEN`, LARI as Lane A at completed batch 430/current batch 431, and runtime health `HEALTHY`.
- UI-V2 remained `OPERATOR_SUSPENDED`; forbidden stale LARI remained `OPERATOR_CANCELLED_MISBOUND_GOAL`; neither worker was started.
- Proof artifacts:
  - pre-promotion: `C:\Users\mozcelikbas\AppData\Local\AOS\runtime-v1-resource-os-activation\proofs\phase-g\phase-g-27e67c9-pre-promotion.json`, SHA-256 `1ff7421c60cdc173191c4e6edf6d4cb5c2533a16eb2c04e02a2ac736fac1dfc0`;
  - post-promotion: `C:\Users\mozcelikbas\AppData\Local\AOS\runtime-v1-resource-os-activation\proofs\phase-g\phase-g-27e67c9-post-promotion.json`, SHA-256 `8a41714c85d46163e95f90e143945b924f86eb556a645ab75abca90508c379d2`.
- `PAID_CALLS_MADE=0`, `PAID_API_FALLBACK=DISABLED`, external watchdogs remained off, and `PRODUCTION=NO_GO`.

## Phase H - native recovery and watchdog retirement

Phase H result: `PASS` with the promoted runtime as the sole recovery authority.

- The stable runtime remained exact source `27e67c9db359df8ea3e512beac15db25974d7ec8`, slot `candidate-runtime-v1.8-27e67c9db359`, tree `debc5004147f16136075a6a7f15e322a41c7fcb6d2a97923de1faf7ec88f7a43`, running under `PRODUCTION=NO_GO`.
- The same protected LARI lineage advanced to completed batch `431` under native recovery. The bounded event snapshot contained `26,374` unique contiguous events, exactly one `command.accepted`, and `431` unique contiguous `batch.completed` identities.
- Native recovery evidence included durable worker respawn/recovery events, two strategy escalations, and one deterministic recovery-churn hold. Current state reported `NORMAL_RESUME`, same-fingerprint respawn count `1`, and strategy generation `2`.
- ResourceLedger replay independently verified all `158` events, their sequence, unique event identities, previous-hash links, and canonical hashes. It contained six recovery dispositions and zero actual paid cost. QuotaGovernor's persisted rate head matched the latest ledger rate observation.
- Provider circuits retained `12` task-scoped health records and isolated `CONTRACT`, `NETWORK`, and `SERVER_CAPACITY` failure families. Since the promoted runtime start, the attempt journal recorded `14` observations across four providers, with at most six records in one minute; no retry storm was observed.
- All four retained historical retry/watchdog scripts had zero matching processes. They remain on disk as historical evidence but are `RETIRED_NON_AUTHORITATIVE`; none was restarted or granted control authority.
- No direct state-file or circuit-file rewrite was used. UI-V2 remained `OPERATOR_SUSPENDED` with no worker, and forbidden stale LARI remained `OPERATOR_CANCELLED_MISBOUND_GOAL` with no worker.
- Proof: `C:\Users\mozcelikbas\AppData\Local\AOS\runtime-v1-resource-os-activation\proofs\phase-h\phase-h-native-recovery.json`, SHA-256 `f4a8b38d202ee676cc881b3bfa534d23ca5464a90b865bc81835d4f5a5f11d94`.
- `PAID_CALLS_MADE=0`, `PAID_API_FALLBACK=DISABLED`, and `PRODUCTION=NO_GO`.

## Phase I - Qwen local resource realization

Phase I result: `PASS` with a real bounded CPU-only resource proof and on-demand server lifecycle.

- Installed the official `llama.cpp` Windows x64 CPU build `b11149` (`0.5.0-dev`, commit `d2e54583c`). The downloaded archive matched GitHub's published SHA-256 `d1cb5f9ef7bbb7068954b4c9767d5b5309e20bcefeb61d4aafc47f9581f38752`; the executing `llama-server.exe` SHA-256 is `36e2803d3bc1c87ff21180dc1f1be1e53c6f04c515e91b46a480c1e1285471b4`.
- Downloaded the official Qwen `Qwen3-4B-Q4_K_M.gguf` artifact at upstream commit `bc640142c66e1fdd12af0bd68f40445458f3869b`. Its exact size is `2,497,280,256` bytes and its SHA-256 matched the upstream LFS OID `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`.
- The source router initially had no supported way to consume realized executable/model paths, and the real server version probe initially selected a changing startup-timestamp line. Minimal repairs added explicit `AOS_LLAMA_CPP_*`/`AOS_QWEN_MODEL_PATH` configuration, forced zero GPU layers, disabled Qwen thinking for bounded structured output, and bound attestation identity to the stable `version:` line.
- Exact repair source `820aa0b1f0328ef2828a0df18771704cfb29b0ae` passed local canonical validation (`1057 passed, 8 skipped, 26 deselected in 258.24s`) and hosted CI run `35994334139`, job `107615596620`, conclusion `success`.
- Real loopback startup reached health in `2,924.572 ms` with eight threads, zero GPU layers, context `4096`, parallelism `1`, and a measured peak RSS of `4,989,345,792` bytes, below the 12 GiB contract bound.
- The live structured benchmark correctly classified an HTTP 429 with intact project state as `provider_failure`, produced schema-valid JSON in `2,972.036 ms`, and measured `11.345` generated tokens/second from server timing. The machine-local capability status became `PROVEN`.
- A second request through the actual `LlamaCppQwenReasoningBackend` reported router availability `AVAILABLE`, execution `SUCCESS`, and schema-valid output. The bounded proof then stopped the server and verified zero orphans; no always-on service or hidden resource consumption was introduced. The capability remains available for on-demand loopback start, while current health correctly degrades when the server is stopped.
- The unified `llama.exe` benchmark launcher was blocked by Windows Defender as potentially unwanted before execution. No security control was bypassed; the separately packaged, hash-bound `llama-server.exe` provided the accepted runtime benchmark.
- Exact source was staged into both candidate stores. Isolated smoke `16c5e67dfa684f718727c043b3398182` passed on ports `55407/55408` with zero orphans and zero visible windows.
- Transaction `activate-1790250430-a9c33643` promoted slot `candidate-runtime-v1.8-820aa0b1f032` with proof `RESOURCE-OS-PHASE-I-820AA0B-QWEN-DBDC34A2B553`; rollback slot `candidate-runtime-v1.8-27e67c9db359` remains retained. After an exact-source supervisor restart, both runtime and panel reported `820aa0b1f0328ef2828a0df18771704cfb29b0ae`. The same LARI lineage resumed at batch `431`; UI-V2 and forbidden stale LARI remained workerless.
- Proofs:
  - Qwen runtime: `C:\Users\mozcelikbas\AppData\Local\AOS\runtime-v1-resource-os-activation\proofs\phase-i\phase-i-qwen-local-proof.json`, SHA-256 `dbdc34a2b553b97bf62be102030adadce854a1837484f816a197962175c5499e`;
  - activation/continuity: `C:\Users\mozcelikbas\AppData\Local\AOS\runtime-v1-resource-os-activation\proofs\phase-i\phase-i-activation.json`, SHA-256 `8a9714110fdce93a7278f23ba1033c24ea7ffdd0cd055e357892fd5a043bf844`.
- `PAID_CALLS_MADE=0`, `PAID_API_FALLBACK=DISABLED`, and `PRODUCTION=NO_GO`.
