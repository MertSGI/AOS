# AOS controller activation preflight — 2026-10-12

Captured at `2026-10-12T18:54:11.6360434+00:00`. This is review-only evidence. No protected KCP, slot, runtime-maintenance, admission, Supabase, Vercel, production, or canonical-acceptance state was changed.

## Decision

Candidate `9dddf1e202da8e227adca492ba1c5020961c65b3` is source- and CI-qualified for a bounded protected activation transaction, but protected activation is **not authorized and has not occurred**.

- Branch: `fix/aos-runstatus-host-receipt-20261012`
- Parent repair: `8508979b6911ed3e7092c40b7df5a9d5c09b8e04`
- Hosted CI: run `38259843751`, exact head SHA `9dddf1e202da8e227adca492ba1c5020961c65b3`, `completed/success`
- Canary: `PASS` for four native executions in `C:\Projects\AOS-canary-runtime-20261012-04`
- Protected KCP coverage for `9dddf1e...`: **missing** (zero implementation and zero verification receipts at protected ledger sequence 530)
- Protected runtime: source `30369cf7871ea4e254ea1615df871507d919e4a1`, `HEALTHY`, `PAUSED_SAFE`, `NO_GO`, paid fallback disabled

## Independent canary verification

The preserved event journal contains four actual `native_router` runs and four recovered checkpoint projections. The recovered projections are not additional backend executions.

| Task | Run | Backend | Result |
|---|---|---|---|
| alpha mutation | `run-1b55033c2381` | `native_file_worker` | `COMPLETED`; SHA-256 `aec82b6ee66210c89db227346bb22558d07a0523b31f44d30a6e99aba13c11b2` |
| beta mutation | `run-1f4ab7b9bcd1` | `native_file_worker` | `COMPLETED`; SHA-256 `f5ec2cd141baa2f9a7dcf7c35d1e4ae18100d7eeaeb356b4202ab508ceb67c32` |
| alpha verification | `run-94b8fafcb88b` | `native_process_worker` | `COMPLETED` / host verification `PASS` |
| beta verification | `run-fa4cedb64321` | `native_process_worker` | `COMPLETED` / host verification `PASS` |

The re-entry journal grew from 20 to 36 events by creating four `recovered_checkpoint` run projections. Backend telemetry remained exactly two native file attempts and two native process attempts, all successful. No repeated file mutation or test backend invocation is present.

The runtime ledger contains only one `CURRENT_TRUTH_OBSERVATION`. It contains no canary implementation or verification receipt. The host receipt's `verified_tasks` is genuine verification evidence, but it is not a protected KCP receipt.

## Published-checkpoint limitations

Two SHA-256 values in `CANARY_HASHES.json` cannot be reproduced from the preserved runtime tree:

- Published host receipt hash: `807908b2da63d8fc45bfeee8fa1cb10f92b70f03e62fba2c4b5744cb438ae76b`
- Current preserved host receipt hash: `0f41ab06ea02af580e3fd0fff656edf712ac13bb539795d94dff895a4add691f`
- Published coordinator checkpoint hash: `d07c08d1723f05ce7b568ee611a5b8da22cfb9b8b08d4b31a5eb23bdfcfefcfd`
- Current preserved coordinator checkpoint hash: `487e8b9b8b0bad34ed0de83eac2b0e72166ad3ccfa0be4a2c79853926d157784`

Neither published value matches any current file under `C:\Projects\AOS-canary-runtime-20261012-04`. This does not invalidate the event/run/fixture evidence, but the two published runtime hash claims are `NOT_REPRODUCIBLE_FROM_PRESERVED_TREE` and must not be used as activation authority.

The canary runtime config records asset tree `338901f7...`, while its referenced candidate manifest records `279e4760...`. A fresh official materialization generated its own internally consistent manifest tree `38c4d398...`; tree hashes are materialization-specific and only manifest-bound values may be used.

## Protected truth and rollback readiness

- Protected ledger: sequence `530`, head `45197fd8dfd67833678aecfb2d60fb51820682df5afe2c7149ff1b9e71c43512`
- `9dddf1e...` protected accepted-work coverage: `MISSING_ACCEPTED_WORK_COVERAGE`
- Active pointer: `active=candidate`, candidate `candidate-runtime-v1.8-30369cf7871e`, promotion state `TRIAL_MAINTENANCE`
- Stable rollback slot: `candidate-runtime-v1.8-85ee6772b02a`, source `85ee6772b02a0ab72f5c79633672083017380269`
- Current activation transaction: `activate-1791794994-83a457b5`
- Current activation transition `a262338c...`: completed, not aborted; no unresolved runtime-deploy or runtime-slot transitions
- Startup authority file is absent; the transaction records `startup_existed=false` and `startup_managed=false`
- Command admissions: 12 `HOLD`, 12 `SUPERSEDED`, zero `ACTIVE`; stale recorded worker PIDs are not alive

