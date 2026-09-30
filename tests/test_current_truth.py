from __future__ import annotations

import json
import threading
from pathlib import Path

import aos.current_truth as current_truth
from aos.current_truth import PROTECTED_COMMANDS, generate_current_truth, refresh_current_truth
from aos.runtime_server import RuntimeEngine


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _set_identity(runtime_home: Path, *, source_sha: str, slot_id: str) -> dict:
    runtime_root = runtime_home / "state"
    candidate_root = runtime_home / "candidate" / source_sha
    policy_path = runtime_home / "routing-policy.json"
    _write(policy_path, {"allow_paid_fallback": False, "paid_fallback_enabled": False})
    config = {
        "contract_version": "1.0.0",
        "runtime_root": str(runtime_root),
        "port": 18770,
        "candidate_source_sha": source_sha,
        "runtime_slot_id": slot_id,
        "runtime_slot_root": str(candidate_root),
        "operations_ref": "refs/heads/operations",
        "production": "NO_GO",
        "projects": {
            "lari": {"routing_policy_path": str(policy_path)},
        },
    }
    _write(runtime_home / "runtime-config.json", config)
    _write(runtime_home / "supervisor-config.json", {"supervisor_root": str(runtime_home / "supervisor")})
    _write(
        runtime_home / "supervisor" / "active-slot.json",
        {
            "contract_version": "1.0.0",
            "stable_slot_id": slot_id,
            "candidate_slot_id": slot_id,
            "active": "stable",
            "promotion_state": "STABLE",
        },
    )
    _write(
        runtime_home / "supervisor" / "slots" / f"{slot_id}.json",
        {
            "contract_version": "1.0.0",
            "slot_id": slot_id,
            "kind": "runtime_v1",
            "command": ["python", "-m", "aos.runtime_server"],
            "source_sha": source_sha,
        },
    )
    _write(
        candidate_root / "candidate-manifest.json",
        {
            "candidate_slot_id": slot_id,
            "candidate_source_sha": source_sha,
            "build_source_sha": source_sha,
            "provenance": "PROVEN",
        },
    )
    _write(runtime_root / "maintenance-state.json", {"state": "PAUSED_SAFE"})
    admissions = {"records": {}}
    for label, command_id in PROTECTED_COMMANDS.items():
        _write(runtime_root / "commands" / command_id / "state.json", {"state": f"STATE_{label}"})
        admissions["records"][command_id] = {"state": "HOLD"}
    _write(runtime_root / "command-admission.json", admissions)
    return config


def _health(source_sha: str, slot_id: str) -> dict:
    return {
        "contract_version": "1.0.0",
        "runtime_state": "HEALTHY",
        "runtime_source_sha": source_sha,
        "runtime_slot_id": slot_id,
        "paused": True,
    }


def _fixed_git(monkeypatch) -> None:
    monkeypatch.setattr(current_truth, "_git_sha", lambda repo, ref: SHA_D)


def test_runtime_source_changes_projection_without_git_commit(tmp_path, monkeypatch):
    _fixed_git(monkeypatch)
    runtime_home = tmp_path / "runtime-home"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _set_identity(runtime_home, source_sha=SHA_A, slot_id="slot-a")
    first = generate_current_truth(
        runtime_home, repo_root, runtime_observation=_health(SHA_A, "slot-a"), query_runtime=False
    )

    _set_identity(runtime_home, source_sha=SHA_B, slot_id="slot-b")
    second = generate_current_truth(
        runtime_home, repo_root, runtime_observation=_health(SHA_B, "slot-b"), query_runtime=False
    )

    assert first["observations"]["repository"]["local_checkout_head"]["value"] == SHA_D
    assert second["observations"]["repository"]["local_checkout_head"]["value"] == SHA_D
    assert first["observations"]["runtime"]["source_sha"]["value"] == SHA_A
    assert second["observations"]["runtime"]["source_sha"]["value"] == SHA_B


def test_active_slot_change_is_observed_without_git_commit(tmp_path, monkeypatch):
    _fixed_git(monkeypatch)
    runtime_home = tmp_path / "runtime-home"
    config = _set_identity(runtime_home, source_sha=SHA_A, slot_id="slot-a")
    _write(
        runtime_home / "supervisor" / "slots" / "slot-b.json",
        {
            "contract_version": "1.0.0",
            "slot_id": "slot-b",
            "kind": "runtime_v1",
            "command": ["python"],
            "source_sha": SHA_A,
        },
    )
    before = generate_current_truth(
        runtime_home, tmp_path, runtime_observation=_health(SHA_A, "slot-a"), query_runtime=False
    )
    _write(
        runtime_home / "supervisor" / "active-slot.json",
        {
            "contract_version": "1.0.0",
            "stable_slot_id": "slot-a",
            "candidate_slot_id": "slot-b",
            "active": "candidate",
            "promotion_state": "TRIAL",
        },
    )
    after = generate_current_truth(
        runtime_home, tmp_path, runtime_observation=_health(SHA_A, config["runtime_slot_id"]), query_runtime=False
    )
    assert before["observations"]["slot_candidate"]["active_slot_pointer"]["value"]["slot_id"] == "slot-a"
    assert after["observations"]["slot_candidate"]["active_slot_pointer"]["value"]["slot_id"] == "slot-b"
    assert after["observations"]["slot_candidate"]["slot_binding"]["status"] == "CONTRADICTION"


