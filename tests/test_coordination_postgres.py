"""Tests for AOS-5 Stage 11C PostgreSQL Coordination Backend.

Contains:
A. Offline/source-contract unit tests (Protocol structural validation, secret safety, DSN validation, timeout validation, corruption matrix, generation overflow)
B. Real PostgreSQL integration tests (marked with @pytest.mark.postgres_integration, executing when AOS_POSTGRES_TEST_DSN is present)
"""

import os
from pathlib import Path
import pytest

from aos.coordination import (
    ClaimDisposition,
    CoordinationBackend,
    CoordinationStorageError,
    InvalidIdentityError,
    InvalidInputError,
    LeaseStatus,
    WorkerIdentity,
)
from aos.coordination_postgres import (
    BIGINT_MAX,
    POSTGRES_SCHEMA_VERSION,
    PostgresCoordinationBackend,
    _parse_worker_row,
    _sanitize_dsn_for_logging,
)

# Environment variable for real PostgreSQL CI service DSN
POSTGRES_TEST_DSN = os.getenv("AOS_POSTGRES_TEST_DSN")


# ==============================================================================
# A. OFFLINE / SOURCE-CONTRACT UNIT TESTS
# ==============================================================================

def test_dsn_sanitization_scrubs_passwords():
    dsn = "postgresql://user:super_secret_pass@localhost:5432/mydb"
    sanitized = _sanitize_dsn_for_logging(dsn)
    assert "super_secret_pass" not in sanitized
    assert "*****" in sanitized


def test_invalid_dsn_or_namespace_fails_closed():
    with pytest.raises(InvalidInputError):
        PostgresCoordinationBackend(dsn="", namespace_id="ns1")

    with pytest.raises(InvalidInputError):
        PostgresCoordinationBackend(dsn="postgresql://user:pass@localhost/db", namespace_id="   ")


def test_invalid_timeouts_fail_closed():
    dsn = "postgresql://user:pass@localhost/db"
    with pytest.raises(InvalidInputError):
        PostgresCoordinationBackend(dsn=dsn, namespace_id="ns1", lock_timeout_ms=-100)

    with pytest.raises(InvalidInputError):
        PostgresCoordinationBackend(dsn=dsn, namespace_id="ns1", statement_timeout_ms=100000)


def test_psycopg_import_error_raised_as_storage_error():
    dsn = "postgresql://user:pass@localhost/db"

    def mock_factory(d):
        raise CoordinationStorageError("Failed to connect to PostgreSQL")

    with pytest.raises(CoordinationStorageError) as exc_info:
        PostgresCoordinationBackend(dsn=dsn, namespace_id="ns1", connect_factory=mock_factory)

    assert "super_secret_pass" not in str(exc_info.value)


def test_no_external_dependencies_or_git_mutation_in_postgres_backend():
    import aos.coordination_postgres as pg_coord

    with open(pg_coord.__file__, "r", encoding="utf-8") as f:
        content = f.read()

    forbidden = ["git", "supabase", "requests", "urllib", "lari", "antigravity"]
    for word in forbidden:
        assert f"import {word}" not in content.lower()
        assert f"from {word}" not in content.lower()


def test_corrupt_worker_row_parsing_fails_closed():
    # Incorrect column count
    with pytest.raises(CoordinationStorageError):
        _parse_worker_row(("ns1", "w1", "s1"))

    # Empty worker_id
    with pytest.raises(CoordinationStorageError):
        _parse_worker_row(("ns1", "", "s1", "[]", "2026-08-28T12:00:00+00:00"))

    # Malformed capability tags JSON
    with pytest.raises(CoordinationStorageError):
        _parse_worker_row(("ns1", "w1", "s1", "invalid-json", "2026-08-28T12:00:00+00:00"))

    # Naive registered_at
    with pytest.raises(CoordinationStorageError):
        _parse_worker_row(("ns1", "w1", "s1", "[]", "2026-08-28T12:00:00"))


# ==============================================================================
# B. REAL POSTGRESQL INTEGRATION TESTS
# ==============================================================================

