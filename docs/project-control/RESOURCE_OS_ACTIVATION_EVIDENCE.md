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
