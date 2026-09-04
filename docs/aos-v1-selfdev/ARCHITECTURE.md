# AOS Autonomy Fabric + Design Intelligence V1 Architecture

AUTHORITY_ID: `AOS-SELFDEV-AUTONOMY-DESIGN-INTELLIGENCE-V1-20260904-02`

## System Overview

The AOS Autonomy Fabric and Design Intelligence V1 system is an additive extension architecture for the Autonomous Operating System (AOS). It provides multi-run supervision, machine-readable CLI conversation integration, task DAG orchestration, human/authority decision routing, pre-presentation controller review, evidence aggregation, multi-PC worker management, and a comprehensive Design Intelligence pipeline.

```
+-----------------------------------------------------------------------------------+
|                                   AOS RUNTIME                                     |
+-----------------------------------------------------------------------------------+
                                          |
          +-------------------------------+-------------------------------+
          |                                                               |
          v                                                               v
+-----------------------------------+           +-----------------------------------+
|      EXTENSIONS/AUTONOMY-FABRIC    |           |   EXTENSIONS/DESIGN-INTELLIGENCE  |
|                                   |           |                                   |
| - Agent Run Registry (R1)         |           | - Versioned Contracts (R10)       |
| - Antigravity CLI Adapter (R2)    |           | - Reference Intelligence (R11)    |
| - Parallel Supervisor (R3)        |           | - Design DNA & Product Story (R12)|
| - Task DAG & Work Graph (R4)      | --------> | - 7-Critic Ensemble (R13)         |
| - Authority Decision Router (R5)  |           | - Visual QA & Multi-Viewport (R14)|
| - Completion Supervisor (R6)      |           | - Taste Memory & Feedback (R15)   |
| - Evidence Aggregator (R7)        |           | - Tool & Video Primitives (R16)   |
| - Worker & Device Registry (R8)   |           | - Autonomous Design Loop (R17)    |
+-----------------------------------+           +-----------------------------------+
```

## Key Architectural Principles

1. **Absolute Core Freeze**: Core AOS runtime files (`src/aos/*`, `schemas/*`, etc. existing at base SHA `7c4c75e32c0d7c43fc071b0eb872b2b73fdd3c1e`) are 100% frozen. All new capabilities are implemented in authorized extension prefixes.
2. **Offline & Deterministic Execution**: Default tests and benchmarks run offline without live credentials, third-party APIs, or live services. Fake adapters are provided for deterministic testing.
3. **Fail-Closed State Machine**: Explicit run state machine rejecting invalid transitions with immutable append-only event journals.
4. **Resumable Execution Identity**: Runs and Antigravity conversations use explicit persistent IDs rather than UI scrape context or temporary state.
5. **Strict Governance & Anti-Impersonation**: AOS autonomously resolves technical defects and quality gate failures up to bounded revision limits, while requiring explicit human authorization for product acceptance and business decisions.
