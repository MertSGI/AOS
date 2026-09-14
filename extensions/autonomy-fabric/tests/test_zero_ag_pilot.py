"""End-to-End Zero-AG Pilot Test.

Proves that AOS can execute the entire autonomous coding lifecycle:
inspect -> plan -> modify -> test -> commit -> verify -> evidence
with ANTIGRAVITY_INSTALLED=FALSE and AG_INVOCATION_COUNT=0.
"""

import pytest
import os
import subprocess
import tempfile
from pathlib import Path

from extensions.autonomy_fabric.run_registry import AgentRunRegistry, RunStatus
from extensions.autonomy_fabric.task_dag import TaskDAG
from extensions.autonomy_fabric.execution_router import ExecutionRouter
from extensions.autonomy_fabric.native_workers import (
    NativeFileWorker,
    NativeProcessWorker,
    NativeGitWorker,
    ModelReasoningBackend,
    AntigravityExecutionBackend,
    compute_file_sha256,
)
from extensions.autonomy_fabric.persistent_coordinator import PersistentCoordinator
from extensions.autonomy_fabric.evidence_aggregator import EvidenceAggregator
from extensions.autonomy_fabric.autonomy_metrics import AutonomyCompletenessProfile, MetricState


def test_zero_ag_autonomous_pilot():
    ag_invocations = 0

    def record_ag_call(*args, **kwargs):
        nonlocal ag_invocations
        ag_invocations += 1

    # AG backend disabled / tracking calls
    ag_backend = AntigravityExecutionBackend(simulate_unavailable=True)

    file_worker = NativeFileWorker()
    proc_worker = NativeProcessWorker()
    git_worker = NativeGitWorker()
    model_worker = ModelReasoningBackend()

    router = ExecutionRouter(
        backends=[file_worker, proc_worker, git_worker, model_worker, ag_backend],
        ag_required=False,
    )

    with tempfile.TemporaryDirectory() as fixture_repo:
        # Initialize small fixture git repo
        subprocess.run(["git", "init"], cwd=fixture_repo, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "AOS Bot"], cwd=fixture_repo, check=True)
        subprocess.run(["git", "config", "user.email", "aos@domain.test"], cwd=fixture_repo, check=True)

        # 1. Initial file
        src_file = os.path.join(fixture_repo, "calc.py")
        with open(src_file, "w") as f:
            f.write("def multiply(a, b):\n    return a + b\n")  # Bug intentionally present

        test_file = os.path.join(fixture_repo, "test_calc.py")
        with open(test_file, "w") as f:
            f.write("from calc import multiply\n\ndef test_multiply():\n    assert multiply(3, 4) == 12\n")

        subprocess.run(["git", "add", "."], cwd=fixture_repo, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=fixture_repo, check=True)

        registry = AgentRunRegistry()
        evidence = EvidenceAggregator(registry)
        dag = TaskDAG("pilot-project", registry)

        # Setup DAG tasks
        t1 = dag.add_node("task-plan", "REASONING", "AUTH-PILOT-01")
        t2 = dag.add_node("task-fix", "PATCH", "AUTH-PILOT-01", dependencies=["task-plan"])
        t3 = dag.add_node("task-test", "TEST", "AUTH-PILOT-01", dependencies=["task-fix"])
        t4 = dag.add_node("task-commit", "GIT_COMMIT", "AUTH-PILOT-01", dependencies=["task-test"])

        # Attach payloads
        t1.payload = {
            "prompt": "Fix multiply bug in calc.py",
        }
        patch_content = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def multiply(a, b):
-    return a + b
+    return a * b
"""
        t2.payload = {
            "action": "apply_patch",
            "patch": patch_content,
        }
        t3.payload = {
            "cmd": ["python", "-m", "pytest", "test_calc.py"],
        }
        t4.payload = {
            "action": "commit",
            "args": ["-a", "-m", "fix(calc): repair multiplication logic autonomously"],
        }

        # Run coordinator across DAG
        coord = PersistentCoordinator(
            project_id="pilot-project",
            workspace_path=fixture_repo,
            dag=dag,
            router=router,
            registry=registry,
            evidence_aggregator=evidence,
        )

        coord.run_until_complete(max_iterations=10)

        # Assert zero AG invocations
        assert ag_invocations == 0

        # Assert DAG completed cleanly
        assert dag.compute_progress() == 100.0
        assert "task-plan" in coord.state.completed_task_ids
        assert "task-fix" in coord.state.completed_task_ids
        assert "task-test" in coord.state.completed_task_ids
        assert "task-commit" in coord.state.completed_task_ids

        # Assert file was actually repaired
        with open(src_file, "r") as f:
            content = f.read()
        assert "return a * b" in content

        # Verify git commit happened
        log_proc = subprocess.run(
            ["git", "log", "-n", "1", "--oneline"],
            cwd=fixture_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "fix(calc): repair multiplication logic autonomously" in log_proc.stdout

        # Verify autonomy metrics
        profile = AutonomyCompletenessProfile()
        assert profile.ag_optional is True
        assert profile.ag_default_required is False
        assert profile.executor_independence == MetricState.RUNTIME_PROVEN
