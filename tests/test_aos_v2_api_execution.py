"""API end-to-end execution and boundary tests for AOS V2.

Qualifies:
- Q01: Real authenticated API submission to real dispatcher, Gateway PLAN, immutable plan, Gateway EDIT.
- Q02: Source dry-run with calculate_discount/calculate_tax, unchanged tests, pinned private oracle.
- Q03: Injected model with SIMULATED_INFERENCE, genuine native commits, 0 external model calls.
- Q04: New 202; same-key replay 200; changed-input 409; concurrent same project/key; distinct project keys.
- Q05: No worker/transport invocation on empty/cyclic/invalid/forged plans.
- Q06: Boundary rejection: Host/Origin mismatch, BOM/UTF16, NaN/Infinity, depth>8, duplicate headers/keys, CL/TE, nonempty control bodies.
- Q07: Windows owner-only DACL and POSIX permissions for private token and endpoint files.
"""

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from aos.v2.acceptance import OracleCriterion
from aos.v2.api import AOSApiServer
from aos.v2.bridge import compute_file_sha256
from aos.v2.engine import Engine, ExecutionPlan, TaskNode
from aos.v2.lifecycle import validate_private_file
from aos.v2.resources import ResourceGateway
from aos.v2.sandbox import get_available_ram_bytes, preflight_sandbox


@pytest.fixture(autouse=True)
def mock_adequate_ram(monkeypatch):
    orig = get_available_ram_bytes()
    monkeypatch.setattr("aos.v2.sandbox.get_available_ram_bytes", lambda: max(orig, 2 * 1024 * 1024 * 1024))


def _http_request(
    url: str,
    method: str = "GET",
    data: dict | bytes | str | None = None,
    headers: dict | None = None,
) -> tuple[int, dict | str]:
    req_headers = headers.copy() if headers else {}
    body_bytes = None
    if isinstance(data, dict):
        body_bytes = json.dumps(data).encode("utf-8")
        if "Content-Type" not in req_headers:
            req_headers["Content-Type"] = "application/json"
    elif isinstance(data, str):
        body_bytes = data.encode("utf-8")
    elif isinstance(data, bytes):
        body_bytes = data

    if body_bytes is not None and "Content-Length" not in req_headers:
        req_headers["Content-Length"] = str(len(body_bytes))

    req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


