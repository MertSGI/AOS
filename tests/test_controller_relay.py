"""Comprehensive unit and contract tests for AOS Native Controller Relay & Remote Outbox."""
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aos.controller_relay import (
    ControllerRelayPublisher,
    LaneTelemetry,
    RelaySnapshot,
    sanitize_text,
)


def test_sanitize_text():
    secret_gh = "ghp_123456789012345678901234567890123456"
    secret_gemini = "AIzaSyDummySecretKeyForGeminiApiTesting30"
    secret_nv = "nvapi-1234567890abcdef1234567890abcdef12345678"
    bearer = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummysecret"

    raw = f"Token is {secret_gh} and gemini is {secret_gemini} with {secret_nv} and header: {bearer}"
    sanitized = sanitize_text(raw)
    assert secret_gh not in sanitized
    assert secret_gemini not in sanitized
    assert secret_nv not in sanitized
    assert "Bearer" not in sanitized
    assert "[REDACTED_SECRET]" in sanitized


def test_atomic_local_publish(tmp_path: Path):
    relay_dir = tmp_path / "controller-relay"
    config = {
        "runtime_root": str(tmp_path / "state"),
        "candidate_source_sha": "57a47e9bf3c71c449b6e42b5598c837d766ed667",
        "runtime_slot_id": "candidate-slot-test",
    }
    pub = ControllerRelayPublisher(relay_dir, config, writer_instance_id="test-sup-1")

    # Create dummy snapshot
    snap = pub.collect_snapshot(
        runtime_health_dict={
            "runtime_state": "HEALTHY",
            "runtime_slot_id": "candidate-slot-test",
            "runtime_source_sha": "57a47e9bf3c71c449b6e42b5598c837d766ed667",
            "pid": 1234,
        },
        supervisor_pid=5678,
    )
    assert snap.writer == "AOS"
    assert snap.sequence_number == 1

    pub.publish_local(snap, is_checkpoint=False)

    md_file = relay_dir / "LATEST.md"
    json_file = relay_dir / "LATEST.json"
    events_file = relay_dir / "events.jsonl"

    assert md_file.is_file()
    assert json_file.is_file()
    assert events_file.is_file()

    md_content = md_file.read_text(encoding="utf-8")
    assert "REPORT_TYPE=AOS_NATIVE_HEARTBEAT_AND_CHECKPOINT" in md_content
    assert "WRITER=AOS" in md_content
    assert "SEQUENCE_NUMBER=1" in md_content

    json_data = json.loads(json_file.read_text(encoding="utf-8"))
    assert json_data["writer"] == "AOS"
    assert json_data["sequence_number"] == 1
    assert json_data["runtime_health"] == "HEALTHY"

    events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").strip().splitlines()]
    assert len(events) == 1
    assert events[0]["event_type"] == "HEARTBEAT"
    assert events[0]["sequence_number"] == 1


def test_stale_fallback_writer_rejection(tmp_path: Path):
    relay_dir = tmp_path / "controller-relay"
    config = {"runtime_root": str(tmp_path / "state")}
    pub = ControllerRelayPublisher(relay_dir, config, writer_instance_id="test-sup-1")

    # 1. Native AOS writes seq 5
    snap_aos = pub.collect_snapshot()
    snap_aos.sequence_number = 5
    pub.publish_local(snap_aos)

    latest = json.loads((relay_dir / "LATEST.json").read_text(encoding="utf-8"))
    assert latest["writer"] == "AOS"
    assert latest["sequence_number"] == 5

    # 2. AG Fallback attempts to write older or equal seq 5
    snap_fallback = pub.collect_snapshot()
    snap_fallback.writer = "AG_FALLBACK"
    snap_fallback.sequence_number = 5
    snap_fallback.runtime_health = "CORRUPTED"
    pub.publish_local(snap_fallback)

    # Must reject overwrite
    latest2 = json.loads((relay_dir / "LATEST.json").read_text(encoding="utf-8"))
    assert latest2["writer"] == "AOS"
    assert latest2["runtime_health"] != "CORRUPTED"


