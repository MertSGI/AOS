"""Negative and conformance tests for AOS V2 server registry & authorization."""
import datetime
import json
import os
import subprocess
from pathlib import Path

import pytest

from aos.v2.registry import (
    AuthorizedProject,
    RegistryAuthorizationError,
    RegistrySnapshot,
    authorize,
    load_registry,
    revalidate,
)


def _init_disposable_repo(path: Path) -> str:
    """Initialize a disposable Git repository and commit an initial file."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "AOS Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@aos.local"], cwd=path, check=True)
    (path / "src").mkdir(exist_ok=True)
    (path / "src" / "one.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=path, check=True, capture_output=True)
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True)
    return res.stdout.strip()


@pytest.fixture
def registry_fixture(tmp_path):
    repo_dir = tmp_path / "repo"
    head = _init_disposable_repo(repo_dir)
    reg_file = tmp_path / "registry.json"
    data = {
        "schema_version": 1,
        "projects": [
            {
                "project_id": "test-project",
                "enabled": True,
                "classification": "DISPOSABLE_SOURCE",
                "authority_id": "AUTH-001",
                "workspace": str(repo_dir),
                "base_sha": head,
                "write_files": ["src/one.py"],
                "criteria": [
                    {
                        "id": "crit-1",
                        "kind": "FUNCTION_ORACLE_V1",
                        "module": "src.one",
                        "symbol": "foo",
                        "oracle_sha256": "0" * 64,
                    }
                ],
                "resources": ["free-res-1"],
                "cost_mode": "ZERO_PAID",
                "production": "NO_GO",
                "expires_at": None,
                "budget": {
                    "max_network_attempts": 4,
                    "max_active_seconds": 1800,
                    "max_context_bytes": 32768,
                    "max_output_bytes": 8192,
                },
            }
        ],
        "resources": [
            {
                "id": "free-res-1",
                "enabled": True,
                "provider": "openai_compatible",
                "model": "gpt-free-model",
            }
        ],
    }
    reg_file.write_text(json.dumps(data))
    return reg_file, data, repo_dir, head


def test_load_and_authorize_valid_registry(registry_fixture):
    reg_file, data, repo_dir, head = registry_fixture
    snapshot = load_registry(reg_file)
    assert snapshot.schema_version == 1
    assert "test-project" in snapshot.projects
    assert "free-res-1" in snapshot.resources

    auth = authorize(snapshot, "test-project")
    assert isinstance(auth, AuthorizedProject)
    assert auth.project_id == "test-project"
    assert auth.workspace == str(repo_dir.resolve())
    assert auth.base_sha == head
    assert auth.write_files == ("src/one.py",)
    assert auth.cost_mode == "ZERO_PAID"
    assert auth.production == "NO_GO"
    assert auth.is_expired() is False


def test_revalidate_matching_binding(registry_fixture):
    reg_file, data, repo_dir, head = registry_fixture
    snapshot = load_registry(reg_file)
    auth = authorize(snapshot, "test-project")
    binding = auth.binding()

    revalidated = revalidate(binding, reg_file)
    assert revalidated.project_id == auth.project_id
    assert revalidated.base_sha == auth.base_sha
    assert revalidated.binding() == binding


def test_reject_product_lanes(tmp_path):
    reg_file = tmp_path / "registry.json"
    for forbidden in ["lari", "lari-ui-v2"]:
        data = {
            "schema_version": 1,
            "projects": [
                {
                    "project_id": forbidden,
                    "enabled": True,
                    "classification": "DISPOSABLE_SOURCE",
                    "authority_id": "AUTH-001",
                    "workspace": str(tmp_path),
                    "base_sha": "a" * 40,
                    "write_files": ["src/one.py"],
                    "cost_mode": "ZERO_PAID",
                    "production": "NO_GO",
                }
            ],
            "resources": [],
        }
        reg_file.write_text(json.dumps(data))
        with pytest.raises(RegistryAuthorizationError):
            load_registry(reg_file)


def test_reject_protected_roots(registry_fixture, tmp_path):
    reg_file, data, _, _ = registry_fixture
    bad_ws = tmp_path / "runtime-v1" / "nested"
    bad_ws.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=bad_ws, check=True, capture_output=True)
    (bad_ws / "src").mkdir()
    (bad_ws / "src" / "one.py").write_text("x")
    subprocess.run(["git", "add", "."], cwd=bad_ws, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=bad_ws, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=bad_ws, check=True, capture_output=True, text=True).stdout.strip()

    data["projects"][0]["workspace"] = str(bad_ws)
    data["projects"][0]["base_sha"] = head
    reg_file.write_text(json.dumps(data))
    snap = load_registry(reg_file)
    with pytest.raises(RegistryAuthorizationError):
        authorize(snap, "test-project")


def test_reject_nonportable_path_aliases(registry_fixture):
    reg_file, data, _, _ = registry_fixture
    for bad_path in [
        ".git/config",
        ".GIT/HEAD",
        "src/NUL.py",
        "src/CON.txt",
        "src/trailing.",
        "src/trailing ",
        "src/stream:test",
        "../outside.py",
    ]:
        data["projects"][0]["write_files"] = [bad_path]
        reg_file.write_text(json.dumps(data))
        snap = load_registry(reg_file)
        with pytest.raises(RegistryAuthorizationError):
            authorize(snap, "test-project")


def test_reject_hardlink_scope(registry_fixture, tmp_path):
    reg_file, data, repo_dir, _ = registry_fixture
    sentinel = tmp_path / "sentinel.py"
    sentinel.write_text("secret")
    os.link(sentinel, repo_dir / "src" / "linked.py")
    data["projects"][0]["write_files"] = ["src/linked.py"]
    reg_file.write_text(json.dumps(data))
    snap = load_registry(reg_file)
    with pytest.raises(RegistryAuthorizationError):
        authorize(snap, "test-project")


def test_reject_expired_grant(registry_fixture):
    reg_file, data, _, _ = registry_fixture
    # Expired 1 hour ago
    past = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat()
    data["projects"][0]["expires_at"] = past
    reg_file.write_text(json.dumps(data))
    snap = load_registry(reg_file)
    with pytest.raises(RegistryAuthorizationError, match="expired"):
        authorize(snap, "test-project")


def test_reject_disabled_project(registry_fixture):
    reg_file, data, _, _ = registry_fixture
    data["projects"][0]["enabled"] = False
    reg_file.write_text(json.dumps(data))
    snap = load_registry(reg_file)
    with pytest.raises(RegistryAuthorizationError, match="disabled"):
        authorize(snap, "test-project")


def test_reject_paid_cost_mode(registry_fixture):
    reg_file, data, _, _ = registry_fixture
    data["projects"][0]["cost_mode"] = "PAID"
    reg_file.write_text(json.dumps(data))
    snap = load_registry(reg_file)
    with pytest.raises(RegistryAuthorizationError, match="cost_mode"):
        authorize(snap, "test-project")


def test_reject_excessive_attempts_budget(registry_fixture):
    reg_file, data, _, _ = registry_fixture
    data["projects"][0]["budget"]["max_network_attempts"] = 10
    reg_file.write_text(json.dumps(data))
    snap = load_registry(reg_file)
    with pytest.raises(RegistryAuthorizationError, match="max_network_attempts"):
        authorize(snap, "test-project")


def test_revalidate_fails_on_mutation(registry_fixture):
    reg_file, data, repo_dir, head = registry_fixture
    snap = load_registry(reg_file)
    auth = authorize(snap, "test-project")
    binding = auth.binding()

    # Modify the registry workspace
    data["projects"][0]["base_sha"] = "b" * 40
    reg_file.write_text(json.dumps(data))

    with pytest.raises(RegistryAuthorizationError):
        revalidate(binding, reg_file)