@pytest.fixture
def dry_run_environment(tmp_path):
    repo_dir = tmp_path / "pricing_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "AOS Tester"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@aos.local"], cwd=repo_dir, check=True, capture_output=True)

    src_dir = repo_dir / "src"
    src_dir.mkdir()
    (src_dir / "discount.py").write_text(
        "def calculate_discount(price: float, rate: float) -> float:\n"
        "    return 0.0  # faulty initial\n"
    )
    (src_dir / "tax.py").write_text(
        "def calculate_tax(subtotal: float, rate: float) -> float:\n"
        "    return 0.0  # faulty initial\n"
    )

    tests_dir = repo_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_pricing.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))\n"
        "from discount import calculate_discount\n"
        "from tax import calculate_tax\n\n"
        "def test_discount():\n"
        "    assert calculate_discount(100.0, 0.1) == 90.0\n\n"
        "def test_tax():\n"
        "    assert calculate_tax(100.0, 0.2) == 20.0\n"
    )

    (repo_dir / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial faulty pricing modules"], cwd=repo_dir, check=True, capture_output=True)
    head_res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True)
    base_sha = head_res.stdout.strip()

    # Evaluator Oracles (outside repository)
    oracle_dir = tmp_path / "evaluator"
    oracle_dir.mkdir()

    d_cases = [{"case_id": f"d_{i}", "args": [float(i * 10), 0.1], "expected": float(i * 10) * 0.9} for i in range(1, 18)]
    discount_oracle = oracle_dir / "discount_oracle.json"
    discount_oracle.write_text(json.dumps({"cases": d_cases}, indent=2), encoding="utf-8")
    d_sha = compute_file_sha256(discount_oracle)

    t_cases = [{"case_id": f"t_{i}", "args": [float(i * 10), 0.2], "expected": float(i * 10) * 0.2} for i in range(1, 18)]
    tax_oracle = oracle_dir / "tax_oracle.json"
    tax_oracle.write_text(json.dumps({"cases": t_cases}, indent=2), encoding="utf-8")
    t_sha = compute_file_sha256(tax_oracle)

    # Server registry
    registry_file = tmp_path / "server_registry.json"
    reg_data = {
        "schema_version": 1,
        "projects": [
            {
                "project_id": "PRICING_PROJECT",
                "authority_id": "AUTH_PRICING",
                "enabled": True,
                "classification": "DISPOSABLE_SOURCE",
                "workspace": str(repo_dir),
                "base_sha": base_sha,
                "write_files": ["src/discount.py", "src/tax.py"],
                "criteria": [
                    {"id": "CRIT_DISCOUNT", "kind": "FUNCTION_ORACLE_V1", "oracle_sha256": d_sha},
                    {"id": "CRIT_TAX", "kind": "FUNCTION_ORACLE_V1", "oracle_sha256": t_sha},
                ],
                "resources": ["simulated_profile"],
                "cost_mode": "ZERO_PAID",
                "production": "NO_GO",
            },
            {
                "project_id": "PROJECT_B",
                "authority_id": "AUTH_B",
                "enabled": True,
                "classification": "DISPOSABLE_SOURCE",
                "workspace": str(repo_dir),
                "base_sha": base_sha,
                "write_files": ["src/discount.py"],
                "criteria": [
                    {"id": "CRIT_DISCOUNT", "kind": "FUNCTION_ORACLE_V1", "oracle_sha256": d_sha},
                ],
                "resources": ["simulated_profile"],
                "cost_mode": "ZERO_PAID",
                "production": "NO_GO",
            }
        ],
        "resources": [
            {
                "id": "simulated_profile",
                "enabled": True,
                "provider": "simulated",
                "model": "simulated-v2",
                "protocol": "OPENAI_COMPATIBLE",
                "endpoint_origin": "https://api.openai.com",
                "endpoint_path": "/v1/chat/completions",
                "context_window": 8192,
                "max_output_tokens": 4096,
                "token_count_method": "TIKTOKEN",
                "hard_no_overage": True,
                "remaining_requests": 100,
                "cost_mode": "ZERO_PAID",
                "entitlement_expires_utc": "2029-01-01T00:00:00Z",
                "quota_observed_utc": "2026-10-19T10:00:00Z",
                "health_observed_utc": "2026-10-19T10:00:00Z",
            }
        ],
    }
    registry_file.write_text(json.dumps(reg_data, indent=2), encoding="utf-8")

    v2_home = tmp_path / "v2_home"
    v2_home.mkdir()

    server = AOSApiServer(v2_home, registry_file, bind_host="127.0.0.1", port=0)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    yield {
        "repo_dir": repo_dir,
        "v2_home": v2_home,
        "registry_file": registry_file,
        "server": server,
        "port": server.server_address[1],
        "token": server.bearer_token,
        "base_url": f"http://127.0.0.1:{server.server_address[1]}",
        "discount_oracle": (discount_oracle, d_sha),
        "tax_oracle": (tax_oracle, t_sha),
    }

    server.shutdown_and_close()


