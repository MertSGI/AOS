import json
from pathlib import Path

import pytest

from aos.planning_kernel import (
    Objective,
    PlanningKernelError,
    ProjectSituation,
    _normalize_plan_schema_envelope,
)


def _objective():
    return Objective(
        objective_id="obj-1",
        title="Test",
        description="Test objective",
        authority_id="DECISION-020",
        risk_class="R0",
        rationale="test",
        scope_tags=("LARI",),
        completion_criteria=("done",),
        parallel_candidates=(),
    )


def _situation():
    return ProjectSituation(
        schema_version="1.0.0",
        project_id="lari",
        repository="MertSGI/Randapp-main",
        control_ref="control/lari-project-control-plane",
        control_sha="a" * 40,
        repository_head="b" * 40,
        execution_base_sha="c" * 40,
        current_status="ACTIVE",
        current_milestone="TEST",
        canonical_next_action="continue",
        canonical_hashes={"STATE.json": "d" * 64},
        canonical_excerpt="CURRENT_STATUS=ACTIVE\nCURRENT_MILESTONE=TEST\n",
        working_tree_state="CLEAN",
        ci_state=(),
        accepted_gates=(),
        blocked_gates=(),
        authority_records={},
        goal="continue",
        constraints=(),
        red_lines=(),
        completion_criteria=("done",),
        ambiguity_reasons=(),
        captured_at="2026-09-16T00:00:00+00:00",
    )


def _plan(version="1.0.0"):
    return {
        "schema_version": version,
        "objective_id": "obj-1",
        "tasks": [{
            "node_id": "inspect",
            "run_type": "TEST",
            "authority_id": "DECISION-020",
            "risk_class": "R0",
            "mutating": False,
            "dependencies": [],
            "scope_tags": ["LARI"],
            "write_scope": [],
            "payload": {"cmd": ["python", "--version"]},
            "expected_artifacts": [],
            "tests": ["exit zero"],
            "evidence_requirements": ["receipt"],
            "completion_criteria": ["proof complete"],
        }],
        "parallel_safe_groups": [["inspect"]],
        "rollback_strategy": "none",
    }


@pytest.mark.parametrize("version", ["1", "1.0", "v1.0.0", 1, 1.0, None, ""])
def test_v1_schema_envelope_alias_is_repaired_only_as_metadata(tmp_path: Path, version):
    plan = _plan(version)
    before_task = json.loads(json.dumps(plan["tasks"][0]))
    normalized = _normalize_plan_schema_envelope(plan, _objective(), _situation(), tmp_path)
    assert normalized["schema_version"] == "1.0.0"
    assert normalized["tasks"][0]["payload"] == before_task["payload"]
    artifact = json.loads((tmp_path / "plan-schema-envelope-repair.json").read_text(encoding="utf-8"))
    assert artifact["status"] == "APPLIED"
    assert artifact["repair_scope"] == "PLAN_SCHEMA_ENVELOPE_METADATA_ONLY"
    assert artifact["task_content_modified_by_repair"] is False
    assert artifact["authority_bypass"] is False


def test_exact_schema_needs_no_repair_artifact(tmp_path: Path):
    normalized = _normalize_plan_schema_envelope(_plan("1.0.0"), _objective(), _situation(), tmp_path)
    assert normalized["schema_version"] == "1.0.0"
    assert not (tmp_path / "plan-schema-envelope-repair.json").exists()


@pytest.mark.parametrize("version", ["0.1.0", "2.0.0", "latest", {}, []])
def test_unknown_or_different_schema_version_fails_closed(tmp_path: Path, version):
    with pytest.raises(PlanningKernelError):
        _normalize_plan_schema_envelope(_plan(version), _objective(), _situation(), tmp_path)


def test_extra_top_level_field_cannot_hide_behind_schema_repair(tmp_path: Path):
    plan = _plan("1.0")
    plan["surprise"] = True
    with pytest.raises(PlanningKernelError):
        _normalize_plan_schema_envelope(plan, _objective(), _situation(), tmp_path)


def test_extra_task_field_cannot_hide_behind_schema_repair(tmp_path: Path):
    plan = _plan("1.0")
    plan["tasks"][0]["surprise"] = True
    with pytest.raises(PlanningKernelError):
        _normalize_plan_schema_envelope(plan, _objective(), _situation(), tmp_path)
