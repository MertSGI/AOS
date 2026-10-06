"""Comprehensive tests for Canonical Lane Scope and Shared-Path Enforcement.

Test Matrix requirements:
- canonical allowed paths are projected into execution authority
- FILE task outside canonical lane scope rejected
- AGENTIC task outside canonical lane scope rejected
- in-scope mutation accepted
- BookingPage mutation by lari accepted while lari owns it
- BookingPage mutation by ui-v2 rejected while lari owns it
- explicit valid handoff can transfer ownership
- stale authority revision rejected
- absence of canonical lane scope fails closed for mutating product work
- read-only work can remain inspectable without mutation permission
- UI-V2 still cannot inherit global Node3 objective
- product lane count remains two
- production remains NO_GO
- paid fallback remains disabled
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from aos.execution_authority import validate_execution_authority
from aos.planning_kernel import (
    AuthorityDenied,
    AuthorityRecord,
    CanonicalAuthorityResolver,
    Objective,
    PlanningKernelError,
    ProjectSituation,
    _validate_plan_shape,
)
from aos.source_adapter import ProjectSourceAdapter


def build_c4_state_json() -> Dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "current_status": "PHASE_7_NODE_3_FAVORITES_REBOOKING_R2_PRODUCT_INTEGRATION_AUTHORIZED",
        "current_milestone": "P7N3-FAVORITES-FAST-REBOOKING-R2",
        "next_action": "Implement bounded Phase 7 Node 3 Favorites & Fast Rebooking R2 Product Integration from accepted R1 execution base a8740a4a252c83060e8eaad28f49f58140c0ee5a.",
        "next_action_execution_base_sha": "a8740a4a252c83060e8eaad28f49f58140c0ee5a",
        "production_status": "NO_GO",
        "paid_fallback": "DISABLED",
        "parallel_execution_lanes": {
            "lari": {
                "project_id": "lari",
                "lane_id": "product",
                "status": "READY_POST_CONVERGENCE",
                "authority": "DECISION-024",
                "authority_revision": "20261007-02",
                "execution_base_sha": "a8740a4a252c83060e8eaad28f49f58140c0ee5a",
                "objective": "Complete canonically authorized Phase 7 Node 3 R2 Favorites & Fast Rebooking Product Integration under DECISION-024 and current canonical control.",
                "gate": "P7N3-FAVORITES-FAST-REBOOKING-R2",
                "production": "NO_GO",
                "allowed_scope": [
                    "pages/AppointmentSelfServicePage.tsx",
                    "pages/BookingPage.tsx",
                    "pages/customer/",
                    "services/appointmentSelfServiceService.ts",
                    "services/repositories/supabaseBookingRepository.ts",
                    "scripts/test-phase7-node3-favorites-rebooking-contracts.mjs",
                    "supabase/tests/program-v2/phase7-live/test-phase7-node3-favorites-fast-rebooking-behavioral-matrix.mjs",
                ],
                "hold_reason": None,
            },
            "ui_v2": {
                "project_id": "lari-ui-v2",
                "lane_id": "ui_v2",
                "status": "READY_POST_CONVERGENCE",
                "authority": "DECISION-024",
                "authority_revision": "20261007-02",
                "execution_base_sha": "a8740a4a252c83060e8eaad28f49f58140c0ee5a",
                "objective": "Continue LARI UI-V2 productization from the current accepted product frontier, preserving valid historical UI productization while adapting it to current canonical server-authoritative contracts.",
                "gate": "LARI-UI-V2-PRODUCTIZATION",
                "production": "NO_GO",
                "allowed_scope": [
                    "components/SalonWebsiteViewV2.tsx",
                    "components/layouts/MarketingLayout.tsx",
                    "components/layouts/SalonBookingLayout.tsx",
                    "pages/MarketingHomePage.tsx",
                    "services/tenantService.ts",
                    "src/components/DiscoveryPortfolio.tsx",
                    "src/pages/DiscoveryMarketplace.tsx",
                ],
                "hold_reason": None,
            },
        },
        "shared_path_governance": {
            "contract_version": "1.0.0",
            "status": "ENFORCED",
            "paths": {
                "pages/BookingPage.tsx": {
                    "owner_lane": "lari",
                    "state": "EXCLUSIVE",
                    "handoff_required": True,
                    "handoff_condition": "PHASE7_NODE3_R2_ACCEPTED_OR_EXPLICIT_CONTROLLER_HANDOFF",
                }
            },
        },
    }


def make_situation(
    project_id: str,
    lane_allowed_scope: list[str] | None = None,
    authority_revision: str = "20261007-02",
    shared_path_governance: dict[str, Any] | None = None,
) -> ProjectSituation:
    auth_rec = AuthorityRecord(
        authority_id="DECISION-024",
        source_path="DECISIONS.md",
        text="DECISION-024 Standing Authority for Program V2 non-production",
        superseded=False,
        production_allowed=False,
    )
    return ProjectSituation(
        schema_version="1.0.0",
        project_id=project_id,
        repository="MertSGI/Randapp-main",
        control_ref="control/lari-project-control-plane",
        control_sha="49c138f944252dba7e4c42436ff6dd735f777902",
        repository_head="a8740a4a252c83060e8eaad28f49f58140c0ee5a",
        execution_base_sha="a8740a4a252c83060e8eaad28f49f58140c0ee5a",
        current_status="READY",
        current_milestone="P7N3-FAVORITES-FAST-REBOOKING-R2",
        canonical_next_action="Action",
        canonical_hashes={},
        canonical_excerpt="",
        working_tree_state="CLEAN",
        ci_state=[],
        accepted_gates=[],
        blocked_gates=[],
        authority_records={"DECISION-024": auth_rec},
        goal="Integration",
        constraints=(),
        red_lines=(),
        completion_criteria=(),
        ambiguity_reasons=(),
        captured_at="2026-10-06T20:00:00Z",
        authority_revision=authority_revision,
        lane_allowed_scope=tuple(lane_allowed_scope) if lane_allowed_scope is not None else None,
        shared_path_governance=shared_path_governance,
    )


def test_1_canonical_allowed_paths_projected_into_execution_authority():
    """Canonical allowed paths in STATE.json project accurately into snapshots for both lanes."""
    adapter = ProjectSourceAdapter("MertSGI/Randapp-main", "control/main")
    state_content = json.dumps(build_c4_state_json())

    # Snapshot for lari lane
    snap_lari = adapter.build_normalized_snapshot(
        "lari",
        "49c138f944252dba7e4c42436ff6dd735f777902",
        {"state": state_content},
        {},
    )
    assert snap_lari["authority_revision"] == "20261007-02"
    assert "pages/BookingPage.tsx" in snap_lari["lane_allowed_scope"]
    assert "services/repositories/supabaseBookingRepository.ts" in snap_lari["lane_allowed_scope"]
    assert snap_lari["shared_path_governance"]["paths"]["pages/BookingPage.tsx"]["owner_lane"] == "lari"

    # Snapshot for lari-ui-v2 lane
    snap_ui = adapter.build_normalized_snapshot(
        "lari-ui-v2",
        "49c138f944252dba7e4c42436ff6dd735f777902",
        {"state": state_content},
        {},
    )
    assert snap_ui["authority_revision"] == "20261007-02"
    assert "components/SalonWebsiteViewV2.tsx" in snap_ui["lane_allowed_scope"]
    assert "src/pages/DiscoveryMarketplace.tsx" in snap_ui["lane_allowed_scope"]
    assert snap_ui["shared_path_governance"]["paths"]["pages/BookingPage.tsx"]["owner_lane"] == "lari"


def test_2_file_task_outside_canonical_lane_scope_rejected():
    """Mutating FILE task targeting outside canonical lane allowed scope is rejected."""
    c4 = build_c4_state_json()
    lari_scope = c4["parallel_execution_lanes"]["lari"]["allowed_scope"]
    sit = make_situation("lari", lane_allowed_scope=lari_scope)

    objective = Objective(
        objective_id="OBJ-01",
        title="Node3 R2",
        description="Node3 R2 Favorites & Fast Rebooking",
        authority_id="DECISION-024",
        risk_class="R1",
        rationale="Authorized",
        scope_tags=("LARI",),
        completion_criteria=("tests pass",),
        parallel_candidates=(),
    )

    plan = {
        "schema_version": "1.0.0",
        "objective_id": "OBJ-01",
        "tasks": [
            {
                "node_id": "TASK-OUTSIDE-SCOPE",
                "run_type": "FILE",
                "mutating": True,
                "authority_id": "DECISION-024",
                "risk_class": "R1",
                "write_scope": ["src/unauthorized_subsystem/foo.ts"],
                "dependencies": [],
                "scope_tags": ["LARI"],
                "payload": {"action": "write_file", "path": "src/unauthorized_subsystem/foo.ts", "content": "// unauthorized\n"},
            }
        ],
    }

    with pytest.raises(PlanningKernelError, match="CANONICAL_LANE_SCOPE_VIOLATION"):
        _validate_plan_shape(plan, objective, sit)


def test_3_agentic_task_outside_canonical_lane_scope_rejected():
    """Mutating AGENTIC task targeting outside canonical lane allowed scope is rejected."""
    c4 = build_c4_state_json()
    lari_scope = c4["parallel_execution_lanes"]["lari"]["allowed_scope"]
    sit = make_situation("lari", lane_allowed_scope=lari_scope)

    objective = Objective(
        objective_id="OBJ-01",
        title="Node3 R2",
        description="Node3 R2 Favorites & Fast Rebooking",
        authority_id="DECISION-024",
        risk_class="R1",
        rationale="Authorized",
        scope_tags=("LARI",),
        completion_criteria=("tests pass",),
        parallel_candidates=(),
    )

    plan = {
        "schema_version": "1.0.0",
        "objective_id": "OBJ-01",
        "tasks": [
            {
                "node_id": "TASK-AGENTIC-OUTSIDE",
                "run_type": "AGENTIC",
                "mutating": True,
                "authority_id": "DECISION-024",
                "risk_class": "R1",
                "write_scope": ["components/SalonWebsiteViewV2.tsx"],  # UI-V2 path, outside LARI lane
                "dependencies": [],
                "scope_tags": ["LARI"],
                "payload": {"prompt": "modify something"},
                "expected_artifacts": ["components/SalonWebsiteViewV2.tsx"],
            }
        ],
    }

    with pytest.raises(PlanningKernelError, match="CANONICAL_LANE_SCOPE_VIOLATION"):
        _validate_plan_shape(plan, objective, sit)


def test_4_in_scope_mutation_accepted():
    """In-scope mutation tasks pass planning validation."""
    c4 = build_c4_state_json()
    lari_scope = c4["parallel_execution_lanes"]["lari"]["allowed_scope"]
    shared_gov = c4["shared_path_governance"]
    sit = make_situation("lari", lane_allowed_scope=lari_scope, shared_path_governance=shared_gov)

    objective = Objective(
        objective_id="OBJ-01",
        title="Node3 R2",
        description="Node3 R2 Favorites & Fast Rebooking",
        authority_id="DECISION-024",
        risk_class="R1",
        rationale="Authorized",
        scope_tags=("LARI",),
        completion_criteria=("tests pass",),
        parallel_candidates=(),
    )

    plan = {
        "schema_version": "1.0.0",
        "objective_id": "OBJ-01",
        "tasks": [
            {
                "node_id": "TASK-IN-SCOPE",
                "run_type": "FILE",
                "mutating": True,
                "authority_id": "DECISION-024",
                "risk_class": "R1",
                "write_scope": ["services/appointmentSelfServiceService.ts"],
                "dependencies": [],
                "scope_tags": ["LARI"],
                "payload": {"action": "write_file", "path": "services/appointmentSelfServiceService.ts", "content": "// test content\n"},
            }
        ],
    }

    norm = _validate_plan_shape(plan, objective, sit)
    assert len(norm["tasks"]) == 1


def test_5_booking_page_mutation_by_lari_accepted_while_lari_owns_it():
    """pages/BookingPage.tsx mutation by lari is accepted because lari is the recorded owner."""
    c4 = build_c4_state_json()
    lari_scope = c4["parallel_execution_lanes"]["lari"]["allowed_scope"]
    shared_gov = c4["shared_path_governance"]
    sit = make_situation("lari", lane_allowed_scope=lari_scope, shared_path_governance=shared_gov)

    resolver = CanonicalAuthorityResolver(sit)
    task = {
        "node_id": "TASK-LARI-BOOKING",
        "authority_id": "DECISION-024",
        "risk_class": "R1",
        "mutating": True,
        "write_scope": ["pages/BookingPage.tsx"],
    }
    # Should not raise
    resolver.validate_task(task)


def test_6_booking_page_mutation_by_uiv2_rejected_while_lari_owns_it():
    """pages/BookingPage.tsx mutation by ui-v2 is rejected with SHARED_PATH_OWNERSHIP_REQUIRED while lari owns it."""
    c4 = build_c4_state_json()
    ui_scope = c4["parallel_execution_lanes"]["ui_v2"]["allowed_scope"] + ["pages/BookingPage.tsx"]
    shared_gov = c4["shared_path_governance"]
    sit = make_situation("lari-ui-v2", lane_allowed_scope=ui_scope, shared_path_governance=shared_gov)

    resolver = CanonicalAuthorityResolver(sit)
    task = {
        "node_id": "TASK-UIV2-BOOKING",
        "authority_id": "DECISION-024",
        "risk_class": "R1",
        "mutating": True,
        "write_scope": ["pages/BookingPage.tsx"],
    }

    with pytest.raises(AuthorityDenied, match="SHARED_PATH_OWNERSHIP_REQUIRED"):
        resolver.validate_task(task)


def test_7_explicit_valid_handoff_can_transfer_ownership():
    """With an explicit valid handoff, ui-v2 may mutate BookingPage."""
    c4 = build_c4_state_json()
    ui_scope = c4["parallel_execution_lanes"]["ui_v2"]["allowed_scope"] + ["pages/BookingPage.tsx"]
    shared_gov = c4["shared_path_governance"]
    sit = make_situation("lari-ui-v2", lane_allowed_scope=ui_scope, shared_path_governance=shared_gov)

    resolver = CanonicalAuthorityResolver(sit)
    task = {
        "node_id": "TASK-UIV2-HANDOFF",
        "authority_id": "DECISION-024",
        "risk_class": "R1",
        "mutating": True,
        "write_scope": ["pages/BookingPage.tsx"],
        "shared_path_handoff": {
            "valid": True,
            "condition": "PHASE7_NODE3_R2_ACCEPTED_OR_EXPLICIT_CONTROLLER_HANDOFF",
            "from_lane": "lari",
            "to_lane": "ui_v2",
        },
    }

    # Should succeed with valid handoff
    resolver.validate_task(task)


def test_8_stale_authority_revision_rejected():
    """Execution authority rejects tasks generated against stale authority revision 20261007-01."""
    adapter = ProjectSourceAdapter("MertSGI/Randapp-main", "control/main")
    snap = adapter.build_normalized_snapshot(
        "lari",
        "49c138f944252dba7e4c42436ff6dd735f777902",
        {"state": json.dumps(build_c4_state_json())},
        {},
    )

    task = {
        "schema_version": "0.1.0",
        "project_id": "lari",
        "task_id": "TASK-STALE-REV",
        "gate": "P7N3-FAVORITES-FAST-REBOOKING-R2",
        "title": "Title",
        "risk_class": "R1",
        "base_sha": "a8740a4a252c83060e8eaad28f49f58140c0ee5a",
        "branch_name": "feature/node3",
        "allowed_scope": {"paths": ["pages/BookingPage.tsx"]},
        "worker_requirements": {"adapter": "antigravity", "environment": "non_production", "isolated_worktree": True},
        "evidence_requirements": {"minimum_level": "E3_ISOLATED_RUNTIME_PROVEN"},
        "retry_policy": {"max_retries": 1},
        "extensions": {
            "authority_revision": "20261007-01",  # Stale revision!
        },
    }

    res = validate_execution_authority(snap, task)
    assert res.is_valid is False
    assert any("STALE_AUTHORITY_REVISION" in err for err in res.errors)


def test_9_absence_of_canonical_lane_scope_fails_closed():
    """Mutating product task without canonical lane allowed scope fails closed."""
    sit = make_situation("lari", lane_allowed_scope=None)
    resolver = CanonicalAuthorityResolver(sit)

    task = {
        "node_id": "TASK-NO-SCOPE",
        "authority_id": "DECISION-024",
        "risk_class": "R1",
        "mutating": True,
        "write_scope": ["pages/BookingPage.tsx"],
    }

    with pytest.raises(AuthorityDenied, match="CANONICAL_LANE_SCOPE_MISSING"):
        resolver.validate_task(task)


def test_10_read_only_work_remains_inspectable_without_mutation_permission():
    """Non-mutating tasks remain inspectable and allowed even if mutating scope is empty."""
    sit = make_situation("lari", lane_allowed_scope=["services/"])
    resolver = CanonicalAuthorityResolver(sit)

    task = {
        "node_id": "TASK-READ-ONLY",
        "authority_id": "DECISION-024",
        "risk_class": "R0",
        "mutating": False,
        "write_scope": [],
    }

    # Should not raise for read-only inspection
    resolver.validate_task(task)


def test_11_uiv2_still_cannot_inherit_global_node3_objective():
    """lari-ui-v2 still cannot inherit global Node3 objective from top-level state."""
    adapter = ProjectSourceAdapter("MertSGI/Randapp-main", "control/main")
    state_content = json.dumps(build_c4_state_json())

    snap_ui = adapter.build_normalized_snapshot(
        "lari-ui-v2",
        "49c138f944252dba7e4c42436ff6dd735f777902",
        {"state": state_content},
        {},
    )

    assert "Node 3" not in snap_ui["canonical_next_action"]
    assert "UI-V2" in snap_ui["canonical_next_action"]


def test_12_cockpit_product_lane_count_and_invariants():
    """Cockpit invariants: 2 product lanes, production NO_GO, paid fallback DISABLED."""
    state = build_c4_state_json()
    assert len(state["parallel_execution_lanes"]) == 2
    assert "lari" in state["parallel_execution_lanes"]
    assert "ui_v2" in state["parallel_execution_lanes"]
    assert state["production_status"] == "NO_GO"
    assert state["paid_fallback"] == "DISABLED"
