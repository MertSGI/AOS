"""Integration tests for AOS V2 durable execution engine."""

import json
import subprocess
from pathlib import Path

import pytest

from aos.v2.acceptance import OracleCriterion
from aos.v2.bridge import compute_file_sha256
from aos.v2.engine import (
    Engine,
    EngineError,
    ExecutionPlan,
    PlanValidationError,
    TaskNode,
)
from aos.v2.lifecycle import OwnerBusyError
from aos.v2.registry import authorize, load_registry
from aos.v2.resources import ResourceBudgetExceededError, ResourceGateway
from aos.v2.sandbox import get_available_ram_bytes


@pytest.fixture(autouse=True)
def mock_adequate_ram(monkeypatch):
    orig = get_available_ram_bytes()
    monkeypatch.setattr("aos.v2.sandbox.get_available_ram_bytes", lambda: max(orig, 2 * 1024 * 1024 * 1024))


@pytest.fixture
def disposable_project(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "AOS Tester"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@aos.local"], cwd=repo_dir, check=True, capture_output=True)

    src_dir = repo_dir / "src"
    src_dir.mkdir()
    (src_dir / "discount.py").write_text(
        "def apply_discount(price, rate):\n    return 0.0  # faulty initial\n"
    )
    (src_dir / "tax.py").write_text(
        "def apply_tax(subtotal, rate):\n    return 0.0  # faulty initial\n"
    )

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)

    owned_branch = "feature/v2-eval"
    subprocess.run(["git", "checkout", "-b", owned_branch], cwd=repo_dir, check=True, capture_output=True)
    head_res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True)
    base_sha = head_res.stdout.strip()

    # Setup Evaluator Oracles OUTSIDE repo
    oracle_dir = tmp_path / "evaluator"
    oracle_dir.mkdir()

    # 1. Discount oracle
    discount_cases = [
        {"case_id": f"d_{i}", "args": [float(i * 10), 0.1], "expected": float(i * 10) * 0.9}
        for i in range(1, 15)
    ]
    discount_oracle_file = oracle_dir / "discount_oracle.json"
    discount_oracle_file.write_text(
        json.dumps({"cases": discount_cases}, indent=2), encoding="utf-8"
    )
    discount_oracle_sha = compute_file_sha256(discount_oracle_file)

    # 2. Tax oracle
    tax_cases = [
        {"case_id": f"t_{i}", "args": [float(i * 10), 0.2], "expected": float(i * 10) * 1.2}
        for i in range(1, 15)
    ]
    tax_oracle_file = oracle_dir / "tax_oracle.json"
    tax_oracle_file.write_text(
        json.dumps({"cases": tax_cases}, indent=2), encoding="utf-8"
    )
    tax_oracle_sha = compute_file_sha256(tax_oracle_file)

    # Registry file
    registry_file = tmp_path / "server_registry.json"
    registry_data = {
        "schema_version": 1,
        "projects": [
            {
                "project_id": "DISPOSABLE_PROJ",
                "authority_id": "AUTH_1",
                "enabled": True,
                "classification": "DISPOSABLE_SOURCE",
                "workspace": str(repo_dir),
                "base_sha": base_sha,
                "write_files": ["src/discount.py", "src/tax.py"],
                "cost_mode": "ZERO_PAID",
                "production": "NO_GO",
                "criteria": [
                    {"id": "CRIT_DISCOUNT", "kind": "FUNCTION_ORACLE_V1"},
                    {"id": "CRIT_TAX", "kind": "FUNCTION_ORACLE_V1"},
                    {"id": "C1", "kind": "FUNCTION_ORACLE_V1"},
                ],
            }
        ],
        "resources": [],
    }
    registry_file.write_text(json.dumps(registry_data, indent=2), encoding="utf-8")

    return {
        "repo_dir": repo_dir,
        "owned_branch": owned_branch,
        "registry_file": registry_file,
        "discount_oracle": (discount_oracle_file, discount_oracle_sha),
        "tax_oracle": (tax_oracle_file, tax_oracle_sha),
    }


def test_engine_owner_lock_and_competing_process_denial(disposable_project, tmp_path):
    v2_home = tmp_path / "v2_home"
    reg_file = disposable_project["registry_file"]

    with Engine(v2_home, reg_file) as engine1:
        # Second engine on same v2_home must raise OwnerBusyError
        with pytest.raises(OwnerBusyError, match="OWNER_BUSY"):
            with Engine(v2_home, reg_file) as engine2:
                pass


