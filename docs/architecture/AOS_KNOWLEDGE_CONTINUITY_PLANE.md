# AOS Knowledge Continuity Plane

**Contract:** 1.0.0
**Safety posture:** `PRODUCTION=NO_GO`, `PAID_FALLBACK=DISABLED`

The Knowledge Continuity Plane (KCP) is Runtime V1's durable continuity layer.
It prevents accepted decisions, work receipts, module relationships, audit
findings, blockers, and handoffs from disappearing when an agent, session, or
tool changes. It is not a transcript store and it does not replace existing AOS
authority systems.

## Authority order

1. `CANONICAL_GOVERNANCE` is accepted, versioned governance in Git under
   `docs/project-control/`.
2. `OPERATIONAL_TRUTH` is a fresh machine-local `CURRENT_TRUTH` observation.
   An observation stored in KCP becomes historical immediately after its
   observation time and must never be treated as current runtime state.
3. `KNOWLEDGE_LEDGER` is the append-only continuity record for decisions,
   receipts, relationships, gaps, and supersession.
4. `SECOND_BRAIN_MIRROR` is a derived, advisory corpus. Its answers and sync
   state have no authority to mutate Git, `STATE`, `CURRENT_TRUTH`, admissions,
   runtime slots, production, or policy.

The ledger is authoritative for continuity. `index.json`, module graph queries,
materialized Markdown, Drive outbox files, and future Notebook sources are
rebuildable derivatives.

These invariants are mandatory: `KNOWLEDGE_LEDGER != SECOND_BRAIN_MIRROR`,
`SECOND_BRAIN_UNAVAILABLE != STATE_LOSS`,
`SECOND_BRAIN_ANSWER != DECISION_AUTHORITY`,
`ARCHIVE_SUCCESS != CHANGE_ACCEPTANCE`,
`COMPLETED_WORK_MUST_NOT_BE_REDISCOVERED_AS_UNKNOWN`,
`STALE_KNOWLEDGE_MUST_NOT_OVERRIDE_FRESH_OPERATIONAL_TRUTH`,
`EVERY_MEANINGFUL_MUTATION -> KNOWLEDGE_RECEIPT`,
`HIGH_IMPACT_ACTION -> KNOWLEDGE_CONTEXT_PREFLIGHT`,
`AUDIT_FINDING -> KNOWLEDGE_EVENT`, and
`SUPERSEDED_FACT != CURRENT_FACT`.

## Runtime layout

KCP stores machine-local data under `<runtime-home>/knowledge/`:

```text
knowledge/
  ledger.jsonl
  index.json
  snapshots/
  materialized/
  sync/
  receipts/
```

Each JSONL event has a monotonically increasing sequence, deterministic event
identity, previous-event hash, and content hash. Appends hold an OS-level file
lock, flush and fsync before returning, and never repair or rewrite historical
bytes silently. A corrupt or truncated ledger fails closed.

## Context and mutation boundaries

`python -m aos.knowledge context` selects only decisions, invariants,
relationships, blockers, audit findings, receipts, and next actions relevant to
the requested modules and paths. The pack is deterministic and capped at 32 KiB.
Operational truth is `NOT_QUERIED` unless a caller explicitly supplies a fresh
observation provider.

Runtime deployment, promotion, rollback, controlled execution, and platform
recovery have bounded KCP hooks. A local ledger failure prevents high-impact
acceptance or promotion from reporting success. External mirror failure instead
records `SECOND_BRAIN_SYNC_PENDING` or degraded state and preserves the local
ledger and product execution.

KCP never mutates product command state merely to record or read knowledge.

## Materialized second brain

`python -m aos.knowledge materialize` deterministically renders:

- `AOS_CURRENT_KNOWLEDGE.md`
- `AOS_ARCHITECTURE_AND_MODULE_MAP.md`
- `AOS_DECISIONS.md`
- `AOS_WORK_JOURNAL.md`
- `AOS_OPEN_GAPS_AND_AUDIT.md`
- `AOS_LIVE_AND_VERIFICATION_HISTORY.md`

`LOCAL_ONLY` is the default. `DRIVE_MIRROR` and `NOTEBOOK_ENTERPRISE` produce a
local outbox and pending manifest when no external transport is configured.
No OAuth token or provider secret belongs in source or materialized documents.
Notebook responses are advisory candidates only; normal governance is required
before any decision can become accepted.

## Bootstrap and audit

Bootstrap events say `BOOTSTRAP_FROM_CANONICAL_SOURCE`, bind the exact Git SHA,
and hash each source document. They do not claim that KCP existed historically,
and chat history is not imported as canonical truth. Audit findings and
resolutions are append-only events, so an audit can resume across process,
session, and model changes without manufacturing closure.
