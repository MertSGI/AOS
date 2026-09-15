# AOS Runtime Contract V1

Status: **candidate runtime integration**. Production remains **NO_GO**.

Runtime Contract V1 is the only normal orchestration boundary for AOS Direct and
`aosctl`. The contract is structured JSON over loopback HTTP and durable JSON /
JSONL state on disk. Stdout/stderr are diagnostic only and are not orchestration
contracts.

## Normal invocation

The default project is preconfigured by the runtime. A normal continuation
request therefore needs only a goal:

```json
{
  "contract_version": "1.0.0",
  "command_type": "continue_project",
  "goal": "Continue LARI Program V2 to completion within standing authority.",
  "production": "NO_GO",
  "ag_backend_enabled": false
}
```

The runtime expands the configured project profile into a durable command that
binds descriptor, workspace and routing policy paths. A human-authored run plan
is not part of normal Runtime V1 invocation.

## API

- `GET /v1/health` — unauthenticated loopback health only.
- `POST /v1/commands/continue` — submit autonomous goal command.
- `GET /v1/commands/{id}` — structured command/state/result snapshot.
- `GET /v1/commands/{id}/events?after_seq=N` — ordered structured events.

Write/read command endpoints require the local Runtime V1 token. CORS is not
supported and the API binds only to `127.0.0.1`.

## Durable continuation

The runtime persists command state before starting a worker. A worker uses the
Planning Kernel checkpoint under the command runtime directory. When a worker is
terminated, the Runtime V1 server detects the dead PID, respawns the worker and
the Planning Kernel resumes the active batch before requesting new reasoning.
Completed work remains represented by the underlying PersistentCoordinator and
Planning Kernel checkpoints.

## Candidate/stable slots

The supervisor maintains an atomically replaced `active-slot.json`. Runtime V1
starts as a candidate trial. Repeated candidate health failures switch the
active pointer back to the stable legacy host slot. This rollback is a runtime
safety mechanism and is not evidence that Runtime V1 itself is stable.

Promotion of the candidate slot requires a separate accepted proof identifier.
Packaging as a Windows Service or executable does not itself satisfy autonomy
acceptance.
