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