def test_command_admission_and_state_changes_are_fresh(tmp_path, monkeypatch):
    _fixed_git(monkeypatch)
    runtime_home = tmp_path / "runtime-home"
    _set_identity(runtime_home, source_sha=SHA_A, slot_id="slot-a")
    command_id = PROTECTED_COMMANDS["lari"]
    first = generate_current_truth(
        runtime_home, tmp_path, runtime_observation=_health(SHA_A, "slot-a"), query_runtime=False
    )
    _write(runtime_home / "state" / "commands" / command_id / "state.json", {"state": "HUMAN_REQUIRED"})
    admission_path = runtime_home / "state" / "command-admission.json"
    admissions = json.loads(admission_path.read_text(encoding="utf-8"))
    admissions["records"][command_id]["state"] = "ACTIVE"
    _write(admission_path, admissions)
    second = generate_current_truth(
        runtime_home, tmp_path, runtime_observation=_health(SHA_A, "slot-a"), query_runtime=False
    )
    assert first["observations"]["commands"]["protected_command_states"]["lari"]["value"] == "STATE_lari"
    assert second["observations"]["commands"]["protected_command_states"]["lari"]["value"] == "HUMAN_REQUIRED"
    assert first["observations"]["commands"]["admission_store"]["commands"]["lari"]["value"] == "HOLD"
    assert second["observations"]["commands"]["admission_store"]["commands"]["lari"]["value"] == "ACTIVE"


def test_missing_observations_are_unknown_and_previous_values_are_not_carried(tmp_path, monkeypatch):
    _fixed_git(monkeypatch)
    runtime_home = tmp_path / "runtime-home"
    _set_identity(runtime_home, source_sha=SHA_A, slot_id="slot-a")
    refresh_current_truth(
        runtime_home,
        tmp_path,
        runtime_observation=_health(SHA_A, "slot-a"),
        query_runtime=False,
    )

    (runtime_home / "runtime-config.json").unlink()
    (runtime_home / "supervisor" / "active-slot.json").unlink()
    refreshed = refresh_current_truth(runtime_home, tmp_path, query_runtime=False)
    persisted = json.loads((runtime_home / "current-truth.json").read_text(encoding="utf-8"))

    assert refreshed["observations"]["runtime"]["source_sha"]["status"] == "UNKNOWN"
    assert refreshed["observations"]["runtime"]["health"]["status"] == "UNKNOWN"
    assert refreshed["observations"]["slot_candidate"]["active_slot_pointer"]["status"] == "UNKNOWN"
    assert SHA_A not in json.dumps(persisted)


def test_conflicting_slot_candidate_and_runtime_identity_is_contradiction(tmp_path, monkeypatch):
    _fixed_git(monkeypatch)
    runtime_home = tmp_path / "runtime-home"
    config = _set_identity(runtime_home, source_sha=SHA_A, slot_id="slot-a")
    slot_path = runtime_home / "supervisor" / "slots" / "slot-a.json"
    slot = json.loads(slot_path.read_text(encoding="utf-8"))
    slot["source_sha"] = SHA_B
    _write(slot_path, slot)
    manifest_path = Path(config["runtime_slot_root"]) / "candidate-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["candidate_source_sha"] = SHA_C
    manifest["build_source_sha"] = SHA_C
    _write(manifest_path, manifest)

    projection = generate_current_truth(
        runtime_home, tmp_path, runtime_observation=_health(SHA_D, "slot-a"), query_runtime=False
    )

    assert projection["overall_status"] == "CONTRADICTION"
    assert projection["observations"]["runtime"]["source_sha"]["value"] == SHA_D
    assert projection["observations"]["slot_candidate"]["source_binding"]["status"] == "CONTRADICTION"
    assert any(item["path"] == "slot_candidate.source_binding" for item in projection["contradictions"])


