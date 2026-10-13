# Supervisor hot-switch debug status - 2026-10-13

## Root cause status

`CONFIRMED_RACE; REPAIR_COMMITTED_AND_CI_GREEN; REPAIRED_ACTIVATION_PROOF_MISSING`

`runtime_deploy.activate()` prepared a deployment transition and exposed an active-slot pointer containing `transition_state=INCOMPLETE_HOLD` while the old stable supervisor was still running. That supervisor's `SlotManager` was created before the transition and had no matching prepared-transition identity. On its next pointer read it correctly failed closed with `RuntimeTransitionIncompleteError`. The newly launched candidate supervisor concurrently attempted to acquire the same singleton. Depending on timing, it either lost and exited or acquired the singleton after the old supervisor crashed. This explains why the protected attempts aborted while the isolated reproduction eventually completed.

## Exact reproduced traceback

```text
Traceback (most recent call last):
  File "C:\Projects\AOS-hotswitch-runtime-20261012-01\candidate\85ee6772b02a0ab72f5c79633672083017380269\site\aos\runtime_supervisor.py", line 567, in run
    return self._run_owned_loop()
           ^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Projects\AOS-hotswitch-runtime-20261012-01\candidate\85ee6772b02a0ab72f5c79633672083017380269\site\aos\runtime_supervisor.py", line 632, in _run_owned_loop
    pointer = self.slots.read_pointer()
              ^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Projects\AOS-hotswitch-runtime-20261012-01\candidate\85ee6772b02a0ab72f5c79633672083017380269\site\aos\runtime_slots.py", line 139, in read_pointer
    raise RuntimeTransitionIncompleteError(
aos.runtime_slots.RuntimeTransitionIncompleteError: RUNTIME_TRANSITION_INCOMPLETE:bb60b6991794362651838dc0268edd74848e7991afc71b54f9af4be612155c09
```

Observed phase: `READ_SLOT_POINTER`. Old stable supervisor PID: `27524`. Old runtime PID: `7532`. Old source: `85ee6772...`. Target source: `9dddf1e...`. Isolated transaction: `activate-1791837337-95ede1e0`; transition: `bb60b6991794362651838dc0268edd74848e7991afc71b54f9af4be612155c09`.

The durable raw record is attached as `SUPERVISOR_FAILURE_DIAGNOSTIC.json`.

## Repair at `edaab389...`

- `src/aos/runtime_supervisor.py`: records sanitized, immutable per-exit diagnostics before cleanup; distinguishes unexpected exception, singleton conflict, and requested shutdown; records the active phase and process/slot/transition identity.
- `src/aos/runtime_deploy.py`: before pointer mutation, requests an authenticated-by-file handoff from an existing supervisor, requires a request-correlated acknowledgement, `SHUTDOWN` state, singleton release, and termination of all recorded owned PIDs; aborts the prepared transition if handoff proof fails.
- `tests/test_runtime_supervisor_identity.py`: proves unexpected exceptions survive final cleanup and singleton conflict is classified separately.
- `tests/test_runtime_deploy.py`: proves a correlated handoff waits for acknowledgement and owned-process exit, and rejects stale shutdown state.

## Remaining proof gap

Do not call this candidate activation-qualified yet. `edaab389...` still needs a newly materialized isolated candidate, genuine isolated implementation and verification KCP receipts, official stage/validate, real stable-to-candidate activation, paused exact-SHA health, restart/re-entry, and rollback-safety evidence. No protected retry is authorized.
