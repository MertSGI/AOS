# Resource OS Decisions

Track: `AOS-RESOURCE-OS-MASTER-20260923-01`

## D-001 — Accepted source is immutable input

The exact accepted remote head `ccdebfd7b5bdc32f7e95634ef2c464293356c872` is `RESOURCE_OS_BASE_SHA`. All implementation occurs on the isolated Resource OS branch and worktree.

## D-002 — Project truth and resource truth are separate

Canonical project checkpoints and completed work remain authoritative. Provider health, quota, availability, scarcity, agent sessions, and the ResourceLedger may influence scheduling but cannot declare project completion or erase work.

## D-003 — Evidence remains typed and sanitized

Provider and execution failures use closed enums, safe numeric metadata, and bounded allowlisted details. Raw headers, bodies, exception payloads, credentials, prompts, transcripts, tool parameters, and arbitrary provider text are not persisted.

## D-004 — Health is capability- and task-class-specific

Scheduling evidence is keyed by resource/provider, model, and task class. Failure streaks are isolated by family. Quota/rate/credit never becomes a health streak, and a small probe proves only small reasoning.

## D-005 — Completed work identity is content-aware

Read deduplication uses normalized path, worker-observed content SHA-256, and workspace/source generation. Agentic work uses durable work IDs/signatures and verified artifact hashes. Backend changes never authorize repetition.

## D-006 — External sessions are disposable continuation aids

Antigravity conversation IDs and Codex thread UUIDs are resumable only when exact source, workspace fingerprint, adapter identity, checkpoint, and session contracts match. Otherwise they are archived/superseded and a fresh session receives the current ContextPack.

## D-007 — Agentic backends are not planner providers

Antigravity and Codex are `AGENTIC_EXECUTION_BACKEND` resources with `SUBSCRIPTION_INCLUDED` cost. Codex is never added to `_PROVIDER_FACTORIES`; API-key fallback and ambiguous `--last` resume are forbidden.

## D-008 — Optional resources degrade out

Qwen, Jev, FreeLLMAPI, Antigravity, and Codex may become unavailable without failing the project. Qwen is bounded by measured capability; Jev is advisory only and cannot authorize production, payment, destruction, security overrides, or completion.

## D-009 — Recovery is bounded in the same lineage

Same-batch/no-progress recovery gets one normal resume, one strategy/backend/objective escalation, then a durable hold. It never creates a replacement project command merely to escape churn.

## D-010 — Production and spend stay closed

Production is `NO_GO`, Council is `SHADOW_ONLY`, paid API fallback is disabled, and all paid/production/destructive authority remains explicitly human-gated.
