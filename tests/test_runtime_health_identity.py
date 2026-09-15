from pathlib import Path

from aos.runtime_server import RuntimeEngine


def _config(tmp_path: Path):
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "runtime"),
        "runtime_token_path": str(tmp_path / "token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {
            "lari": {
                "project_id": "lari",
                "descriptor_path": str(descriptor),
                "workspace": str(workspace),
                "routing_policy_path": str(policy),
                "standing_authority": True,
            }
        },
        "default_project": "lari",
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "candidate_source_sha": "a" * 40,
        "runtime_asset_tree_sha256": "b" * 64,
        "runtime_slot_root": str(tmp_path / "slot"),
        "runtime_slot_id": "candidate-runtime-v1.4-test",
    }


def test_health_reports_exact_candidate_identity(tmp_path: Path):
    engine = RuntimeEngine(_config(tmp_path))
    try:
        health = engine.health()
        assert health["runtime_source_sha"] == "a" * 40
        assert health["runtime_asset_tree_sha256"] == "b" * 64
        assert health["runtime_slot_root"].endswith("slot")
        assert health["runtime_slot_id"] == "candidate-runtime-v1.4-test"
        assert health["production"] == "NO_GO"
        assert health["ag_backend_enabled"] is False
    finally:
        engine.shutdown()
