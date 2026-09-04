"""Process Restart & Durable Journal Recovery Unit Tests (Correction R1)."""

import pytest
import os
import tempfile
from extensions.autonomy_fabric.run_registry import (
    AgentRunRegistry,
    FileRunJournal,
    RunStatus,
    JournalCorruptRecordError,
)
from extensions.autonomy_fabric.antigravity_adapter import FakeAntigravityAdapter, AntigravityStatus
from extensions.autonomy_fabric.supervisor import ParallelSupervisor


def test_durable_journal_process_restart_recovery():
    with tempfile.TemporaryDirectory() as tmpdir:
        journal_file = os.path.join(tmpdir, "runs.jsonl")

        # Process A: Initialize registry A with FileRunJournal
        journal_a = FileRunJournal(journal_file)
        registry_a = AgentRunRegistry(journal=journal_a)
        adapter_a = FakeAntigravityAdapter()
        adapter_a.default_status = AntigravityStatus.WAITING
        supervisor_a = ParallelSupervisor(registry_a, adapter_a)

        run_a = supervisor_a.launch_run(
            project_id="p-restart",
            run_type="SELF_DEV",
            authority_id="AUTH-RESTART-100",
            controller_id="ctrl-process-a",
            agent_provider="antigravity",
            workspace_path="/ws/restart_test",
            branch="feature/restart-test",
            initial_prompt="Analyze durable recovery",
        )

        registry_a.update_run_metadata(run_a.run_id, {"agent_conversation_id": "conv-durable-777"})

        run_id = run_a.run_id
        conversation_id = run_a.agent_conversation_id
        workspace_path = run_a.workspace_path
        branch = run_a.branch
        authority_id = run_a.authority_id

        # Discard Process A objects completely
        del supervisor_a
        del registry_a
        del journal_a

        # Process B: Create NEW process / registry instance from durable file journal
        journal_b = FileRunJournal(journal_file)
        registry_b = AgentRunRegistry(journal=journal_b)
        supervisor_b = ParallelSupervisor(registry_b, FakeAntigravityAdapter())

        # Recover exact run without receiving original in-memory object
        recovered_count = supervisor_b.recover_from_crash()
        assert recovered_count == 1

        recovered_run = registry_b.get_run(run_id)
        assert recovered_run is not None
        assert recovered_run.run_id == run_id
        assert recovered_run.agent_conversation_id == "conv-durable-777"
        assert recovered_run.status == RunStatus.WAITING_AGENT
        assert recovered_run.authority_id == authority_id
        assert recovered_run.workspace_path == workspace_path
        assert recovered_run.branch == branch
        assert supervisor_b.workspace_locks[workspace_path] == run_id
        assert supervisor_b.branch_locks[branch] == run_id


def test_corrupt_journal_record_fails_closed():
    with tempfile.TemporaryDirectory() as tmpdir:
        journal_file = os.path.join(tmpdir, "corrupt.jsonl")
        with open(journal_file, "w") as f:
            f.write('{"event_id": "e1", "run_id": "r1"}\n')
            f.write('TRUNCATED_INVALID_JSON_RECORD_HERE\n')

        with pytest.raises(JournalCorruptRecordError):
            FileRunJournal(journal_file)
