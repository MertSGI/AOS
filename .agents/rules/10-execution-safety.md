# Execution Safety Rule

Execution in AOS must be strictly bounded, isolated, and safety-checked.

## Safety Directives
1. **Branch and Worktree Isolation**: All mutations must occur on dedicated, isolated task branches (e.g., `feature/*`, `aos/*`). Direct work on `main` is prohibited.
2. **Path Scope Enforcement**: Changes must remain strictly within the declared `allowed_scope` of the assigned task. Never modify unrelated files or repositories (including LARÝ product code unless explicitly tasked).
3. **No Secrets in Code or Control Plane**: Never write API keys, access tokens, credentials, or private keys to repository files or commit history.
4. **Human Authority Boundaries**: Never autonomously execute production deployments, destructive database operations, payment activations, security boundary changes, or roadmap alterations.
5. **Disposable Coordination**: Treat operational states (leases, heartbeats, queues) as transient. Never confuse ephemeral operational state with canonical Git truth.
6. **Bounded Retries**: Never exceed task retry limits. Stop and escalate to HOLD if fixes do not resolve issues deterministically.