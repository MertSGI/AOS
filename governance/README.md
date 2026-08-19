# AOS Global Governance Directory

This directory defines the global, cross-project governance assets for the Agent Operating System (AOS).

## Purpose

The `governance/` directory houses reusable rules, policies, and promoted design patterns applicable across all projects managed by AOS.

## What Belongs Here

- Cross-project governance principles and policies (Layer A Memory).
- Promoted, universally applicable lessons learned across multiple project deployments (promoted via explicit review).
- Machine-readable governance definitions and validation assets that transcend any single project.

## What Does NOT Belong Here

- **Canonical Project Control**: Canonical project control (roadmaps, charters, state, decisions, evidence) for a specific project lives in `docs/project-control/` or `<project>/.aos/`.
- **Project-Specific Logic**: Project-specific rules, product charters, or business policies remain inside the respective project repositories (`Layer B Memory`).
- **Execution Projections**: Tool/agent-specific execution rules (e.g., `.agents/rules/`) are execution projections and adapters, not canonical global governance.
- **Operational / Transient State**: Worker leases, heartbeats, task queues, or runtime operational state do NOT belong in version control.
- **Secrets and Credentials**: API keys, tokens, passwords, private keys, or sensitive configuration must never be committed here or anywhere in source control.

## Governance Authority & Change Control

- Canonical authority resides in accepted project-control documents (`docs/project-control/` or `.aos/`).
- Global governance assets are reusable across all projects.
- Project-specific rules must not silently become global policy. Lessons require explicit promotion before becoming global policy (see `AOS-DEC-015`).
- Amendments to global governance require explicit architectural or governance decision records (see `Master Charter §19`).

## References

- `docs/project-control/CHARTER.md` (Master Project Charter & Memory Model §6)
- `docs/project-control/DECISIONS.md` (`AOS-DEC-015 — Reusable lessons require promotion`)