def test_plan_validation_denies_empty_and_cyclic_dag(disposable_project, tmp_path):
    reg_file = disposable_project["registry_file"]
    snap = load_registry(reg_file)
    project = authorize(snap, "DISPOSABLE_PROJ")

    # Empty plan
    empty_plan = ExecutionPlan("plan_empty", [])
    with pytest.raises(PlanValidationError, match="empty"):
        empty_plan.validate(project)

    # Cyclic plan: task_a -> task_b -> task_a
    d_file, d_sha = disposable_project["discount_oracle"]
    crit = OracleCriterion("C1", "discount", "apply_discount", d_file, d_sha)
    cyclic_plan = ExecutionPlan(
        "plan_cyclic",
        [
            TaskNode("task_a", "src/discount.py", crit, depends_on=["task_b"]),
            TaskNode("task_b", "src/discount.py", crit, depends_on=["task_a"]),
        ],
    )
    with pytest.raises(PlanValidationError, match="Cyclic dependency"):
        cyclic_plan.validate(project)


def test_engine_executes_two_step_dag_to_terminal_closed(disposable_project, tmp_path):
    v2_home = tmp_path / "v2_home"
    reg_file = disposable_project["registry_file"]

    d_file, d_sha = disposable_project["discount_oracle"]
    t_file, t_sha = disposable_project["tax_oracle"]

    crit_discount = OracleCriterion("CRIT_DISCOUNT", "discount", "apply_discount", d_file, d_sha)
    crit_tax = OracleCriterion("CRIT_TAX", "tax", "apply_tax", t_file, t_sha)

    # Two-step DAG: Tax depends on Discount
    plan = ExecutionPlan(
        "plan_repair_pricing",
        [
            TaskNode(
                "task_repair_discount",
                "src/discount.py",
                crit_discount,
                description="Fix percentage discount formula",
            ),
            TaskNode(
                "task_repair_tax",
                "src/tax.py",
                crit_tax,
                depends_on=["task_repair_discount"],
                description="Fix percentage tax formula",
            ),
        ],
    )

    # Simulated model generator returning exact edits
    def simulated_generator(task_node: TaskNode, pre_sha: str, repo_root: Path):
        if task_node.task_id == "task_repair_discount":
            return [{"old": "return 0.0  # faulty initial", "new": "return price * (1.0 - rate)"}]
        elif task_node.task_id == "task_repair_tax":
            return [{"old": "return 0.0  # faulty initial", "new": "return subtotal * (1.0 + rate)"}]
        return ()

    with Engine(v2_home, reg_file) as engine:
        cmd = engine.execute_goal(
            project_id="DISPOSABLE_PROJ",
            goal="Repair discount and tax modules",
            plan=plan,
            idempotency_key="idemp_goal_1",
            generator_func=simulated_generator,
        )

        assert cmd["state"] == "PROJECT_COMPLETE"
        assert cmd["admission"] == "CLOSED"

        # Check git branch has 2 new commits
        repo_root = disposable_project["repo_dir"]
        log_res = subprocess.run(
            ["git", "log", "--oneline", "-n", "3"],
            cwd=repo_root, capture_output=True, text=True, check=True
        )
        assert "AOS V2: Fix percentage tax formula" in log_res.stdout
        assert "AOS V2: Fix percentage discount formula" in log_res.stdout

        # Check source files on disk
        assert (repo_root / "src" / "discount.py").read_text() == "def apply_discount(price, rate):\n    return price * (1.0 - rate)\n"
        assert (repo_root / "src" / "tax.py").read_text() == "def apply_tax(subtotal, rate):\n    return subtotal * (1.0 + rate)\n"

        # Check outbox result.json
        result_json = v2_home / "commands" / cmd["id"] / "result.json"
        assert result_json.is_file()
        res_data = json.loads(result_json.read_text())
        assert res_data["state"] == "PROJECT_COMPLETE"
        assert res_data["admission"] == "CLOSED"

        # Check isolated KnowledgeLedger
        events = engine.outbox.ledger.read_events()
        assert len(events) == 2  # implementation and verification receipts


def test_engine_idempotent_replay(disposable_project, tmp_path):
    v2_home = tmp_path / "v2_home"
    reg_file = disposable_project["registry_file"]

    d_file, d_sha = disposable_project["discount_oracle"]
    crit_discount = OracleCriterion("CRIT_DISCOUNT", "discount", "apply_discount", d_file, d_sha)
    plan = ExecutionPlan(
        "plan_single",
        [TaskNode("task_discount", "src/discount.py", crit_discount)],
    )

    def simulated_generator(task_node: TaskNode, pre_sha: str, repo_root: Path):
        return [{"old": "return 0.0  # faulty initial", "new": "return price * (1.0 - rate)"}]

    with Engine(v2_home, reg_file) as engine:
        cmd1 = engine.execute_goal(
            project_id="DISPOSABLE_PROJ",
            goal="Repair discount",
            plan=plan,
            idempotency_key="idemp_repeat",
            generator_func=simulated_generator,
        )

        # Re-running with same idempotency key must return same command immediately
        cmd2 = engine.execute_goal(
            project_id="DISPOSABLE_PROJ",
            goal="Repair discount",
            plan=plan,
            idempotency_key="idemp_repeat",
            generator_func=simulated_generator,
        )
        assert cmd1["id"] == cmd2["id"]
        assert cmd2["state"] == "PROJECT_COMPLETE"
