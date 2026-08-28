-- AOS-5 Distributed Multi-PC Coordination — Stage 11C PostgreSQL Schema Version 0.1.0
-- Non-canonical coordination store tables for PostgreSQL distributed backend.

CREATE TABLE IF NOT EXISTS aos_coordination_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO aos_coordination_meta (key, value)
VALUES ('schema_version', '0.1.0')
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS aos_coordination_workers (
    namespace_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    capability_tags JSONB NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (namespace_id, worker_id, session_id)
);

CREATE TABLE IF NOT EXISTS aos_coordination_task_locks (
    namespace_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    PRIMARY KEY (namespace_id, task_id)
);

CREATE TABLE IF NOT EXISTS aos_coordination_leases (
    namespace_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL,
    last_heartbeat_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    ttl_seconds DOUBLE PRECISION NOT NULL,
    generation BIGINT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'RELEASED', 'EXPIRED')),
    PRIMARY KEY (namespace_id, task_id)
);
