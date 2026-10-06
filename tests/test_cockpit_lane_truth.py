"""Comprehensive tests for AOS Cockpit and Product Lane Truth & Authority.

Covers all 16 required invariants from CONTROLLER_AUTHORITY_ID=LARI-AOS-WP0-WP1-RUNTIME-COCKPIT-TRUTH-20261006-01:
1. AOS maintenance excluded from product lane count.
2. Only lari + lari-ui-v2 appear in product activity.
3. lari continues to consume global canonical next_action.
4. lari-ui-v2 CANNOT consume global Node3 next_action.
5. Missing explicit UI-V2 canonical lane authority => CANONICAL_LANE_AUTHORITY_MISSING / HOLD.
6. Explicit future UI-V2 lane authority can be consumed correctly.
7. Historical hardcoded command IDs are not required for downstream activation.
8. Superseded command is never reactivated.
9. Current command beats older failed/completed command.
10. Old failed UI command does not create current System Attention if a newer current command exists.
11. No hardcoded 25/18 batch baseline remains.
12. Batch increase alone does not count as meaningful product progress.
13. Candidate SHA / CI / KCP transition DOES count as meaningful progress.
14. Evidence ladder does not label staging/live PROVEN without evidence.
15. Loopback-only server invariant remains enforced.
16. Local mutation endpoint authentication remains enforced.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from aos import control_panel
from aos.cross_lane_coordinator import DOWNSTREAM_LANES, evaluate_downstream_gates
from aos.runtime_admission import AdmissionRecord, AdmissionState, CommandAdmissionStore
from aos.source_adapter import ProjectSourceAdapter


def test_1_and_2_maintenance_excluded_and_only_product_lanes_appear(tmp_path: Path, monkeypatch):
    """1. AOS maintenance excluded from product lane count.
    2. Only lari + lari-ui-v2 appear in product activity.
    """
    state_root = tmp_path / "runtime" / "state"
    commands = state_root / "commands"
    commands.mkdir(parents=True)

    # 3 commands in store: aos-maintenance (running), lari (hold), lari-ui-v2 (hold)
    for cid, proj, state in [
        ("continue-maint-001", "aos-maintenance", "RUNNING"),
        ("continue-lari-001", "lari", "HOLD"),
        ("continue-uiv2-001", "lari-ui-v2", "HOLD"),
    ]:
        cdir = commands / cid
        cdir.mkdir(parents=True)
        (cdir / "command.json").write_text(json.dumps({"project": {"project_id": proj}}), encoding="utf-8")
        (cdir / "state.json").write_text(json.dumps({"state": state, "completed_batch_count": 10}), encoding="utf-8")

    monkeypatch.setattr(control_panel, "runtime_configured", lambda _cfg: True)
    monkeypatch.setattr(
        control_panel,
        "runtime_status",
        lambda _cfg: {
            "host_state": "HEALTHY",
            "runtime_v1": {
                "runtime_state": "HEALTHY",
                "active_commands": ["continue-maint-001"],
                "waiting_commands": [],
                "latest_command": {"command_id": "continue-lari-001"},
            },
        },
    )

    status = control_panel.build_status({"runtime_root": str(state_root)})

    # Product lanes contains strictly product identities
    assert set(status["lanes"].keys()) == {"lari", "lari-ui-v2"}
    assert "aos-maintenance" not in status["lanes"]

    # Platform operations captures maintenance separately
    assert "platform_operations" in status
    assert status["platform_operations"]["maintenance_lane"]["command_id"] == "continue-maint-001"


def test_3_lari_consumes_global_next_action():
    """3. lari continues to consume global canonical next_action."""
    adapter = ProjectSourceAdapter("MertSGI/Randapp-main", "control/main")
    state_content = json.dumps({
        "current_status": "PHASE_7_NODE_3_R2",
        "next_action": "Phase 7 Node 3 Favorites & Fast Rebooking R2",
        "current_milestone": "Phase 7 Node 3",
    })
    snap = adapter.build_normalized_snapshot(
        "lari",
        "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
        {"state": state_content},
        {},
    )

    assert snap["canonical_next_action"] == "Phase 7 Node 3 Favorites & Fast Rebooking R2"
    assert not any("CANONICAL_LANE_AUTHORITY_MISSING" in r for r in snap["ambiguity_reasons"])


def test_4_and_5_uiv2_cannot_consume_global_next_action_and_fails_closed_when_missing():
    """4. lari-ui-v2 CANNOT consume global Node3 next_action.
    5. Missing explicit UI-V2 canonical lane authority => CANONICAL_LANE_AUTHORITY_MISSING / HOLD.
    """
    adapter = ProjectSourceAdapter("MertSGI/Randapp-main", "control/main")
    state_content = json.dumps({
        "current_status": "PHASE_7_NODE_3_R2",
        "next_action": "Phase 7 Node 3 Favorites & Fast Rebooking R2",
        "current_milestone": "Phase 7 Node 3",
    })
    snap = adapter.build_normalized_snapshot(
        "lari-ui-v2",
        "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
        {"state": state_content},
        {},
    )

    # Never consumes global next_action
    assert snap["canonical_next_action"] != "Phase 7 Node 3 Favorites & Fast Rebooking R2"
    assert snap["canonical_next_action"] == "CANONICAL_LANE_AUTHORITY_MISSING"
    assert snap["current_status"] == "CANONICAL_LANE_AUTHORITY_MISSING"
    assert snap["has_ambiguity"] is True
    assert any("CANONICAL_LANE_AUTHORITY_MISSING" in r for r in snap["ambiguity_reasons"])


def test_6_uiv2_consumes_structured_canonical_lane_authority():
    """6. Explicit future UI-V2 lane authority can be consumed correctly."""
    adapter = ProjectSourceAdapter("MertSGI/Randapp-main", "control/main")
    state_content = json.dumps({
        "current_status": "PHASE_7_NODE_3_R2",
        "next_action": "Phase 7 Node 3 Favorites & Fast Rebooking R2",
        "parallel_execution_lanes": {
            "ui_v2": {
                "status": "READY",
                "authority": "CANONICAL_DUAL_LANE",
                "gate": "P7N2-DISCOVERY-MARKETPLACE_R2",
                "execution_base_sha": "d" * 40,
                "objective": "Discovery Marketplace UI-V2 Productization",
                "next_action": "Implement UI-V2 Design Polish",
            }
        }
    })
    snap = adapter.build_normalized_snapshot(
        "lari-ui-v2",
        "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
        {"state": state_content},
        {},
    )

    assert snap["canonical_next_action"] == "Discovery Marketplace UI-V2 Productization"
    assert snap["current_status"] == "READY"
    assert not any("CANONICAL_LANE_AUTHORITY_MISSING" in r for r in snap["ambiguity_reasons"])


def test_7_historical_command_id_not_required_for_downstream_activation(tmp_path: Path):
    """7. Historical hardcoded command IDs are not required for downstream activation."""
    store = CommandAdmissionStore(tmp_path)
    # Use an entirely new arbitrary command ID for lari-ui-v2
    new_cmd_id = "continue-new-ui-v2-arbitrary-12345"
    cdir = tmp_path / "commands" / new_cmd_id
    cdir.mkdir(parents=True)
    (cdir / "command.json").write_text(json.dumps({"project": {"project_id": "lari-ui-v2"}}), encoding="utf-8")
    (cdir / "state.json").write_text(json.dumps({"state": "HOLD"}), encoding="utf-8")

    control_pc = tmp_path / "control" / "docs" / "project-control"
    control_pc.mkdir(parents=True)
    (control_pc / "STATE.json").write_text(json.dumps({
        "current_status": "PHASE_7_NODE_2_DISCOVERY_MARKETPLACE_R3_BOUND_READY",
        "current_milestone": "Program V2 Phase 7",
        "phase7_node2_contract": {
            "delivery_slices": {"R1": "ACCEPTED_PROVEN", "R2": "ACCEPTED_PROVEN"},
        },
        "phase7_accepted_execution_chain": {"node2_r2": "b" * 40},
        "accepted_gates": [{
            "gate": "P7N2-DISCOVERY-MARKETPLACE_R2",
            "status": "CLOSED_PROVEN",
            "tested_sha": "b" * 40,
        }],
        "parallel_execution_lanes": {
            "ui_v2": {
                "objective": "Arbitrary UI V2 Objective",
                "status": "READY",
            }
        },
    }), encoding="utf-8")

    activated = evaluate_downstream_gates(tmp_path / "control", store)
    assert len(activated) == 1
    assert activated[0].command_id == new_cmd_id
    assert activated[0].state == AdmissionState.ACTIVE.value


def test_8_superseded_command_is_never_reactivated_or_selected(tmp_path: Path, monkeypatch):
    """8. Superseded command is never reactivated and never selected as current."""
    state_root = tmp_path / "runtime" / "state"
    commands = state_root / "commands"
    commands.mkdir(parents=True)

    # Admission store has command as SUPERSEDED
    adm_file = state_root / "command-admission.json"
    adm_file.write_text(json.dumps({
        "contract_version": "1.0.0",
        "records": {
            "continue-superseded-cmd": {
                "command_id": "continue-superseded-cmd",
                "project_id": "lari",
                "state": "SUPERSEDED",
                "reason": "superseded by new",
                "authority": "TEST",
                "updated_at": "2026-10-06T12:00:00Z",
            }
        }
    }), encoding="utf-8")

    cdir = commands / "continue-superseded-cmd"
    cdir.mkdir(parents=True)
    (cdir / "command.json").write_text(json.dumps({"project": {"project_id": "lari"}}), encoding="utf-8")
    (cdir / "state.json").write_text(json.dumps({"state": "RUNNING", "updated_at": "2026-10-06T12:00:00Z"}), encoding="utf-8")

    # Downstream gate check
    store = CommandAdmissionStore(state_root)
    activated = evaluate_downstream_gates(tmp_path, store)
    assert len(activated) == 0

    # Cockpit selection check
    monkeypatch.setattr(control_panel, "runtime_configured", lambda _cfg: True)
    monkeypatch.setattr(control_panel, "runtime_status", lambda _cfg: {
        "host_state": "HEALTHY",
        "runtime_v1": {"runtime_state": "HEALTHY", "active_commands": [], "waiting_commands": []},
    })
    status = control_panel.build_status({"runtime_root": str(state_root)})
    assert "lari" not in status["lanes"]


def test_9_current_command_beats_older_failed_or_completed_command(tmp_path: Path, monkeypatch):
    """9. Current command beats older failed/completed command."""
    state_root = tmp_path / "runtime" / "state"
    commands = state_root / "commands"
    commands.mkdir(parents=True)

    # Command 1: older completed command
    c1 = commands / "continue-old-completed"
    c1.mkdir(parents=True)
    (c1 / "command.json").write_text(json.dumps({"project": {"project_id": "lari"}, "created_at": "2026-10-01T10:00:00Z"}), encoding="utf-8")
    (c1 / "state.json").write_text(json.dumps({"state": "PROJECT_COMPLETE", "updated_at": "2026-10-01T12:00:00Z"}), encoding="utf-8")

    # Command 2: newer active/hold command
    c2 = commands / "continue-new-hold"
    c2.mkdir(parents=True)
    (c2 / "command.json").write_text(json.dumps({"project": {"project_id": "lari"}, "created_at": "2026-10-05T10:00:00Z"}), encoding="utf-8")
    (c2 / "state.json").write_text(json.dumps({"state": "HOLD", "updated_at": "2026-10-05T12:00:00Z"}), encoding="utf-8")

    monkeypatch.setattr(control_panel, "runtime_configured", lambda _cfg: True)
    monkeypatch.setattr(control_panel, "runtime_status", lambda _cfg: {
        "host_state": "HEALTHY",
        "runtime_v1": {"runtime_state": "HEALTHY", "active_commands": [], "waiting_commands": []},
    })
    status = control_panel.build_status({"runtime_root": str(state_root)})

    assert status["lanes"]["lari"]["command_id"] == "continue-new-hold"
    assert status["lanes"]["lari"]["state"] == "HOLD"


def test_10_old_failed_command_does_not_create_system_attention_when_newer_exists(tmp_path: Path, monkeypatch):
    """10. Old failed UI command does not create current System Attention if a newer current command exists."""
    state_root = tmp_path / "runtime" / "state"
    commands = state_root / "commands"
    commands.mkdir(parents=True)

    # Older failed UI command
    c1 = commands / "continue-old-failed"
    c1.mkdir(parents=True)
    (c1 / "command.json").write_text(json.dumps({"project": {"project_id": "lari-ui-v2"}, "created_at": "2026-09-01T10:00:00Z"}), encoding="utf-8")
    (c1 / "state.json").write_text(json.dumps({
        "state": "FAILED",
        "failure_class": "RUNTIME_EXECUTION_FAILURE",
        "updated_at": "2026-09-01T11:00:00Z"
    }), encoding="utf-8")

    # Newer hold command
    c2 = commands / "continue-new-held"
    c2.mkdir(parents=True)
    (c2 / "command.json").write_text(json.dumps({"project": {"project_id": "lari-ui-v2"}, "created_at": "2026-10-06T10:00:00Z"}), encoding="utf-8")
    (c2 / "state.json").write_text(json.dumps({
        "state": "HOLD",
        "updated_at": "2026-10-06T11:00:00Z"
    }), encoding="utf-8")

    monkeypatch.setattr(control_panel, "runtime_configured", lambda _cfg: True)
    monkeypatch.setattr(control_panel, "runtime_status", lambda _cfg: {
        "host_state": "HEALTHY",
        "runtime_v1": {"runtime_state": "HEALTHY", "active_commands": [], "waiting_commands": []},
    })
    status = control_panel.build_status({"runtime_root": str(state_root)})

    assert status["lanes"]["lari-ui-v2"]["command_id"] == "continue-new-held"
    # No LANE_FAILED alert in current alerts because the current command is HOLD, not FAILED
    assert not any("LANE_FAILED" in a for a in status["alerts"])


def test_11_and_12_no_batch_baseline_and_batch_increase_alone_is_not_meaningful_progress(tmp_path: Path, monkeypatch):
    """11. No hardcoded 25/18 batch baseline remains.
    12. Batch increase alone does not count as meaningful product progress.
    """
    state_root = tmp_path / "runtime" / "state"
    commands = state_root / "commands"
    commands.mkdir(parents=True)

    c = commands / "continue-lari-01"
    c.mkdir(parents=True)
    (c / "command.json").write_text(json.dumps({"project": {"project_id": "lari"}}), encoding="utf-8")
    # Batch count jumped from 25 to 100, but no meaningful progress timestamp or task transition recorded
    (c / "state.json").write_text(json.dumps({
        "state": "RUNNING",
        "completed_batch_count": 100,
        "updated_at": None,
    }), encoding="utf-8")

    monkeypatch.setattr(control_panel, "runtime_configured", lambda _cfg: True)
    monkeypatch.setattr(control_panel, "runtime_status", lambda _cfg: {
        "host_state": "HEALTHY",
        "runtime_v1": {"runtime_state": "HEALTHY", "active_commands": ["continue-lari-01"], "waiting_commands": []},
    })
    status = control_panel.build_status({"runtime_root": str(state_root)})

    # Baseline delta structure is deleted from product evidence
    assert "meaningful_batch_delta" not in status.get("product_evidence", {})
    # Since only batch count increased without semantic milestone, surfaces NO_MEANINGFUL_PROGRESS
    assert any("NO_MEANINGFUL_PROGRESS" in a for a in status["alerts"])


def test_13_semantic_candidate_ci_transition_counts_as_meaningful_progress(tmp_path: Path):
    """13. Candidate SHA / CI / KCP transition DOES count as meaningful progress."""
    cdir = tmp_path / "cmd1"
    cdir.mkdir(parents=True)
    prun = cdir / "project-runtime"
    prun.mkdir(parents=True)

    state = {
        "completed_batch_count": 5,
        "candidate_sha": "a" * 40,
        "ci_status": "SUCCESS",
        "kcp_implementation_status": "PROVEN",
        "updated_at": "2026-10-06T12:00:00Z",
    }
    cmd = {"goal": "Test Semantic Progress"}
    work = control_panel._command_work(cdir, state, cmd)

    assert work["CANDIDATE_SHA"] == "a" * 40
    assert work["CI_STATUS"] == "SUCCESS"
    assert work["KCP_IMPLEMENTATION_STATUS"] == "PROVEN"
    assert work["LAST_MEANINGFUL_PROGRESS_AT"] == "2026-10-06T12:00:00Z"


def test_14_evidence_ladder_does_not_label_staging_or_live_proven_without_evidence(tmp_path: Path, monkeypatch):
    """14. Evidence ladder does not label staging/live PROVEN without evidence."""
    state_root = tmp_path / "runtime" / "state"
    commands = state_root / "commands"
    commands.mkdir(parents=True)

    monkeypatch.setattr(control_panel, "runtime_configured", lambda _cfg: True)
    monkeypatch.setattr(control_panel, "runtime_status", lambda _cfg: {
        "host_state": "HEALTHY",
        "runtime_v1": {"runtime_state": "HEALTHY", "active_commands": [], "waiting_commands": []},
    })
    status = control_panel.build_status({"runtime_root": str(state_root)})

    ladder = status["evidence_ladder"]
    assert ladder["PRODUCTION"] == "NO_GO"
    assert ladder["STAGING_WEB"] != "PROVEN"
    assert ladder["STAGING_DB"] != "PROVEN"


def test_15_and_16_loopback_only_and_mutation_endpoint_auth(tmp_path: Path):
    """15. Loopback-only server invariant remains enforced.
    16. Local mutation endpoint authentication remains enforced.
    """
    import inspect
    serve_src = inspect.getsource(control_panel.serve)
    # Proves server binds to 127.0.0.1
    assert '("127.0.0.1", port)' in serve_src
    assert '0.0.0.0' not in serve_src

    # Handler verifies X-AOS-Panel-Token
    post_src = inspect.getsource(control_panel._Handler.do_POST)
    assert 'self.headers.get("X-AOS-Panel-Token") != self.token' in post_src
    assert 'HTTPStatus.FORBIDDEN' in post_src
