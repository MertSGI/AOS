# AOS Integration Contracts

The normative machine-readable contract is
`docs/architecture/AOS_INTEGRATION_CONTRACTS.json`; its validator is
`schemas/v0.1/aos_integration_contract.schema.json`.

## Binding chain

Every executable task is bound as follows:

```text
project_identity + canonical_revision + control_generation
  -> command_lineage + objective_id + task_id + task_signature
  -> workspace_fingerprint + write_scope + lease_id
  -> resource_id + resource_class + capability_requirements
  -> health_state + quota_state + scarcity_state
  -> session_id? + checkpoint_id + evidence_id
  -> accepted_source_sha? + candidate_id? + runtime_slot?
  -> runtime_source_sha? + provenance_status
```

Question marks mean the value is nullable before that lifecycle stage; absence
must never be converted into success.

## Enforcement locations

| Contract | Enforcement |
|---|---|
| Exact canonical and execution binding | `planning_kernel.py`, `execution_authority.py` |
| Workspace/session compatibility | `workspace_fingerprint.py`, `agentic_resume.py` |
| Write scope and exclusive lease | planner validation, `integrity_reconciler.py` |
| Accepted-work dedupe and lost-work detection | `integrity_reconciler.py` |
| Health/quota/scarcity routing | provider observations, `QuotaGovernor`, `ResourceOrchestrator` |
| Human action freshness and provenance | `action_center.py` |
| Candidate/source/runtime SHA | candidate store, runtime assets/slots, provenance validator |
| Projection-only operations view | controller relay and control panel |

## Outcome partition

Every dispatched execution contributes to exactly one bucket:
`SUCCESS`, `RETRYABLE_PARTIAL`, `RESOURCE_WAIT`, `HUMAN_HOLD`,
`TERMINAL_FAILURE`, or `REPLAN_NOOP`. Pre-dispatch collision rejection and
accepted-work dedupe do not pretend a backend execution occurred. The relay
publishes the six counts, total, and partition validity.

## Compatibility and re-entry

Agentic exact-session resume is allowed only if backend ID, resource ID, source
SHA, workspace fingerprint, adapter contract, executable identity, auth mode, and
objective state remain compatible. If they do not, the stale session is rejected;
a caller may start a fresh context from the durable AOS checkpoint, but may not
replay an accepted signature.

## Unknown semantics

`UNKNOWN` is a truthful result when a historical store lacks instrumentation.
It is not zero and not healthy. A store becomes calculable only after its
`instrumentation.json` marker and durable ledgers exist and parse successfully.
