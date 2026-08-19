# AOS Autonomy & Human Gate Policy

**Version:** 0.1.0
**Default posture:** FAIL_CLOSED

## Autonomous Allowed by Default

When project policy and risk class allow:
- read-only repository inspection,
- state/roadmap parsing,
- task decomposition within an approved milestone,
- isolated branch/worktree creation,
- R1 implementation,
- build/lint/unit/integration tests in approved environments,
- non-production browser verification where configured,
- evidence packaging,
- bounded retry/fix cycles,
- draft PR preparation,
- status reporting.

## Human Approval Required by Default

- roadmap change,
- scope expansion that changes product commitment,
- production deployment or mutation,
- production database migration,
- destructive data operation,
- payment/billing activation or commercial-mode change,
- auth/security boundary change,
- secrets/trust ownership change,
- legal/compliance interpretation,
- use of real sensitive customer data beyond existing approved policy,
- overriding verifier failure,
- waiving required evidence,
- lowering a gate’s evidence requirement to force closure.

## Automatic HOLD

HOLD immediately when:
- state/schema invalid,
- base SHA mismatch,
- task lease invalid,
- scope violation,
- required tests unavailable,
- required evidence missing,
- executor claim conflicts with evidence,
- accepted state conflicts with new authoritative evidence,
- retry ceiling reached,
- worker loses required permissions,
- coordination state becomes ambiguous,
- human-required action is encountered.

## Post-HOLD Rule

After HOLD:
- no further product mutation,
- evidence/log preservation only,
- human-readable escalation generated,
- decision resumes from the same canonical state unless an approved state update changes it.

## Prohibited Behavior

AOS/worker must not:
- invent a new roadmap phase,
- mark a gate proven based on narrative confidence,
- fabricate browser/runtime evidence,
- hide failed tests,
- broaden scope to make a task easier,
- silently switch baselines,
- commit secrets,
- use production access because it happens to be available,
- interpret absence of rejection as approval.