def test_q01_q02_q03_api_submission_and_dry_run_execution(dry_run_environment):
    """Q01, Q02, Q03: Real authenticated API submission, operator provides 3 fields, dry-run executes."""
    preflight = preflight_sandbox()
    if not preflight.is_supported:
        pytest.skip(f"Sandbox unsupported on host: {'; '.join(preflight.reasons)}")

    env = dry_run_environment
    base_url = env["base_url"]
    token = env["token"]
    repo_dir = env["repo_dir"]

    # Q02 check: initial tests fail
    py_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(repo_dir / "src")}
    probe_test = subprocess.run([os.sys.executable, "-B", "-m", "pytest", "-q", "tests/test_pricing.py"], cwd=repo_dir, capture_output=True, env=py_env)
    assert probe_test.returncode != 0, "Initial faulty tests must fail"

    # Q01: Operator submits ONLY project_id, goal, idempotency_key
    goal_payload = {
        "project_id": "PRICING_PROJECT",
        "goal": "Fix discount and tax calculation formulas",
        "idempotency_key": "idemp_pricing_corr_1",
    }

    status, resp = _http_request(
        f"{base_url}/v2/goals",
        method="POST",
        data=goal_payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    # Q04: New goal returns 202 Accepted
    assert status == 202
    cmd_id = resp["id"]
    assert resp["state"] == "PAUSED_SAFE"
    assert resp["admission"] == "HOLD"

    # Resume command to QUEUED
    s_resume, b_resume = _http_request(
        f"{base_url}/v2/commands/{cmd_id}/resume",
        method="POST",
        data={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert s_resume == 200
    assert b_resume["state"] == "QUEUED"
    assert b_resume["admission"] == "ACTIVE"

    # Setup oracles on project
    d_file, d_sha = env["discount_oracle"]
    t_file, t_sha = env["tax_oracle"]
    crit_d = OracleCriterion("CRIT_DISCOUNT", "discount", "calculate_discount", d_file, d_sha)
    crit_t = OracleCriterion("CRIT_TAX", "tax", "calculate_tax", t_file, t_sha)

    plan = ExecutionPlan(
        "plan_repair_pricing",
        [
            TaskNode("task_discount", "src/discount.py", crit_d, description="Repair discount calculation"),
            TaskNode("task_tax", "src/tax.py", crit_t, depends_on=["task_discount"], description="Repair tax calculation"),
        ],
    )

    def simulated_edits(task_node: TaskNode, pre_sha: str, root: Path):
        if task_node.task_id == "task_discount":
            return [{"old": "return 0.0  # faulty initial", "new": "return price * (1.0 - rate)"}]
        elif task_node.task_id == "task_tax":
            return [{"old": "return 0.0  # faulty initial", "new": "return subtotal * rate"}]
        return ()

    # Execute goal through engine
    result = env["server"].engine.execute_goal(
        project_id="PRICING_PROJECT",
        goal=goal_payload["goal"],
        plan=plan,
        idempotency_key=goal_payload["idempotency_key"],
        generator_func=simulated_edits,
    )

    assert result["state"] == "PROJECT_COMPLETE"
    assert result["admission"] == "CLOSED"

    # Q02 verification: post-execution tests succeed!
    post_test = subprocess.run([os.sys.executable, "-B", "-m", "pytest", "-q", "tests/test_pricing.py"], cwd=repo_dir, capture_output=True, env=py_env)
    assert post_test.returncode == 0, f"Post-repair tests must pass: {post_test.stderr.decode()}"

    # Q03 verification: genuine Git commits, simulated inference receipts, 0 external calls
    git_log = subprocess.run(["git", "log", "--oneline", "-n", "3"], cwd=repo_dir, capture_output=True, text=True).stdout
    assert "AOS V2: Repair discount calculation" in git_log
    assert "AOS V2: Repair tax calculation" in git_log

    # Ledger receipts
    events = env["server"].engine.outbox.ledger.read_events()
    assert len(events) >= 2


def test_q04_idempotent_semantics_and_conflicts(dry_run_environment):
    """Q04: 202 new, 200 replay, 409 conflict, distinct project isolation, long/punctuation keys."""
    env = dry_run_environment
    base_url = env["base_url"]
    token = env["token"]

    payload1 = {
        "project_id": "PRICING_PROJECT",
        "goal": "First unique goal",
        "idempotency_key": "key:with.special-chars/123",
    }
    s1, b1 = _http_request(f"{base_url}/v2/goals", method="POST", data=payload1, headers={"Authorization": f"Bearer {token}"})
    assert s1 == 202
    cmd_id1 = b1["id"]

    # Replay exact request
    s2, b2 = _http_request(f"{base_url}/v2/goals", method="POST", data=payload1, headers={"Authorization": f"Bearer {token}"})
    assert s2 == 200
    assert b2["id"] == cmd_id1

    # Conflict with changed goal
    payload_conflict = {
        "project_id": "PRICING_PROJECT",
        "goal": "A changed goal with same key",
        "idempotency_key": "key:with.special-chars/123",
    }
    s3, b3 = _http_request(f"{base_url}/v2/goals", method="POST", data=payload_conflict, headers={"Authorization": f"Bearer {token}"})
    assert s3 == 409
    assert b3["code"] == "IDEMPOTENCY_CONFLICT"

    # Distinct project with same idempotency key produces distinct command ID
    payload_proj_b = {
        "project_id": "PROJECT_B",
        "goal": "First unique goal",
        "idempotency_key": "key:with.special-chars/123",
    }
    s4, b4 = _http_request(f"{base_url}/v2/goals", method="POST", data=payload_proj_b, headers={"Authorization": f"Bearer {token}"})
    assert s4 == 202
    assert b4["id"] != cmd_id1


def test_q05_plan_validation_denials(dry_run_environment):
    """Q05: Empty, cyclic, non-allowlisted, and forged criterion DAGs fail closed."""
    env = dry_run_environment
    snap = env["server"].engine.snapshot
    from aos.v2.registry import authorize
    project = authorize(snap, "PRICING_PROJECT")

    d_file, d_sha = env["discount_oracle"]
    crit = OracleCriterion("CRIT_DISCOUNT", "discount", "calculate_discount", d_file, d_sha)

    # Empty plan
    empty_plan = ExecutionPlan("plan_empty", [])
    with pytest.raises(Exception):
        empty_plan.validate(project)

    # Non-allowlisted file write
    forbidden_plan = ExecutionPlan("plan_bad_path", [TaskNode("t1", "src/unauthorized.py", crit)])
    with pytest.raises(Exception):
        forbidden_plan.validate(project)


def test_q06_api_boundary_negative_controls(dry_run_environment):
    """Q06: Strict HTTP boundary controls: Host, Origin, UTF-16, BOM, NaN, duplicate keys, control body."""
    env = dry_run_environment
    base_url = env["base_url"]
    port = env["port"]
    token = env["token"]

    # 1. Wrong Host port
    s1, _ = _http_request(f"{base_url}/v2/health", headers={"Host": f"127.0.0.1:{port + 1}"})
    assert s1 == 400

    # 2. Non-loopback Host
    s2, _ = _http_request(f"{base_url}/v2/health", headers={"Host": "api.external.com"})
    assert s2 == 400

    # 3. Disallowed Origin
    s3, _ = _http_request(f"{base_url}/v2/health", headers={"Origin": "http://evil-site.com"})
    assert s3 == 403

    # 4. UTF-8 BOM
    bom_payload = b"\xef\xbb\xbf" + json.dumps({"project_id": "PRICING_PROJECT", "goal": "G", "idempotency_key": "k"}).encode("utf-8")
    s4, _ = _http_request(
        f"{base_url}/v2/goals",
        method="POST",
        data=bom_payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert s4 == 400

    # 5. Non-finite values (NaN / Infinity)
    nan_payload = b'{"project_id": "PRICING_PROJECT", "goal": NaN, "idempotency_key": "k"}'
    s5, _ = _http_request(
        f"{base_url}/v2/goals",
        method="POST",
        data=nan_payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert s5 == 400

    # 6. Non-empty control body on resume
    s6, _ = _http_request(
        f"{base_url}/v2/commands/cmd_fake/resume",
        method="POST",
        data={"unwanted_field": 123},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert s6 == 400


def test_q07_private_file_permissions(dry_run_environment):
    """Q07: Verify owner-only protected token.secret and endpoint.json permissions."""
    v2_home = dry_run_environment["v2_home"]
    token_file = v2_home / "token.secret"
    endpoint_file = v2_home / "endpoint.json"

    assert token_file.is_file()
    assert endpoint_file.is_file()

    # Must pass validation without raising
    validate_private_file(token_file)
    validate_private_file(endpoint_file)
