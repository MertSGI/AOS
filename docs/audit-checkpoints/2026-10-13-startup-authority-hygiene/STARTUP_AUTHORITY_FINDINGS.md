# Windows Startup authority hygiene addendum — 2026-10-13

Observed at `2026-10-13T06:10:48Z` during the continuous AOS activation-qualification mission.

## Confirmed login-dialog cause

The current-user Startup directory contains exactly one AOS-named entry:

- `AOS-RuntimeV1-Supervisor.vbs.disabled`
- length: `433` bytes
- SHA-256: `9501EE5AA2B8D3EA6FA311ED5585B9A97043A6680686E1CC13C1FB71B1748B14`
- embedded candidate: `1dd293ac1722e69b301e22b5dba693a3348d7219`
- embedded launcher: `%LOCALAPPDATA%\AOS\runtime-v1\candidate\1dd293ac1722e69b301e22b5dba693a3348d7219\launch_supervisor.py`

Windows reports this exact file as a `Win32_StartupCommand`. The `.disabled` extension has no machine or current-user file association, while `.vbs` is correctly associated with `VBSFile` and `%WINDIR%\System32\WScript.exe`. There is no AOS Run/RunOnce entry, policy Run entry, common-Startup entry, or AOS Startup shortcut. These observations, together with the operator's repeatable logon symptom, confirm that Explorer's attempt to open the unassociated `.disabled` Startup entry causes the “Choose an app” dialog. A genuinely unassociated `.vbs` is not the cause.

The artifact was inspected as text only. It was not opened through Shell, WScript, Python, or any other host.

## Scheduled-task and singleton distinction

Two AOS scheduled tasks exist:

1. `AOS-RuntimeV1-OneShot-Proof-20261009` is `Ready`, has no triggers, and targets the historical `1dd293ac...` launcher. It is not running and cannot start automatically at logon in its observed configuration.
2. `AOS-RuntimeV1-Persistent` is the actual logon authority. It is running and has one enabled logon trigger. Its task action is historically pinned to candidate `cf97eb9fe3635c7af7266e2045dac72a11dad1ee`; that supervisor currently owns `Local\AOS.RuntimeV1.Supervisor` and dynamically launches the active stable slot `85ee6772b02a0ab72f5c79633672083017380269`.

No process command line contains `1dd293ac...`. The protected supervisor PID observed during this inspection was `11668`; its exact stable runtime child was PID `16024` from `85ee6772...`.

At ordinary logon, the unassociated `.disabled` file reaches the chooser and does not execute, so it does not currently contend for the singleton. If a user selects WScript for the file, or `.disabled` later receives a script-host association, the embedded `1dd293ac...` supervisor can run against the shared protected supervisor root and contend with the scheduled-task supervisor for the same singleton. This is a real dormant ownership hazard.

The Startup artifact did not cause the independently reproduced isolated `RuntimeTransitionIncompleteError` hot-switch race. That race occurred in `C:\Projects\AOS-hotswitch-runtime-20261012-02` with Startup installation disabled and was repaired and qualified separately by commits `edaab389...`, `36230176...`, and `ccf61e807...`.

## Source defect and repair

At `ccf61e807a6b852c66b7aa544dd06676885b5f5b`, `validate_startup_ownership()` filtered only a fixed set of final suffixes. Because `Path.suffix` for `AOS-RuntimeV1-Supervisor.vbs.disabled` is `.disabled`, the legacy artifact escaped ownership validation.

Commit `95c4dd5513a37e6a9a2b486a3ccfdadb7966ca68` changes the check to treat every AOS-named file in a Startup directory as an ownership artifact, regardless of suffix. It therefore catches current, legacy, compound-disabled, backup-style, and unknown-extension variants. It does not automatically delete or move them.

Changed files:

- `src/aos/runtime_deploy.py`
- `tests/test_runtime_deploy.py`

Verification:

- focused deploy tests: `11 passed`
- adjacent deploy/supervisor/slot/transition suite: `53 passed`
- live read-only probe against the real Startup directory: expected fail-closed, naming the exact `.vbs.disabled` artifact
- hosted AOS CI run `38354019333`: terminal `SUCCESS` at exact SHA `95c4dd5513...`

## Safety boundary

The real Startup file, both scheduled tasks, file associations, Run keys, protected slot pointer, protected runtime processes, and protected KCP ledger were not changed. Quarantine requires fresh explicit authorization; see `QUARANTINE_PROCEDURE.md`.