def test_remote_outbox_auth_unavailable(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    relay_dir = tmp_path / "controller-relay"
    config = {"runtime_root": str(tmp_path / "state")}
    pub = ControllerRelayPublisher(relay_dir, config)

    snap = pub.collect_snapshot()
    published = pub.publish_remote(snap)
    assert not published
    assert pub.remote_outbox_status == "HUMAN_REQUIRED_AUTH_SETUP"


def test_remote_outbox_throttling_and_major_gate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fakeMockTokenForRelayTesting12345678")

    relay_dir = tmp_path / "controller-relay"
    config = {"runtime_root": str(tmp_path / "state")}
    pub = ControllerRelayPublisher(relay_dir, config)

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"number": 42}).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        snap = pub.collect_snapshot()
        # 1. First routine publish succeeds
        pub.last_routine_remote_publish = 0.0
        success1 = pub.publish_remote(snap)
        assert success1
        assert pub.remote_outbox_status == "PUBLISHED"
        assert pub.remote_issue_number == 42

        # 2. Second routine publish immediately after is throttled (15 min interval)
        success2 = pub.publish_remote(snap)
        assert not success2

        # 3. Immediate publish triggers when major_gate_reason is provided
        success3 = pub.publish_remote(snap, major_gate_reason="RUNTIME_CANDIDATE_PROMOTION")
        assert success3
        assert pub.remote_outbox_status == "PUBLISHED"


def test_emit_cycle_heartbeat_and_checkpoint(tmp_path: Path):
    relay_dir = tmp_path / "controller-relay"
    config = {"runtime_root": str(tmp_path / "state")}
    pub = ControllerRelayPublisher(relay_dir, config)

    # Routine cycle
    snap1 = pub.emit_cycle(runtime_health_dict={"runtime_state": "HEALTHY", "pid": 100}, supervisor_pid=200)
    assert snap1.sequence_number == 1
    assert snap1.aos_heartbeat == "ALIVE"

    # Force checkpoint cycle
    snap2 = pub.emit_cycle(
        runtime_health_dict={"runtime_state": "HEALTHY", "pid": 100},
        supervisor_pid=200,
        force_checkpoint=True,
    )
    assert snap2.sequence_number == 2

    events = [json.loads(l) for l in (relay_dir / "events.jsonl").read_text("utf-8").strip().splitlines()]
    assert len(events) == 2
    assert events[0]["event_type"] == "HEARTBEAT"
    assert events[1]["event_type"] == "CHECKPOINT"


def test_operations_command_surface(tmp_path: Path):
    from aos.runtime_server import RuntimeEngine

    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    config = {
        "contract_version": "1.0.0",
        "runtime_root": str(runtime_root),
        "authorized_roots": [str(tmp_path)],
        "default_project": "lari",
        "controller_relay_dir": str(tmp_path / "relay"),
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "projects": {
            "lari": {
                "project_id": "lari",
                "descriptor_path": str(tmp_path / "desc.json"),
                "workspace": str(tmp_path / "ws"),
                "routing_policy_path": str(tmp_path / "policy.json"),
                "standing_authority": True,
            }
        },
    }
    engine = RuntimeEngine(config)
    try:
        # 1. pause-safe
        r_pause = engine.pause_safe()
        assert r_pause["status"] == "PAUSED_SAFE"
        assert engine.is_paused is True

        # 2. resume
        r_resume = engine.resume()
        assert r_resume["status"] == "RESUMED"
        assert engine.is_paused is False

        # 3. heartbeat-now
        r_hb = engine.trigger_relay(is_checkpoint=False, force_remote=False)
        assert r_hb["status"] == "RELAY_EMITTED"
        assert r_hb["is_checkpoint"] is False

        # 4. checkpoint-now
        r_cp = engine.trigger_relay(is_checkpoint=True, force_remote=False)
        assert r_cp["status"] == "RELAY_EMITTED"
        assert r_cp["is_checkpoint"] is True
    finally:
        engine.shutdown()


