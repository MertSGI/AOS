# AOS Protected Non-Production Activation Result

Recorded at: `2026-10-13T10:38:19Z`

## Terminal result

- Gate: `PASS_MAINTENANCE`
- Candidate: `95c4dd5513a37e6a9a2b486a3ccfdadb7966ca68`
- Source branch: `fix/aos-supervisor-hotswitch-20261012`
- Hosted CI: `38354019333` (`SUCCESS`)
- Protected slot: `candidate-runtime-v1.8-95c4dd5513a3`
- Candidate tree SHA-256: `29b5a0edfbf3df2cfb9c30b5cb5770248c35c4d31f7ec08dc4610bd0e2239725`
- Transaction: `activate-1791887810-11058200`
- Transition: `2691a34a493daa2c2dacb924ee180fa568964c30ff8f72ef766e9f3768ed6b5b` (`COMPLETED`)
- Runtime: `HEALTHY`, exact candidate SHA, PID `18264`
- Panel: `HEALTHY`, exact candidate SHA, PID `24632`
- Supervisor: PID `23488`, named singleton held, correlated source/slot/launch identity
- Maintenance: `PAUSED_SAFE`; autonomous spawning disabled
- Safety: `PRODUCTION=NO_GO`; `PAID_FALLBACK=DISABLED`

Official `stage` and separate `validate` both returned `PASS`. The activation transaction recorded a correlated shutdown acknowledgement for the previous supervisor PID `11668`, termination of owned PIDs `11668`, `15584`, and `16024`, and release of singleton `Local\\AOS.RuntimeV1.Supervisor` before the candidate supervisor started.

## Startup and scheduled task

The authorized legacy artifact `AOS-RuntimeV1-Supervisor.vbs.disabled` was backed up and quarantined outside Startup. Both preserved files are 433 bytes and have SHA-256 `9501EE5AA2B8D3EA6FA311ED5585B9A97043A6680686E1CC13C1FB71B1748B14`. Both current-user and common Startup directories contain zero AOS-named entries.

`AOS-RuntimeV1-Persistent` remains unchanged. After handoff it is `Ready`, still points to historical supervisor source `cf97eb9fe3635c7af7266e2045dac72a11dad1ee`, has `RestartCount=0`, `StartWhenAvailable=false`, and `MultipleInstances=IgnoreNew`. Isolated compatibility proof established that this supervisor implementation dynamically launches the active slot. No reboot or login persistence claim is made; the historical task entrypoint remains a persistence caveat requiring separate authority if it is ever to be updated.

## KCP and command safety

Protected KCP is `READY` at sequence `557`, head `3903711346b67c662c146bd9283eae7571345d34048d7c437cd1e5d414d826f6`, with zero unresolved deployment transitions.

- Implementation receipt: sequence 550, event `660903d35e0f8a1eea8f5d9d64c490a1a40275b0154819e1a366becad3d995e3`
- Verification receipt: sequence 551, event `46818a955a6f13118f4c7e8b4aa587f795b4ecd6cd1201338928eb6597581eff`
- Protected validation receipt: sequence 552, event `a74dbbf79695e6de4fa9d5bb9e943c47b34333dec0b89639362439eede6aacef`
- Materialization receipt: sequence 553, event `3c8ad003789ad64458d33e96726860192208815cb81eed26b64c77b40d4a7501`
- Activation intent: sequence 555, event `de0272090bb0302db7594798775f2eb138767effb56da1454097c4590a2292b8`
- Activation receipt: sequence 557, event `8c57232aa707c62f3ed98bf092ae87aec20b76f72ffcafb151c6e77f2d689bb4`

Admissions remain 12 `HOLD`, 12 `SUPERSEDED`, and 0 `ACTIVE`. Two historical state files retain stale worker PIDs, but both PIDs are absent; live command workers are zero. No historical command was resumed or replayed.

## Rollback and stop boundary

The pointer retains stable slot `candidate-runtime-v1.8-85ee6772b02a`; its slot record and candidate root exist. The completed transaction backup is present at `deployment-backups/activate-1791887810-11058200/transaction.json`. Rollback is therefore ready through the supported transaction interface. No rollback was needed.

Global unpause and both LARI admissions were intentionally not performed. Product remains at accepted R2 with R3 visual/browser verification not admitted. UI-V2 productization remains not admitted, and the dirty `C:\Projects\LARI-ui-v2-post-wp5-sync` worktree is preserved.
