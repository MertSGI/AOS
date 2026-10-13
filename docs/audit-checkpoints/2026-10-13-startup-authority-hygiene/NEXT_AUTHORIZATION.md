# Next authorization boundary

Exact candidate `95c4dd5513a37e6a9a2b486a3ccfdadb7966ca68` is source-, CI-, stage-, validate-, hot-switch-, restart-, KCP-, and rollback-qualified in the isolated non-production runtime. The protected runtime and Windows persistence surfaces have not been changed.

Fresh explicit authorization is required because the candidate SHA and the protected persistence preconditions differ from the prior authorization.

## Requested bounded sequence

1. Revalidate the protected SHA, stable pointer, health, `PAUSED_SAFE`, protected ledger head, zero unresolved transitions, 12 HOLD, 12 SUPERSEDED, zero ACTIVE admissions, zero live command workers, `PRODUCTION=NO_GO`, and paid fallback disabled.
2. Authorize the exact reversible quarantine in `QUARANTINE_PROCEDURE.md` for only `AOS-RuntimeV1-Supervisor.vbs.disabled` at SHA-256 `9501EE5A...B1748B14`. Do not execute it, alter associations, or change either scheduled task.
3. Revalidate that no AOS-named file remains in either Startup directory and that the quarantined copy retains the exact hash.
4. Append exactly one protected implementation receipt and one protected successful verification receipt for `95c4dd5513...`, grounded in the actual six-file delta from protected-receipted `9dddf1e...`, hosted CI runs `38350280073`, `38351323428`, and `38354019333`, and the isolated qualification package. Do not append duplicate receipts for intermediate descendant commits.
5. Officially stage and separately validate exact SHA `95c4dd5513...` with CI run `38354019333`.
6. Activate transactionally with Startup installation disabled. Keep `PAUSED_SAFE`; require correlated shutdown, process termination, singleton release, exact source/tree health, panel health, completed KCP transition, rollback capability, and a durability window.
7. Stop and use supported rollback on any drift or failed invariant. Do not manually alter slot pointers or control JSON.

Protected unpause and LARI admission remain a separate post-activation gate. If later authorized, submit only a fresh exact-canonical Product `lari` command first; wait for genuine terminal evidence before admitting `lari-ui-v2`. Never resume historical command IDs.
