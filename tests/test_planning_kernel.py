import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aos.planning_kernel import (
    AuthorityDenied,
    CanonicalAuthorityResolver,
    Objective,
    ProjectSituation,
    detect_completion,
    run_autonomous_project,
)


def _situation(*, status="ACTIVE", next_action="Continue authorized work", ambiguity=()):
    from aos.planning_kernel import AuthorityRecord
    rec = AuthorityRecord(
        authority_id="DECISION-020",
        source_path="docs/project-control/DECISIONS.md",
        text="DECISION-020 STANDING AUTHORITY LARI PROGRAM V2 ROUTINE NON-PRODUCTION MertSGI/Randapp-main R0 R1",
        superseded=False,
        production_allowed=False,
    )
    return ProjectSituation(
        schema_version="1.0.0",
        project_id="lari",
        repository="MertSGI/Randapp-main",
        control_ref="control/lari-project-control-plane",
        control_sha="a" * 40,
        repository_head="b" * 40,
        execution_base_sha="c" * 40,
        current_status=status,
        current_milestone="Program V2",
        canonical_next_action=next_action,
        canonical_hashes={"STATE.json": "d" * 64},
        canonical_excerpt="DECISION-020 STANDING AUTHORITY LARI PROGRAM V2 ROUTINE NON-PRODUCTION",
        working_tree_state="CLEAN",
        ci_state=[],
        accepted_gates=["NODE2=ACCEPTED"],
        blocked_gates=[],
        authority_records={"DECISION-020": rec},
        goal="Continue LARI to completion under standing authority.",
        constraints=(),
        red_lines=("production activation",),
        completion_criteria=("All roadmap work complete",),
        ambiguity_reasons=tuple(ambiguity),
        captured_at="2026-09-15T00:00:00+00:00",
    )


class QueueBackend:
    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        if not self.proposals:
            raise AssertionError("unexpected reasoning call")
        proposal = self.proposals.pop(0)
        return SimpleNamespace(
            status="SUCCESS",
            evidence_payload={"proposal": proposal, "provider_route": "fake"},
        )


def _objective():
    return {
        "objective_id": "obj-1",
        "title": "Bounded proof objective",
        "description": "Perform one bounded non-production task and verify it.",
        "authority_id": "DECISION-020",
        "risk_class": "R0",
        "rationale": "Highest-value ready objective.",
        "scope_tags": ["LARI", "PROGRAM V2"],
        "completion_criteria": ["marker exists"],
        "parallel_candidates": [],
    }


def _plan():
    return {
        "schema_version": "1.0.0",
        "objective_id": "obj-1",
        "tasks": [
            {
                "node_id": "bounded-test",
                "run_type": "TEST",
                "authority_id": "DECISION-020",
                "risk_class": "R0",
                "mutating": False,
                "dependencies": [],
                "scope_tags": ["LARI"],
                "write_scope": [],
                "payload": {"cmd": ["python", "-c", "print('ok')"]},
                "expected_artifacts": [],
                "tests": ["self"],
                "evidence_requirements": ["exit zero"],
                "completion_criteria": ["pass"],
            }
        ],
        "parallel_safe_groups": [["bounded-test"]],
        "rollback_strategy": "No mutation; no rollback required.",
    }


def _complete():
    return {
        "disposition": "PROJECT_COMPLETE",
        "rationale": "Canonical state proves completion.",
        "satisfied_criteria": ["All roadmap work complete"],
        "unsatisfied_criteria": [],
    }


def test_arbitrary_authority_id_is_rejected():
    resolver = CanonicalAuthorityResolver(_situation())
    task = _plan()["tasks"][0]
    task = dict(task)
    task["authority_id"] = "MADE-UP-AUTHORITY"
    with pytest.raises(AuthorityDenied):
        resolver.validate_task(task)


def test_force_push_payload_is_rejected():
    resolver = CanonicalAuthorityResolver(_situation())
    task = dict(_plan()["tasks"][0])
    task.update({
        "run_type": "GIT",
        "mutating": True,
        "payload": {"cmd": ["git", "push", "--force", "origin", "x"]},
    })
    with pytest.raises(AuthorityDenied):
        resolver.validate_task(task)