def test_tracked_pointer_has_no_dynamic_operational_snapshot_fields():
    repo_root = Path(__file__).resolve().parents[1]
    pointer = json.loads(
        (repo_root / "docs" / "project-control" / "AOS_CURRENT_TRUTH.json").read_text(encoding="utf-8")
    )
    forbidden = {
        "as_of",
        "operations_source_sha",
        "live_runtime",
        "runtime_source_sha",
        "runtime_slot",
        "runtime_health",
        "worker_pid",
        "current_command_state",
        "current_admission",
        "current_ci_result",
    }

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    assert forbidden.isdisjoint(set(keys(pointer)))
    assert pointer["document_type"] == "CURRENT_TRUTH_POINTER"
    assert "python -m aos.current_truth" in pointer["command"]


def test_state_json_is_not_consumed_as_live_runtime_truth(tmp_path, monkeypatch):
    monkeypatch.setattr(current_truth, "_git_sha", lambda repo, ref: None)
    repo_root = tmp_path / "repo"
    _write(
        repo_root / "docs" / "project-control" / "STATE.json",
        {"live_runtime": {"source_sha": SHA_A, "health": "HEALTHY"}},
    )
    projection = generate_current_truth(tmp_path / "empty-runtime", repo_root, query_runtime=False)
    assert projection["observations"]["runtime"]["source_sha"]["status"] == "UNKNOWN"
    assert projection["observations"]["runtime"]["health"]["status"] == "UNKNOWN"
    assert SHA_A not in json.dumps(projection)


def test_projection_policy_is_no_go_and_paid_fallback_disabled(tmp_path, monkeypatch):
    _fixed_git(monkeypatch)
    runtime_home = tmp_path / "runtime-home"
    _set_identity(runtime_home, source_sha=SHA_A, slot_id="slot-a")
    projection = generate_current_truth(
        runtime_home, tmp_path, runtime_observation=_health(SHA_A, "slot-a"), query_runtime=False
    )
    assert projection["production"] == "NO_GO"
    assert projection["paid_fallback"] == "DISABLED"
    assert projection["observations"]["policy"]["production"]["value"] == "NO_GO"
    assert projection["observations"]["policy"]["paid_fallback"]["value"] == "DISABLED"


def test_runtime_engine_refresh_does_not_mutate_product_command_state(tmp_path, monkeypatch):
    _fixed_git(monkeypatch)
    runtime_home = tmp_path / "runtime-home"
    config = _set_identity(runtime_home, source_sha=SHA_A, slot_id="slot-a")
    command_id = PROTECTED_COMMANDS["lari_ui_v2"]
    state_path = runtime_home / "state" / "commands" / command_id / "state.json"
    before = state_path.read_bytes()

    engine = RuntimeEngine.__new__(RuntimeEngine)
    engine.config = config
    engine.runtime_root = runtime_home / "state"
    engine.runtime_home = runtime_home
    engine.current_truth_repo_root = tmp_path
    engine.is_paused = True
    engine._status_lock = threading.Lock()
    engine._status_cache = {}
    engine._refresh_current_truth()

    assert state_path.read_bytes() == before
    assert (runtime_home / "current-truth.json").is_file()
    assert engine._status_cache["current_truth_refresh_status"] in {"READY", "DEGRADED"}


def test_refresh_failure_is_visible_and_does_not_raise(tmp_path, monkeypatch):
    _fixed_git(monkeypatch)
    runtime_home = tmp_path / "runtime-home"
    config = _set_identity(runtime_home, source_sha=SHA_A, slot_id="slot-a")
    engine = RuntimeEngine.__new__(RuntimeEngine)
    engine.config = config
    engine.runtime_root = runtime_home / "state"
    engine.runtime_home = runtime_home
    engine.current_truth_repo_root = tmp_path
    engine.is_paused = True
    engine._status_lock = threading.Lock()
    engine._status_cache = {}
    monkeypatch.setattr("aos.runtime_server.refresh_current_truth", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))

    engine._refresh_current_truth()

    assert engine._status_cache["current_truth_refresh_status"] == "DEGRADED"
    assert engine._status_cache["current_truth_overall_status"] == "UNKNOWN"
    assert engine._status_cache["current_truth_error_class"] == "OSError"


def test_cli_prints_and_persists_same_fresh_projection(tmp_path, monkeypatch, capsys):
    _fixed_git(monkeypatch)
    runtime_home = tmp_path / "runtime-home"
    _set_identity(runtime_home, source_sha=SHA_A, slot_id="slot-a")
    monkeypatch.setattr(current_truth, "_live_health", lambda config: _health(SHA_A, "slot-a"))

    assert current_truth.main(["--runtime-home", str(runtime_home), "--repo-root", str(tmp_path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    persisted = json.loads((runtime_home / "current-truth.json").read_text(encoding="utf-8"))
    assert printed == persisted
    assert printed["contract_version"] == "1.0.0"
