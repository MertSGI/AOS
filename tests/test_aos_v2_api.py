"""Tests for AOS V2 Loopback API Server and CLI."""

import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from aos.v2.api import AOSApiServer
from aos.v2.sandbox import get_available_ram_bytes


@pytest.fixture(autouse=True)
def mock_adequate_ram(monkeypatch):
    orig = get_available_ram_bytes()
    monkeypatch.setattr("aos.v2.sandbox.get_available_ram_bytes", lambda: max(orig, 2 * 1024 * 1024 * 1024))


@pytest.fixture
def api_test_env(tmp_path):
    v2_home = tmp_path / "v2_home"
    v2_home.mkdir()

    repo_dir = tmp_path / "disposable_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "AOS Tester"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@aos.local"], cwd=repo_dir, check=True, capture_output=True)
    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "discount.py").write_text("def apply_discount(p, r): return p\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Init"], cwd=repo_dir, check=True, capture_output=True)
    head_res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True)
    base_sha = head_res.stdout.strip()

    reg_file = tmp_path / "server_registry.json"
    reg_data = {
        "schema_version": 1,
        "projects": [
            {
                "project_id": "DISPOSABLE_PROJ",
                "authority_id": "AUTH_DISPOSABLE",
                "enabled": True,
                "classification": "DISPOSABLE_SOURCE",
                "workspace": str(repo_dir),
                "base_sha": base_sha,
                "write_files": ["src/discount.py"],
                "criteria": [{"id": "crit_1", "test_id": "discount"}],
                "resources": [],
                "cost_mode": "ZERO_PAID",
                "production": "NO_GO",
            }
        ],
        "resources": [],
    }
    reg_file.write_text(json.dumps(reg_data, indent=2), encoding="utf-8")

    server = AOSApiServer(v2_home, reg_file, bind_host="127.0.0.1", port=0)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    port = server.server_address[1]
    token = server.bearer_token

    yield {
        "v2_home": v2_home,
        "registry_file": reg_file,
        "server": server,
        "port": port,
        "token": token,
        "base_url": f"http://127.0.0.1:{port}",
    }

    server.shutdown_and_close()


def _request(
    url: str,
    method: str = "GET",
    data: dict | bytes | str | None = None,
    headers: dict | None = None,
) -> tuple[int, dict | str]:
    """Helper to send HTTP request and return (status_code, response_json_or_text)."""
    req_headers = headers.copy() if headers else {}
    body_bytes = None
    if isinstance(data, dict):
        body_bytes = json.dumps(data).encode("utf-8")
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


def test_endpoint_and_token_secret_written(api_test_env):
    v2_home = api_test_env["v2_home"]
    endpoint_file = v2_home / "endpoint.json"
    token_file = v2_home / "token.secret"

    assert endpoint_file.is_file()
    assert token_file.is_file()

    ep_data = json.loads(endpoint_file.read_text(encoding="utf-8"))
    assert ep_data["schema_version"] == 1
    assert ep_data["host"] == "127.0.0.1"
    assert ep_data["port"] == api_test_env["port"]
    assert "pid" in ep_data
    assert "nonce" in ep_data
    assert "source_sha" in ep_data

    saved_token = token_file.read_text(encoding="utf-8").strip()
    assert saved_token == api_test_env["token"]


def test_health_check_public_loopback(api_test_env):
    base_url = api_test_env["base_url"]
    status, body = _request(f"{base_url}/v2/health")
    assert status == 200
    assert body["status"] == "OK"
    assert body["alive"] is True
    assert "pid" in body
    assert "nonce" in body


def test_unauthenticated_request_rejected(api_test_env):
    base_url = api_test_env["base_url"]
    status, body = _request(f"{base_url}/v2/commands/cmd_123")
    assert status == 401
    assert body["code"] == "UNAUTHORIZED"


def test_invalid_bearer_token_rejected(api_test_env):
    base_url = api_test_env["base_url"]
    status, body = _request(
        f"{base_url}/v2/commands/cmd_123",
        headers={"Authorization": "Bearer wrong_token_value_00000000000000"},
    )
    assert status == 401
    assert body["code"] == "UNAUTHORIZED"


def test_cors_origin_rejected(api_test_env):
    base_url = api_test_env["base_url"]
    token = api_test_env["token"]
    status, body = _request(
        f"{base_url}/v2/commands/cmd_123",
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "http://evil-external-site.com",
        },
    )
    assert status == 403
    assert body["code"] == "CORS_DENIED"


def test_non_loopback_host_header_rejected(api_test_env):
    base_url = api_test_env["base_url"]
    token = api_test_env["token"]
    status, body = _request(
        f"{base_url}/v2/commands/cmd_123",
        headers={
            "Authorization": f"Bearer {token}",
            "Host": "api.external.com",
        },
    )
    assert status == 400
    assert body["code"] == "INVALID_HOST"