Protected precondition hashes:

- `runtime-config.json`: `2e20a0c1ab680f865ce630616041d4ba39b1d57c3b56236bcdd13b2effa34200`
- `supervisor/active-slot.json`: `995f477a8afafc5226a1b25b7f3b9742ebbd99b9dadc8c851ba4306a6171a6a9`
- `knowledge/ledger.jsonl`: `33f3eddc907d080ade6a7f18c6a72e21ae2167eb65c8884a7aa19ebed17fcc4a`
- rollback transaction: `5ca07b3a323d45da875512fadb9224231f2598d797057abd926077cf180a9ada`

## Disposable activation proof

In `C:\Projects\AOS-activation-preflight-20261012-01`, a copy of protected ledger sequence 530 was used to exercise the exact transaction without touching protected state:

- implementation receipt: `01b5dd997d32f331f2d808a90d314fac38ca80950ed3f8f3a16cf67beb815860`
- verification receipt: `5bb66aee38fcfb9045144bc39ee8d5cbd30bcacc7b27d12615191b0ad7a025ae`
- accepted-work coverage: `COVERED`
- official stage: `PASS`
- official validate: `PASS`
- immutable candidate source/build SHA: exact `9dddf1e...`
- candidate import origins: every checked `aos` and `extensions` module resolved below the disposable candidate `site` directory
- production: `NO_GO`

Those two receipt IDs are disposable proof only and are **not** present in the protected ledger.

## Supported protected transaction — authorization required

1. Re-read the four protected precondition hashes, health, admission counts, and absence of live workers; stop on drift.
2. Append one genuine implementation receipt for the committed `30369cf... -> 9dddf1e...` four-file repair chain and one verification receipt bound to exact-SHA CI run `38259843751` plus the preserved canary evidence.
3. Run official `runtime_deploy.stage()` and `validate()` for `9dddf1e...`; this materializes the candidate and appends the official materialization receipt.
4. Normalize the existing trial by the supported rollback command for transaction `activate-1791794994-83a457b5`, restoring stable slot `85ee6772...`. Do not edit the slot pointer manually.
5. Activate `9dddf1e...` transactionally with Startup still disabled. Require exact-SHA health and retain `PAUSED_SAFE`; stop and rollback on any mismatch.
6. Verify zero legacy `ACTIVE` admissions. Only after explicit authorization, call normal global resume and immediately submit the Product lane. `submit_continue()` cannot stage a held command while paused: it rejects paused submissions and creates an `ACTIVE` admission when running.
7. Run Product first. Submit UI-V2 only after Product reaches its durable terminal/checkpoint boundary. Never issue a blind resume against stale commands.

After step 5, the newly created deployment transaction is the rollback handle to stable `85ee6772...`. No Supabase migration, Vercel production deployment, canonical acceptance mutation, or paid fallback is included.

## LARI authority

Remote `randapp/control/lari-project-control-plane` is exactly `14589d3a68a1654281ceeec2f3b9445fbbaa807a`. Both lane execution bases are `06ebf176e411faf6820ded34987f894cc5e7dc9e`.

- `lari`: R2 is already accepted. Next work is the authorized R3 visual/browser validation; do not reimplement R2.
- `lari-ui-v2`: synchronization/productization remains pending from the same base. `pages/BookingPage.tsx` remains exclusively owned by `lari` without an explicit handoff.
- Product base worktree `C:\Projects\LARI-product-post-wp5-runtime` is clean.
- Existing UI-V2 base worktree `C:\Projects\LARI-ui-v2-post-wp5-sync` is dirty and must be preserved; use a fresh isolated worktree for AOS execution.

## Authorization boundary

No protected action has been taken. Explicit operator authorization is required for the receipt append, protected stage, trial rollback/normalization, activation, unpause, and lane submissions described above.

## Surgical Planner Repair Status (2026-10-13)

Surgical planner recovery repair has been implemented and qualified on branch `fix/aos-planner-recovery-repair-20261013` at commit `0cf2c869dfaa6d76337a130722e608cc01a08003`.
Hosted GitHub CI passed (`validate-and-test` run 38399455635).
Isolated replay confirmed that independent valid `BROWSER` tasks (`TASK-03`, `TASK-04`) are retained while completed process actions, undeclared Playwright runners, and out-of-lane control writes are safely pruned pre-normalization.
Product command `continue-3a7a49c2de91fa8d60e6f9bc` remains on `HOLD` in protected runtime `PAUSED_SAFE` awaiting operator activation authorization.

