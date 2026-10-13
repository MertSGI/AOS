# Reversible quarantine procedure — authorization required

This procedure is documented but **was not executed**. It changes protected login persistence and requires fresh explicit operator authorization.

## Preconditions

1. Re-read the current-user and common Startup directories, both AOS scheduled tasks, Run/RunOnce keys, active slot, supervisor state, maintenance state, and protected ledger head.
2. Require the protected runtime to remain `HEALTHY`, `PAUSED_SAFE`, `active=stable`, `PRODUCTION=NO_GO`, paid fallback disabled, with no ACTIVE admissions or live command workers.
3. Require the source file to remain exactly:
   - name: `AOS-RuntimeV1-Supervisor.vbs.disabled`
   - length: `433`
   - SHA-256: `9501EE5AA2B8D3EA6FA311ED5585B9A97043A6680686E1CC13C1FB71B1748B14`
4. Stop if the path, hash, contents, task state, runtime state, or authority changes.

## Authorized operation

Use native PowerShell path operations end-to-end. Resolve both absolute paths and verify that the source is inside the current-user Startup directory and the destination is inside `%LOCALAPPDATA%\AOS\runtime-v1\disabled-startup-sink\2026-10-13\`.

1. Create the dated destination directory if absent.
2. Copy the source to the destination and verify the copied SHA-256 equals the precondition hash.
3. Move the original out of Startup to the same verified destination using `Move-Item -LiteralPath`; do not rename it in place and do not delete it.
4. Verify that no AOS-named file remains in either Startup directory.
5. Verify the quarantined file still has the exact original SHA-256.
6. Re-read protected supervisor health, singleton ownership, active slot, maintenance state, KCP head, admissions, and worker liveness. Do not start or stop either scheduled task.

The preserved destination should be:

`%LOCALAPPDATA%\AOS\runtime-v1\disabled-startup-sink\2026-10-13\AOS-RuntimeV1-Supervisor.vbs.disabled`

## Reversal

Reversal is another protected persistence mutation and requires separate authorization. If authorized, require the destination hash above, prove the Startup destination is empty of every AOS-named artifact, then move the exact preserved file back with `Move-Item -LiteralPath`. Reversal intentionally restores the login chooser hazard and must not be used as a normal recovery step.

## Explicit non-actions

- Do not execute or open the quarantined script.
- Do not change `.disabled` or `.vbs` file associations.
- Do not enable the one-shot task.
- Do not install or modify scheduled tasks.
- Do not delete either the source evidence or quarantined copy.
- Do not activate Startup authority during candidate deployment.