def test_operations_console_status(tmp_path: Path):
    from aos.control_panel import build_status

    relay_dir = Path("C:/Projects/AOS/.aos-runtime/controller-relay")
    config = {
        "runtime_root": str(tmp_path / "state"),
        "default_project": {},
    }
    status = build_status(config)
    assert "relay" in status
    assert "product_evidence" in status
    assert "alerts" in status
    assert "self_repair" in status
    assert status["self_repair"]["self_diagnosis_status"] == "SHADOW_ONLY"
    assert status["self_repair"]["self_repair_live_active"] is False


def test_truthful_human_required_and_lane_filtering(tmp_path: Path):
    relay_dir = tmp_path / "controller-relay"
    state_dir = tmp_path / "state" / "commands"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Historical terminal command with HUMAN_REQUIRED
    hist_dir = state_dir / "continue-hist-terminal"
    hist_dir.mkdir(parents=True, exist_ok=True)
    (hist_dir / "command.json").write_text(json.dumps({"project": {"project_id": "lari"}}), encoding="utf-8")
    (hist_dir / "state.json").write_text(json.dumps({
        "state": "HUMAN_REQUIRED",
        "completed_batch_count": 5,
        "attempts": 10,
    }), encoding="utf-8")

    # Active running command
    active_dir = state_dir / "continue-active-lane-a"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "command.json").write_text(json.dumps({"project": {"project_id": "lari"}}), encoding="utf-8")
    (active_dir / "state.json").write_text(json.dumps({
        "state": "RUNNING",
        "completed_batch_count": 12,
        "attempts": 15,
    }), encoding="utf-8")

    config = {"runtime_root": str(tmp_path / "state")}
    pub = ControllerRelayPublisher(relay_dir, config)
    snap = pub.collect_snapshot()

    # Truthful check: active lane is RUNNING, historical terminal command must NOT cause human_required=True
    assert snap.human_required is False
    assert snap.running_lane_count == 1
    assert snap.active_command_count == 1

    rendered = pub.render_markdown(snap)
    assert "continue-active-lane-a" in rendered
    assert "Historical Completed/Stopped Commands" in rendered


def test_provenance_requires_authoritative_git_head(tmp_path: Path):
    """Matching manifest/runtime SHA cannot produce PROVEN if authoritative Git HEAD is absent or different."""
    from aos.provenance import validate_exact_sha_provenance
    from aos.control_panel import build_status

    sha = "facd89d80e43d8e5301e212cf2438914526e48c4"
    diff_sha = "a95f40c8e047f1e26cd4ff296c96f4303c55f382"

    # 1. Authoritative Git HEAD is None -> UNPROVEN
    res_none = validate_exact_sha_provenance(
        local_git_head=None,
        candidate_manifest_source_sha=sha,
        runtime_source_sha=sha,
        build_source_sha=sha,
    )
    assert res_none.valid is False
    assert res_none.status != "PROVEN"

    # 2. Authoritative Git HEAD is different -> FAIL
    res_diff = validate_exact_sha_provenance(
        local_git_head=diff_sha,
        candidate_manifest_source_sha=sha,
        runtime_source_sha=sha,
        build_source_sha=sha,
    )
    assert res_diff.valid is False
    assert res_diff.status == "FAIL"

    # 3. In controller relay collect_snapshot with no git repo accessible
    relay_dir = tmp_path / "controller-relay"
    config = {
        "runtime_root": str(tmp_path / "nonexistent-root"),
        "authoritative_repo_path": str(tmp_path / "nonexistent-repo"),
        "authorized_roots": [],
        "candidate_source_sha": sha,
    }
    pub = ControllerRelayPublisher(relay_dir, config)
    snap = pub.collect_snapshot(runtime_health_dict={"runtime_source_sha": sha, "runtime_state": "HEALTHY"})
    # Must NOT produce PROVEN when git checkout cannot be queried
    assert snap.provenance_status != "PROVEN"