def test_completion_detector_does_not_confuse_dag_empty_with_project_complete(tmp_path):
    backend = QueueBackend([_complete()])
    result = detect_completion(
        _situation(status="ACTIVE", next_action="Continue Program V2"),
        tmp_path / "policy.json",
        tmp_path,
        {"progress": 100.0, "failed_task_ids": []},
        backend_override=backend,
    )
    assert result["disposition"] == "REPLAN"


def test_normal_mode_derives_own_plan_without_human_run_plan(tmp_path):
    calls = {"situation": 0, "batch": 0}

    def situation_factory(**kwargs):
        calls["situation"] += 1
        return _situation(
            status="ACTIVE" if calls["situation"] == 1 else "PROJECT_COMPLETE",
            next_action="Continue authorized work" if calls["situation"] == 1 else "PROJECT COMPLETE",
        )

    backend = QueueBackend([_objective(), _plan(), _complete()])

    def batch_executor(**kwargs):
        calls["batch"] += 1
        plan = json.loads(Path(kwargs["plan_path"]).read_text(encoding="utf-8"))
        assert plan["bound_source_sha"] == "a" * 40
        assert plan["bound_execution_base_sha"] == "c" * 40
        return {
            "progress": 100.0,
            "completed_task_ids": ["bounded-test"],
            "failed_task_ids": [],
            "ag_invocation_count": 0,
            "production": "NO_GO",
        }

    result = run_autonomous_project(
        descriptor_path=tmp_path / "descriptor.json",
        workspace=tmp_path,
        runtime_dir=tmp_path / "runtime",
        routing_policy_path=tmp_path / "policy.json",
        backend_override=backend,
        situation_factory=situation_factory,
        batch_executor=batch_executor,
        max_batches=3,
    )
    assert result["disposition"] == "PROJECT_COMPLETE"
    assert result["run_plan_required_for_normal_mode"] is False
    assert result["ag_invocation_count"] == 0
    assert calls["batch"] == 1
    assert calls["situation"] >= 2


def test_canonical_ambiguity_holds_before_reasoning(tmp_path):
    backend = QueueBackend([])

    def situation_factory(**kwargs):
        return _situation(ambiguity=("STATE contradicts EVIDENCE",))

    result = run_autonomous_project(
        descriptor_path=tmp_path / "descriptor.json",
        workspace=tmp_path,
        runtime_dir=tmp_path / "runtime",
        routing_policy_path=tmp_path / "policy.json",
        backend_override=backend,
        situation_factory=situation_factory,
        batch_executor=lambda **kwargs: pytest.fail("must not execute"),
        max_batches=1,
    )
    assert result["disposition"] == "HUMAN_REQUIRED"
    assert "CANONICAL_CONTRADICTION" in result["reason"]
    assert backend.calls == 0


def test_restart_resumes_active_generated_plan_without_regenerating_it(tmp_path):
    runtime = tmp_path / "runtime"
    batch_runtime = runtime / "batches" / "batch-0000"
    batch_runtime.mkdir(parents=True)
    plan_path = batch_runtime / "generated-run-plan.json"
    plan_path.write_text(json.dumps({"schema_version": "1.0.0", "project_id": "lari", "tasks": [{"node_id": "x"}]}), encoding="utf-8")
    checkpoint = {
        "schema_version": "1.0.0",
        "phase": "EXECUTING",
        "batch_number": 0,
        "active_plan_path": str(plan_path),
        "active_batch_runtime": str(batch_runtime),
        "completed_batches": [],
    }
    (runtime / "planning-kernel-checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    resume_calls = []

    def batch_executor(**kwargs):
        resume_calls.append(kwargs["resume"])
        return {
            "progress": 100.0,
            "completed_task_ids": ["already-done", "pending-now-done"],
            "failed_task_ids": [],
            "ag_invocation_count": 0,
            "production": "NO_GO",
        }

    backend = QueueBackend([_complete()])
    result = run_autonomous_project(
        descriptor_path=tmp_path / "descriptor.json",
        workspace=tmp_path,
        runtime_dir=runtime,
        routing_policy_path=tmp_path / "policy.json",
        backend_override=backend,
        situation_factory=lambda **kwargs: _situation(status="PROJECT_COMPLETE", next_action="PROJECT COMPLETE"),
        batch_executor=batch_executor,
        max_batches=1,
    )
    assert resume_calls == [True]
    assert result["disposition"] == "PROJECT_COMPLETE"
    assert result["completed_batches"][0]["resumed"] is True
    assert result["ag_invocation_count"] == 0
