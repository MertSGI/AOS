# AOS activation qualification handoff — 2026-10-13

Recorded at `2026-10-13T05:40:43Z` from the continuous AOS/LARI recovery mission.

## Outcome

The isolated non-production activation gate is **PASS** for exact AOS SHA `ccf61e807a6b852c66b7aa544dd06676885b5f5b`.

- Source branch: `fix/aos-supervisor-hotswitch-20261012`
- Remote and local SHA: `ccf61e807a6b852c66b7aa544dd06676885b5f5b`
- Hosted CI: run `38351323428`, exact SHA, terminal `SUCCESS`
- Final candidate tree: `a67ff6478e31b940e88fbd20a7a63b0f16d7f31be5e042311c55599ace892d3a`
- Isolated runtime: `C:\Projects\AOS-hotswitch-runtime-20261012-02`
- Startup authority: disabled
- Production: `NO_GO`
- Paid fallback: `DISABLED`

## Failures found and repaired

1. Candidate `edaab3890e7584024ebc499230f9dd7edd31ccff` lost the named-singleton race after the stable supervisor acknowledged shutdown. State/PID checks completed before the named mutex was demonstrably available. Commit `36230176b4e6ca3ebeabfdf85931f4d9fb22c734` requires successful acquire-and-release proof of the configured singleton before candidate launch.
2. Candidate `36230176b4e6ca3ebeabfdf85931f4d9fb22c734` reached `PASS_MAINTENANCE`, then exited when `read_json()` returned its empty fallback during the final atomic replacement of `active-slot.json`. Commit `ccf61e807a6b852c66b7aa544dd06676885b5f5b` retries transient invalid pointer snapshots for a bounded interval and still fails closed if the pointer remains invalid.

The complete delta from protected-receipted SHA `9dddf1e202da8e227adca492ba1c5020961c65b3` to the qualified SHA changes six files: `src/aos/runtime_deploy.py`, `src/aos/runtime_slots.py`, `src/aos/runtime_supervisor.py`, `tests/test_runtime_deploy.py`, `tests/test_runtime_slots.py`, and `tests/test_runtime_supervisor_identity.py`.

## Verification

- Focused runtime/deploy/singleton/transition suite at `36230176`: `44 passed`.
- Real Windows named-mutex conflict/release probe at `36230176`: PASS.
- Hosted CI run `38350280073` at `36230176`: SUCCESS.
- Focused runtime/slots/deploy/singleton/transition suite at `ccf61e807`: `48 passed`.
- Hosted CI run `38351323428` at `ccf61e807`: SUCCESS, including offline, PostgreSQL, full canonical, and evidence validation phases.
- Official isolated stage: PASS.
- Separate official isolated validate: PASS.
- Hot-switch transaction `activate-1791869681-cc170d4c`: `PASS_MAINTENANCE`; correlated shutdown acknowledged; old supervisor and owned processes stopped; named singleton released; exact candidate runtime and panel healthy; `PAUSED_SAFE` retained.
- Post-return durability window: PASS.
- Candidate restart/re-entry: PASS with a new supervisor PID; exact SHA retained; zero command replay.
- Supported rollback: PASS; transaction rollback completed; stable SHA `85ee6772b02a0ab72f5c79633672083017380269` relaunched healthy and paused.

The isolated ledger is `READY` at sequence 582 with no unresolved deploy transitions. The final disposable state is deliberately rolled back to stable and healthy.

## Protected runtime boundary

The protected runtime was read only. It remains exact stable SHA `85ee6772b02a0ab72f5c79633672083017380269`, `HEALTHY`, `PAUSED_SAFE`, `active=stable`, `NO_GO`, with ledger sequence 549. No protected receipt for `ccf61e807...` exists; the candidate is not staged or activated there.

Command admission truth is 12 `HOLD`, 12 `SUPERSEDED`, 0 `ACTIVE`. Eight legacy command state files still contain `state=RUNNING`, but every corresponding admission is non-runnable and their two recorded worker PIDs are dead. This must be revalidated immediately before any protected mutation.

## LARI lanes

- Product `lari`: R2 remains accepted. R3 visual/browser validation is pending. Canonical control SHA is `14589d3a68a1654281ceeec2f3b9445fbbaa807a`; exact execution base is `06ebf176e411faf6820ded34987f894cc5e7dc9e`.
- `lari-ui-v2`: productization synchronization is pending from the same execution base. Existing dirty work at `C:\Projects\LARI-ui-v2-post-wp5-sync` is preserved and must not be overwritten; future AOS execution requires a fresh isolated worktree.

No LARI command was admitted during qualification.

## Current boundary

Fresh operator authorization is required before adding the two protected KCP receipts, staging or activating `ccf61e807...`, unpausing the protected runtime, or admitting either LARI lane. See `NEXT_AUTHORIZATION.md` for the exact bounded transaction.