def test_oversized_body_rejected(api_test_env):
    base_url = api_test_env["base_url"]
    token = api_test_env["token"]
    oversized = b"a" * (65 * 1024)  # 65 KiB > 64 KiB
    status, body = _request(
        f"{base_url}/v2/goals",
        method="POST",
        data=oversized,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status == 413
    assert body["code"] == "BODY_TOO_LARGE"


def test_duplicate_json_keys_rejected(api_test_env):
    base_url = api_test_env["base_url"]
    token = api_test_env["token"]
    duplicate_payload = '{"project_id": "DISPOSABLE_PROJ", "project_id": "FORGED", "goal": "Repair", "idempotency_key": "k1"}'
    status, body = _request(
        f"{base_url}/v2/goals",
        method="POST",
        data=duplicate_payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    assert status == 400
    assert body["code"] == "INVALID_JSON"
    assert "Duplicate JSON key" in body["error"]


def test_forbidden_fields_in_goal_rejected(api_test_env):
    base_url = api_test_env["base_url"]
    token = api_test_env["token"]
    forbidden_payload = {
        "project_id": "DISPOSABLE_PROJ",
        "goal": "Repair discount",
        "idempotency_key": "idemp_forbidden_1",
        "authority": "ROOT_OVERRIDE",
        "cost_mode": "PAID_ALLOWED",
    }
    status, body = _request(
        f"{base_url}/v2/goals",
        method="POST",
        data=forbidden_payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status == 400
    assert body["code"] == "FORBIDDEN_FIELDS"


def test_goal_admission_first_startup_paused_safe(api_test_env):
    base_url = api_test_env["base_url"]
    token = api_test_env["token"]
    valid_payload = {
        "project_id": "DISPOSABLE_PROJ",
        "goal": "Repair discount calculation",
        "idempotency_key": "idemp_safe_1",
    }
    status, body = _request(
        f"{base_url}/v2/goals",
        method="POST",
        data=valid_payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status == 201
    assert body["state"] == "PAUSED_SAFE"
    cmd_id = body["id"]

    # Query command status
    status2, body2 = _request(
        f"{base_url}/v2/commands/{cmd_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status2 == 200
    assert body2["id"] == cmd_id
    assert body2["state"] == "PAUSED_SAFE"


def test_idempotent_replay_and_conflict(api_test_env):
    base_url = api_test_env["base_url"]
    token = api_test_env["token"]
    payload = {
        "project_id": "DISPOSABLE_PROJ",
        "goal": "Repair discount calculation",
        "idempotency_key": "idemp_unique_99",
    }
    status1, body1 = _request(
        f"{base_url}/v2/goals",
        method="POST",
        data=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status1 == 201
    cmd_id = body1["id"]

    # Exact same request: returns 200 with same command ID
    status2, body2 = _request(
        f"{base_url}/v2/goals",
        method="POST",
        data=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status2 == 200
    assert body2["id"] == cmd_id

    # Same idempotency key with CHANGED immutable input: returns 409 Conflict
    conflict_payload = {
        "project_id": "DISPOSABLE_PROJ",
        "goal": "A completely different goal",
        "idempotency_key": "idemp_unique_99",
    }
    status3, body3 = _request(
        f"{base_url}/v2/goals",
        method="POST",
        data=conflict_payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status3 == 409
    assert body3["code"] == "IDEMPOTENCY_CONFLICT"


def test_command_pause_resume_cancel_lifecycle(api_test_env):
    base_url = api_test_env["base_url"]
    token = api_test_env["token"]
    payload = {
        "project_id": "DISPOSABLE_PROJ",
        "goal": "Repair discount calculation",
        "idempotency_key": "idemp_ctrl_1",
    }
    _, cmd = _request(
        f"{base_url}/v2/goals",
        method="POST",
        data=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    cmd_id = cmd["id"]
    assert cmd["state"] == "PAUSED_SAFE"

    # Resume: PAUSED_SAFE -> QUEUED
    s_resume, b_resume = _request(
        f"{base_url}/v2/commands/{cmd_id}/resume",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert s_resume == 200
    assert b_resume["state"] == "QUEUED"

    # Pause: QUEUED -> PAUSED_SAFE
    s_pause, b_pause = _request(
        f"{base_url}/v2/commands/{cmd_id}/pause",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert s_pause == 200
    assert b_pause["state"] == "PAUSED_SAFE"

    # Cancel: PAUSED_SAFE -> CANCELLED
    s_cancel, b_cancel = _request(
        f"{base_url}/v2/commands/{cmd_id}/cancel",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert s_cancel == 200
    assert b_cancel["state"] == "CANCELLED"
    assert b_cancel["admission"] == "CLOSED"