@pytest.mark.postgres_integration
@pytest.mark.skipif(not POSTGRES_TEST_DSN, reason="AOS_POSTGRES_TEST_DSN not set; skipping live PostgreSQL integration tests")
class TestPostgresCoordinationIntegration:

    @pytest.fixture(autouse=True)
    def setup_database_schema(self):
        """Apply migration schema to real PostgreSQL test service before tests."""
        import psycopg
        conn = None
        dsns_to_try = [
            POSTGRES_TEST_DSN,
            "postgresql://aos:aos_ci_test@127.0.0.1:5432/aos_coordination_test",
            "postgresql://aos:aos_ci_test@localhost:5432/aos_coordination_test",
        ]
        for dsn in dsns_to_try:
            if not dsn:
                continue
            try:
                conn = psycopg.connect(dsn, autocommit=True)
                break
            except Exception:
                pass

        if conn is None:
            raise RuntimeError(f"Could not connect to PostgreSQL using DSN {POSTGRES_TEST_DSN}")
        
        # Read and execute migration file
        migration_sql_path = Path(__file__).parent.parent / "sql" / "aos5_coordination_postgres_v0_1_0.sql"
        with open(migration_sql_path, "r", encoding="utf-8") as f:
            sql_script = f.read()

        with conn.cursor() as cur:
            # Drop prior tables for test isolation
            cur.execute("DROP TABLE IF EXISTS aos_coordination_leases CASCADE;")
            cur.execute("DROP TABLE IF EXISTS aos_coordination_task_locks CASCADE;")
            cur.execute("DROP TABLE IF EXISTS aos_coordination_workers CASCADE;")
            cur.execute("DROP TABLE IF EXISTS aos_coordination_meta CASCADE;")
            cur.execute(sql_script)
        conn.close()

    def test_schema_validation_and_reopen(self):
        backend1 = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")
        backend2 = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")

    def test_unsupported_schema_version_fails_closed(self):
        import psycopg
        conn = psycopg.connect(POSTGRES_TEST_DSN, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("UPDATE aos_coordination_meta SET value = '9.9.9' WHERE key = 'schema_version';")
        conn.close()

        with pytest.raises(CoordinationStorageError):
            PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")

    def test_missing_required_table_fails_closed_without_repair(self):
        import psycopg
        conn = psycopg.connect(POSTGRES_TEST_DSN, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("DROP TABLE aos_coordination_leases CASCADE;")
        conn.close()

        with pytest.raises(CoordinationStorageError):
            PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")

    def test_wrong_primary_key_structure_fails_closed(self):
        import psycopg
        conn = psycopg.connect(POSTGRES_TEST_DSN, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE aos_coordination_leases DROP CONSTRAINT aos_coordination_leases_pkey;")
            cur.execute("ALTER TABLE aos_coordination_leases ADD PRIMARY KEY (task_id);")
        conn.close()

        with pytest.raises(CoordinationStorageError):
            PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")

    def test_worker_registration_and_persistence(self):
        backend1 = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")
        w1 = WorkerIdentity("w1", "s1", ["tag1", "tag2"])
        backend1.register_worker(w1)

        backend2 = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")
        assert backend2.is_worker_registered("w1", "s1")

    def test_contradictory_worker_registration_fails(self):
        backend = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")
        w1 = WorkerIdentity("w1", "s1", ["tag1"])
        backend.register_worker(w1)

        w1_bad = WorkerIdentity("w1", "s1", ["tag2"])
        with pytest.raises(InvalidIdentityError):
            backend.register_worker(w1_bad)

    def test_namespace_isolation(self):
        b1 = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns_a")
        b2 = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns_b")

        w1 = WorkerIdentity("w1", "s1")
        w2 = WorkerIdentity("w2", "s2")
        b1.register_worker(w1)
        b2.register_worker(w2)

        res1 = b1.try_claim("shared-task", w1, 60.0)
        res2 = b2.try_claim("shared-task", w2, 60.0)

        assert res1.disposition == ClaimDisposition.ACQUIRED
        assert res2.disposition == ClaimDisposition.ACQUIRED

    def test_claim_already_owned_and_held_by_other(self):
        backend = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")
        w1 = WorkerIdentity("w1", "s1")
        w2 = WorkerIdentity("w2", "s2")
        backend.register_worker(w1)
        backend.register_worker(w2)

        res1 = backend.try_claim("task-1", w1, 60.0)
        assert res1.disposition == ClaimDisposition.ACQUIRED

        # Same owner reclaim -> ALREADY_OWNED
        res1_reclaim = backend.try_claim("task-1", w1, 60.0)
        assert res1_reclaim.disposition == ClaimDisposition.ALREADY_OWNED
        assert res1_reclaim.lease == res1.lease

        # Other owner claim -> HELD_BY_OTHER
        res2 = backend.try_claim("task-1", w2, 60.0)
        assert res2.disposition == ClaimDisposition.HELD_BY_OTHER
        assert res2.lease == res1.lease

    def test_heartbeat_and_expiry_recovery(self):
        backend = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")
        w1 = WorkerIdentity("w1", "s1")
        w2 = WorkerIdentity("w2", "s2")
        backend.register_worker(w1)
        backend.register_worker(w2)

        res1 = backend.try_claim("task-1", w1, 60.0)
        hb = backend.heartbeat("task-1", "w1", "s1", res1.lease.lease_id, res1.lease.generation)
        assert hb is not None
        assert hb.last_heartbeat_at >= res1.lease.last_heartbeat_at

        # Manually set lease to expired in DB
        import psycopg
        conn = psycopg.connect(POSTGRES_TEST_DSN, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE aos_coordination_leases SET expires_at = '2000-01-01T00:00:00Z' WHERE namespace_id = 'ns1' AND task_id = 'task-1';"
            )
        conn.close()

        # Recovery claim by w2
        res2 = backend.try_claim("task-1", w2, 60.0)
        assert res2.disposition == ClaimDisposition.ACQUIRED
        assert res2.lease.worker_id == "w2"
        assert res2.lease.generation == res1.lease.generation + 1
        assert res2.lease.lease_id != res1.lease.lease_id

        # Stale heartbeat/release rejected
        assert backend.heartbeat("task-1", "w1", "s1", res1.lease.lease_id, res1.lease.generation) is None
        assert backend.release("task-1", "w1", "s1", res1.lease.lease_id, res1.lease.generation) is False

    def test_release_and_new_epoch(self):
        backend = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")
        w1 = WorkerIdentity("w1", "s1")
        w2 = WorkerIdentity("w2", "s2")
        backend.register_worker(w1)
        backend.register_worker(w2)

        res1 = backend.try_claim("task-1", w1, 60.0)
        assert backend.release("task-1", "w1", "s1", res1.lease.lease_id, res1.lease.generation) is True

        assert backend.get_lease("task-1").status == LeaseStatus.RELEASED

        res2 = backend.try_claim("task-1", w2, 60.0)
        assert res2.disposition == ClaimDisposition.ACQUIRED
        assert res2.lease.generation == res1.lease.generation + 1

    def test_corrupt_persisted_lease_fails_closed(self):
        backend = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")
        w1 = WorkerIdentity("w1", "s1")
        backend.register_worker(w1)
        backend.try_claim("task-1", w1, 60.0)

        import psycopg
        conn = psycopg.connect(POSTGRES_TEST_DSN, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE aos_coordination_leases SET ttl_seconds = -10.0 WHERE namespace_id = 'ns1' AND task_id = 'task-1';"
            )
        conn.close()

        with pytest.raises(CoordinationStorageError):
            backend.get_lease("task-1")

    def test_corrupt_persisted_worker_registered_at_fails_closed(self):
        backend = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")
        w1 = WorkerIdentity("w1", "s1")
        backend.register_worker(w1)

        import psycopg
        conn = psycopg.connect(POSTGRES_TEST_DSN, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE aos_coordination_workers SET capability_tags = '{\"invalid\": \"shape\"}' WHERE namespace_id = 'ns1' AND worker_id = 'w1';"
            )
        conn.close()

        with pytest.raises(CoordinationStorageError):
            backend.is_worker_registered("w1", "s1")

    def test_generation_overflow_fails_closed(self):
        backend = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")
        w1 = WorkerIdentity("w1", "s1")
        w2 = WorkerIdentity("w2", "s2")
        backend.register_worker(w1)
        backend.register_worker(w2)

        res1 = backend.try_claim("task-1", w1, 60.0)

        import psycopg
        conn = psycopg.connect(POSTGRES_TEST_DSN, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE aos_coordination_leases SET generation = %s, expires_at = '2000-01-01T00:00:00Z' WHERE namespace_id = 'ns1' AND task_id = 'task-1';",
                (BIGINT_MAX,),
            )
        conn.close()

        with pytest.raises(CoordinationStorageError):
            backend.try_claim("task-1", w2, 60.0)

    def test_two_instance_concurrent_claim_exact_winner_binding(self):
        import threading
        b1 = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")
        b2 = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")

        w1 = WorkerIdentity("w1", "s1")
        w2 = WorkerIdentity("w2", "s2")
        b1.register_worker(w1)
        b2.register_worker(w2)

        barrier = threading.Barrier(2)
        results = [None, None]
        exceptions = [None, None]

        def t1_task():
            try:
                barrier.wait()
                results[0] = b1.try_claim("concurrent-task", w1, 60.0)
            except Exception as e:
                exceptions[0] = e

        def t2_task():
            try:
                barrier.wait()
                results[1] = b2.try_claim("concurrent-task", w2, 60.0)
            except Exception as e:
                exceptions[1] = e

        th1 = threading.Thread(target=t1_task)
        th2 = threading.Thread(target=t2_task)
        th1.start()
        th2.start()
        th1.join()
        th2.join()

        assert exceptions == [None, None]

        dispositions = [results[0].disposition, results[1].disposition]
        assert dispositions.count(ClaimDisposition.ACQUIRED) == 1
        assert dispositions.count(ClaimDisposition.HELD_BY_OTHER) == 1

        winner_res = results[0] if results[0].disposition == ClaimDisposition.ACQUIRED else results[1]
        loser_res = results[1] if results[0].disposition == ClaimDisposition.ACQUIRED else results[0]

        persisted = b1.get_lease("concurrent-task")
        assert persisted == winner_res.lease
        assert loser_res.lease == winner_res.lease

    def test_multi_contender_concurrent_claim(self):
        import threading
        backends = [PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1") for _ in range(10)]
        workers = [WorkerIdentity(f"w_{i}", f"s_{i}") for i in range(10)]

        for i in range(10):
            backends[i].register_worker(workers[i])

        barrier = threading.Barrier(10)
        results = [None] * 10
        exceptions = [None] * 10

        def worker_task(idx):
            try:
                barrier.wait()
                results[idx] = backends[idx].try_claim("multi-task", workers[idx], 60.0)
            except Exception as e:
                exceptions[idx] = e

        threads = [threading.Thread(target=worker_task, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(e is None for e in exceptions)

        dispositions = [r.disposition for r in results]
        assert dispositions.count(ClaimDisposition.ACQUIRED) == 1
        assert dispositions.count(ClaimDisposition.HELD_BY_OTHER) == 9

        winner = next(r for r in results if r.disposition == ClaimDisposition.ACQUIRED)
        persisted = backends[0].get_lease("multi-task")

        assert persisted == winner.lease
        for loser in [r for r in results if r.disposition == ClaimDisposition.HELD_BY_OTHER]:
            assert loser.lease == winner.lease

    def test_sql_looking_identifier_treated_as_data(self):
        backend = PostgresCoordinationBackend(POSTGRES_TEST_DSN, namespace_id="ns1")
        sql_id = "task'; DROP TABLE aos_coordination_leases; --"
        w1 = WorkerIdentity("w1'; DELETE FROM aos_coordination_workers; --", "s1")
        backend.register_worker(w1)

        res = backend.try_claim(sql_id, w1, 60.0)
        assert res.disposition == ClaimDisposition.ACQUIRED

        stored = backend.get_lease(sql_id)
        assert stored is not None
        assert stored.task_id == sql_id
        assert stored.worker_id == w1.worker_id