def test_integrity_telemetry_missing_evidence_is_unknown(tmp_path: Path):
    """Missing integrity evidence must emit UNKNOWN, never hardcoded zero."""
    relay_dir = tmp_path / "controller-relay"
    config = {"runtime_root": str(tmp_path / "state")}
    pub = ControllerRelayPublisher(relay_dir, config)

    snap = pub.collect_snapshot()
    assert snap.duplicate_completed_work == "UNKNOWN"
    assert snap.lost_accepted_work == "UNKNOWN"
    assert snap.cross_lane_write_scope_collision == "UNKNOWN"

    md = pub.render_markdown(snap)
    assert "DUPLICATE_COMPLETED_WORK=UNKNOWN" in md
    assert "LOST_ACCEPTED_WORK=UNKNOWN" in md
    assert "CROSS_LANE_WRITE_SCOPE_COLLISION=UNKNOWN" in md


def test_product_mutation_semantics_markdown_not_ui_mutation(tmp_path: Path):
    """visual_productization_status.md is a productization artifact, NOT a user-facing UI mutation."""
    relay_dir = tmp_path / "controller-relay"
    state_dir = tmp_path / "state" / "commands" / "continue-lane-a"
    state_dir.mkdir(parents=True, exist_ok=True)
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir(parents=True, exist_ok=True)

    # Only markdown productization status written
    (ws_dir / "visual_productization_status.md").write_text("# Visual Productization\n", encoding="utf-8")

    (state_dir / "command.json").write_text(json.dumps({
        "project": {"project_id": "lari", "workspace": str(ws_dir)}
    }), encoding="utf-8")
    (state_dir / "state.json").write_text(json.dumps({
        "state": "RUNNING",
        "completed_batch_count": 1,
    }), encoding="utf-8")

    config = {"runtime_root": str(tmp_path / "state")}
    pub = ControllerRelayPublisher(relay_dir, config)
    snap = pub.collect_snapshot()

    assert snap.first_workspace_productization_artifact == "visual_productization_status.md"
    assert snap.first_user_facing_ui_mutation is None
    assert snap.browser_evidence_status == "AWAITING_BROWSER_SUITE_RUN"
    assert snap.responsive_evidence_status == "AWAITING_BROWSER_SUITE_RUN"

    md = pub.render_markdown(snap)
    assert "FIRST_WORKSPACE_PRODUCTIZATION_ARTIFACT=visual_productization_status.md" in md
    assert "FIRST_USER_FACING_UI_MUTATION=NONE" in md
    assert "BROWSER_EVIDENCE_STATUS=AWAITING_BROWSER_SUITE_RUN" in md
    assert "RESPONSIVE_EVIDENCE_STATUS=AWAITING_BROWSER_SUITE_RUN" in md


def test_forward_progress_active_without_delta(tmp_path: Path):
    """Running workers without meaningful delta must emit FORWARD_PROGRESS=NO and ACTIVE_WITHOUT_MEANINGFUL_DELTA."""
    relay_dir = tmp_path / "controller-relay"
    state_dir = tmp_path / "state" / "commands" / "continue-lane-a"
    state_dir.mkdir(parents=True, exist_ok=True)

    (state_dir / "command.json").write_text(json.dumps({"project": {"project_id": "lari"}}), encoding="utf-8")
    (state_dir / "state.json").write_text(json.dumps({
        "state": "RUNNING",
        "completed_batch_count": 10,
    }), encoding="utf-8")

    config = {"runtime_root": str(tmp_path / "state")}
    pub = ControllerRelayPublisher(relay_dir, config)

    # First cycle records completed_batches = 10
    snap1 = pub.collect_snapshot()
    assert snap1.forward_progress == "NO"
    assert snap1.no_progress_reason == "ACTIVE_WITHOUT_MEANINGFUL_DELTA"

    # Second cycle with no change in completed_batches
    snap2 = pub.collect_snapshot()
    assert snap2.running_lane_count == 1
    assert snap2.completed_batch_delta == 0
    assert snap2.forward_progress == "NO"
    assert snap2.no_progress_reason == "ACTIVE_WITHOUT_MEANINGFUL_DELTA"

    # Third cycle where completed_batches advances to 11
    (state_dir / "state.json").write_text(json.dumps({
        "state": "RUNNING",
        "completed_batch_count": 11,
    }), encoding="utf-8")
    snap3 = pub.collect_snapshot()
    assert snap3.completed_batch_delta == 1
    assert snap3.forward_progress == "YES"
    assert snap3.no_progress_reason is None


