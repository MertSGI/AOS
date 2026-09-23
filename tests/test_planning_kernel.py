import dataclasses
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

import aos.planning_kernel as planning_kernel
from aos.read_identity import (
    build_read_identity,
    build_workspace_source_generation,
    normalize_read_path,
)

from aos.planning_kernel import (
    AuthorityDenied,
    CanonicalAuthorityResolver,
    Objective,
    PLAN_SCHEMA,
    ProjectSituation,
    _canonical_excerpt,
    _bounded_completed_read_context,
    _bounded_task_signatures_for_prompt,
    _bounded_workspace_file_manifest,
    _receipt_sha256,
    _recover_waiting_objective,
    _situation_prompt_payload,
    _validate_plan_shape,
    _worker_contract_summary,
    compile_execution_plan,
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


def _test_generation(situation=None):
    value = situation or _situation()
    return build_workspace_source_generation(
        project_id=value.project_id,
        canonical_source_sha=value.control_sha,
        canonical_execution_base_sha=value.execution_base_sha,
    )


def _read_observation(path: Path, generation: str):
    normalized = normalize_read_path(path.name)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "schema_version": "1.0.0",
        "read_identity": build_read_identity(
            normalized_path=normalized,
            content_sha256=digest,
            workspace_source_generation=generation,
        ),
        "normalized_path": normalized,
        "content_sha256": digest,
        "workspace_source_generation": generation,
        "character_count": len(path.read_text(encoding="utf-8")),
        "redacted_excerpt": path.read_text(encoding="utf-8"),
    }


class QueueBackend:
    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.calls = 0
        self.requests = []

    def execute(self, request):
        self.calls += 1
        self.requests.append(request)
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
                "payload": {"cmd": ["git", "diff", "--check"]},
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


def test_atomic_json_retries_transient_windows_destination_lock(tmp_path, monkeypatch):
    destination = tmp_path / "checkpoint.json"
    destination.write_text('{"old": true}\n', encoding="utf-8")
    real_replace = planning_kernel.os.replace
    attempts = []

    def transient_replace(source, target):
        attempts.append((source, target))
        if len(attempts) < 3:
            raise PermissionError("transient Windows sharing violation")
        real_replace(source, target)

    monkeypatch.setattr(planning_kernel.os, "name", "nt")
    monkeypatch.setattr(planning_kernel.os, "replace", transient_replace)
    monkeypatch.setattr(planning_kernel.time, "sleep", lambda _seconds: None)

    planning_kernel._atomic_json(destination, {"new": True})

    assert len(attempts) == 3
    assert json.loads(destination.read_text(encoding="utf-8")) == {"new": True}
    assert not destination.with_suffix(".json.tmp").exists()


def test_reasoning_projection_is_bounded_without_weakening_durable_situation():
    large_excerpt = "A" * 50000 + "CURRENT-FRONTIER" + "Z" * 50000
    situation = dataclasses.replace(_situation(), canonical_excerpt=large_excerpt)

    projected = _situation_prompt_payload(situation)

    assert len(projected["canonical_excerpt"]) <= 2500
    assert projected["canonical_excerpt_chars"] == len(large_excerpt)
    assert projected["canonical_excerpt_sha256"] == hashlib.sha256(large_excerpt.encode()).hexdigest()
    assert situation.canonical_excerpt == large_excerpt
    assert "BOUNDED_CANONICAL_EXCERPT" in projected["canonical_excerpt"]

    compact = _situation_prompt_payload(situation, canonical_excerpt_max_chars=3000)
    assert len(compact["canonical_excerpt"]) <= 3000
    assert compact["canonical_excerpt_chars"] == len(large_excerpt)
    assert compact["canonical_excerpt_sha256"] == projected["canonical_excerpt_sha256"]


def test_worker_contract_summary_is_compact_and_complete():
    summary = _worker_contract_summary()
    assert len(summary) < 5000
    assert all(name in summary for name in ("NativeFileWorker", "NativeProcessWorker", "NativeGitWorker"))
    assert "force push" in summary
    assert '"run_type":"FILE"' in summary
    assert '"run_type":"PROCESS"' in summary
    assert '"run_type":"GIT"' in summary
    assert "Python -c and -m are forbidden" in summary


def test_workspace_file_manifest_is_bounded_hash_bound_and_path_only(tmp_path, monkeypatch):
    tracked = "\n".join([
        "package.json",
        "SECURITY_TODO.md",
        *[f"docs/dependency-safe/item-{index:03d}.md" for index in range(100)],
    ]) + "\n"
    monkeypatch.setattr(
        "aos.planning_kernel.run_headless",
        lambda *args, **kwargs: SimpleNamespace(stdout=tracked),
    )

    manifest = _bounded_workspace_file_manifest(tmp_path, Objective.from_dict(_objective()), max_chars=500)

    encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded) <= 500
    assert manifest["tracked_count"] == 102
    assert len(manifest["path_set_sha256"]) == 64
    assert "package.json" in manifest["representative_existing_paths"]
    assert all("content" not in path.lower() for path in manifest["representative_existing_paths"])


def test_completed_read_context_fresh_reads_only_completed_safe_text_tasks(tmp_path):
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    batch_runtime = runtime / "batches" / "batch-0000"
    batch_runtime.mkdir(parents=True)
    workspace.mkdir()
    roadmap = workspace / "ROADMAP.md"
    roadmap.write_text("Phase 2: implement the bounded API.\n", encoding="utf-8")
    (workspace / ".env").write_text("TOKEN=must-not-appear-anywhere\n", encoding="utf-8")
    (workspace / "not-completed.md").write_text("not evidence\n", encoding="utf-8")
    plan = _plan()
    template = plan["tasks"][0]
    plan["tasks"] = [
        {**template, "node_id": "read-roadmap", "run_type": "FILE", "payload": {"action": "read_file", "path": "ROADMAP.md"}},
        {**template, "node_id": "read-env", "run_type": "FILE", "payload": {"action": "read_file", "path": ".env"}},
        {**template, "node_id": "unfinished", "run_type": "FILE", "payload": {"action": "read_file", "path": "not-completed.md"}},
    ]
    (batch_runtime / "generated-run-plan.json").write_text(json.dumps(plan), encoding="utf-8")

    generation = _test_generation()
    context = _bounded_completed_read_context(
        runtime,
        workspace,
        [{"batch_number": 0, "receipt": {
            "completed_task_ids": ["read-roadmap", "read-env"],
            "completed_read_observations": [
                _read_observation(roadmap, generation),
                _read_observation(workspace / ".env", generation),
            ],
        }}],
        workspace_source_generation=generation,
    )

    assert context["status"] == "AVAILABLE"
    assert context["completed_read_paths"] == ["roadmap.md"]
    assert context["files"][0]["redacted_excerpt"] == "Phase 2: implement the bounded API.\n"
    assert context["files"][0]["content_sha256"] == hashlib.sha256(roadmap.read_bytes()).hexdigest()
    assert "must-not-appear" not in json.dumps(context)


def test_completed_read_context_keeps_only_most_recent_bounded_file(tmp_path):
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    completed_batches = []
    generation = _test_generation()
    for batch_number in range(4):
        path = f"discovery-{batch_number}.md"
        (workspace / path).write_text(str(batch_number) * 2000, encoding="utf-8")
        batch_runtime = runtime / "batches" / f"batch-{batch_number:04d}"
        batch_runtime.mkdir(parents=True)
        plan = _plan()
        plan["tasks"][0].update({
            "node_id": f"read-{batch_number}",
            "run_type": "FILE",
            "payload": {"action": "read_file", "path": path},
        })
        (batch_runtime / "generated-run-plan.json").write_text(json.dumps(plan), encoding="utf-8")
        completed_batches.append({
            "batch_number": batch_number,
            "receipt": {
                "completed_task_ids": [f"read-{batch_number}"],
                "completed_read_observations": [_read_observation(workspace / path, generation)],
            },
        })

    context = _bounded_completed_read_context(
        runtime, workspace, completed_batches,
        workspace_source_generation=generation,
    )

    assert context["completed_read_paths"] == ["discovery-3.md"]
    assert len(context["files"]) == 1
    assert all(len(item["redacted_excerpt"]) <= 500 for item in context["files"])
    assert all(len(item["content_sha256"]) == 64 for item in context["files"])


def test_plan_compiler_rejects_renamed_repeat_of_completed_read_path(tmp_path):
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text("next work\n", encoding="utf-8")
    repeated = _plan()
    repeated["tasks"][0].update({
        "node_id": "renamed-roadmap-read",
        "run_type": "FILE",
        "payload": {"action": "read_file", "path": "ROADMAP.md"},
    })
    backend = QueueBackend([repeated, _plan()])

    generation = _test_generation()
    observation = _read_observation(roadmap, generation)
    result = compile_execution_plan(
        _situation(),
        Objective.from_dict(_objective()),
        tmp_path / "policy.json",
        tmp_path,
        backend_override=backend,
        forbidden_read_paths=[observation["read_identity"]],
        workspace=tmp_path,
        workspace_source_generation=generation,
    )

    assert result["tasks"][0]["run_type"] == "TEST"
    assert backend.calls == 2


def test_changed_file_has_new_read_identity_and_can_be_reread(tmp_path):
    target = tmp_path / "ui.tsx"
    target.write_text("export const version = 1;\n", encoding="utf-8")
    generation = _test_generation()
    old = _read_observation(target, generation)
    target.write_text("export const version = 2;\n", encoding="utf-8")
    plan = _plan()
    plan["tasks"][0].update({
        "node_id": "read-ui-again",
        "run_type": "FILE",
        "payload": {"action": "read_file", "path": "ui.tsx"},
    })
    backend = QueueBackend([plan])

    result = compile_execution_plan(
        _situation(), Objective.from_dict(_objective()),
        tmp_path / "policy.json", tmp_path,
        backend_override=backend,
        forbidden_read_paths=[old["read_identity"]],
        workspace=tmp_path,
        workspace_source_generation=generation,
    )

    assert result["tasks"][0]["node_id"] == "read-ui-again"
    assert backend.calls == 1


def test_source_generation_change_permits_same_bytes_reread(tmp_path):
    target = tmp_path / "README.md"
    target.write_text("same bytes\n", encoding="utf-8")
    old_generation = "1" * 64
    new_generation = "2" * 64
    old = _read_observation(target, old_generation)
    plan = _plan()
    plan["tasks"][0].update({
        "node_id": "read-new-generation",
        "run_type": "FILE",
        "payload": {"action": "read_file", "path": "README.md"},
    })

    result = compile_execution_plan(
        _situation(), Objective.from_dict(_objective()),
        tmp_path / "policy.json", tmp_path,
        backend_override=QueueBackend([plan]),
        forbidden_read_paths=[old["read_identity"]],
        workspace=tmp_path,
        workspace_source_generation=new_generation,
    )

    assert result["tasks"][0]["node_id"] == "read-new-generation"


def test_legacy_read_receipt_is_unbound_and_does_not_ban_path(tmp_path):
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    batch_runtime = runtime / "batches" / "batch-0000"
    batch_runtime.mkdir(parents=True)
    workspace.mkdir()
    (workspace / "ROADMAP.md").write_text("legacy\n", encoding="utf-8")
    plan = _plan()
    plan["tasks"][0].update({
        "node_id": "legacy-read",
        "run_type": "FILE",
        "payload": {"action": "read_file", "path": "ROADMAP.md"},
    })
    (batch_runtime / "generated-run-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    batches = [{"batch_number": 0, "receipt": {"completed_task_ids": ["legacy-read"]}}]

    context = _bounded_completed_read_context(
        runtime, workspace, batches,
        workspace_source_generation=_test_generation(),
    )

    assert context["files"] == []
    assert context["completed_read_identities"] == []
    assert context["legacy_unbound_paths"] == ["roadmap.md"]
    assert planning_kernel._completed_task_signatures(runtime, batches) == []


def test_strategy_generation_change_forces_fresh_objective_and_context(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    situation = _situation()
    (runtime / "situation-0000.json").write_text(
        json.dumps(situation.to_dict()), encoding="utf-8"
    )
    (runtime / "objective-0000.json").write_text(
        json.dumps(_objective()), encoding="utf-8"
    )
    (runtime / "planning-kernel-checkpoint.json").write_text(json.dumps({
        "schema_version": "1.0.0",
        "phase": "WAITING_FOR_REASONING_PROVIDER",
        "batch_number": 0,
        "situation_id": situation.identity(),
        "canonical_source_sha": situation.control_sha,
        "canonical_execution_base_sha": situation.execution_base_sha,
        "strategy_generation": 0,
    }), encoding="utf-8")
    backend = QueueBackend([_objective(), _plan()])

    result = run_autonomous_project(
        descriptor_path=tmp_path / "descriptor.json",
        workspace=tmp_path,
        runtime_dir=runtime,
        routing_policy_path=tmp_path / "policy.json",
        backend_override=backend,
        situation_factory=lambda **kwargs: situation,
        batch_executor=lambda **kwargs: {
            "progress": 100.0,
            "completed_task_ids": ["bounded-test"],
            "failed_task_ids": [],
            "production": "NO_GO",
        },
        max_batches=1,
        strategy_generation=1,
        recovery_failure_context={"failure_family": "PLANNER_VALIDATION"},
    )

    assert result["disposition"] == "BOUNDED_RUN_EXHAUSTED"
    assert backend.requests[0].task_id == "objective-selection"
    assert "RECOVERY_STRATEGY_ESCALATION:PLANNER_VALIDATION" in backend.requests[0].payload["prompt"]


def test_plan_compiler_rejects_renamed_repeat_of_completed_action(tmp_path):
    repeated = _plan()
    repeated["tasks"][0].update({
        "node_id": "renamed-status-check",
        "run_type": "GIT",
        "payload": {"action": "status", "args": []},
    })
    backend = QueueBackend([repeated, _plan()])

    result = compile_execution_plan(
        _situation(),
        Objective.from_dict(_objective()),
        tmp_path / "policy.json",
        tmp_path,
        backend_override=backend,
        forbidden_task_signatures=['GIT:{"action":"status","args":[]}'],
        workspace=tmp_path,
    )

    assert result["tasks"][0]["run_type"] == "TEST"
    assert backend.calls == 2


def test_completed_action_signatures_are_bounded_for_provider_prompt():
    signatures = [f"FILE:{{\"patch\":\"{index}-{'x' * 1000}\"}}" for index in range(100)]

    bounded = _bounded_task_signatures_for_prompt(signatures)

    assert bounded["total_count"] == 100
    assert len(bounded["signature_set_sha256"]) == 64
    assert bounded["representative_signatures"]
    assert all(len(item) <= 240 for item in bounded["representative_signatures"])
    assert len(json.dumps(bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) <= 1800


def test_plan_compiler_rejects_generic_readiness_only_batch(tmp_path):
    generic = _plan()
    first = generic["tasks"][0]
    generic["tasks"] = [
        {**first, "node_id": "status-again", "run_type": "GIT", "payload": {"action": "status", "args": []}},
        {**first, "node_id": "runtime-version", "run_type": "PROCESS", "dependencies": ["status-again"], "payload": {"cmd": ["python", "--version"]}},
    ]
    generic["parallel_safe_groups"] = [["status-again"], ["runtime-version"]]
    backend = QueueBackend([generic, _plan()])

    result = compile_execution_plan(
        _situation(),
        Objective.from_dict(_objective()),
        tmp_path / "policy.json",
        tmp_path,
        backend_override=backend,
        workspace=tmp_path,
    )

    assert result["tasks"][0]["run_type"] == "TEST"
    assert backend.calls == 2


def test_replanning_prompt_receives_fresh_completed_read_context(tmp_path):
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    batch_runtime = runtime / "batches" / "batch-0000"
    batch_runtime.mkdir(parents=True)
    workspace.mkdir()
    (workspace / "ROADMAP.md").write_text("Implement endpoint Z next.\n", encoding="utf-8")
    completed_plan = _plan()
    completed_plan["tasks"][0].update({
        "node_id": "read-roadmap",
        "run_type": "FILE",
        "payload": {"action": "read_file", "path": "ROADMAP.md"},
    })
    (batch_runtime / "generated-run-plan.json").write_text(json.dumps(completed_plan), encoding="utf-8")
    prior_receipt = {
        "progress": 100.0,
        "completed_task_ids": ["read-roadmap"],
        "failed_task_ids": [],
        "ag_invocation_count": 0,
        "production": "NO_GO",
        "completed_read_observations": [
            _read_observation(workspace / "ROADMAP.md", _test_generation())
        ],
    }
    (runtime / "planning-kernel-checkpoint.json").write_text(json.dumps({
        "schema_version": "1.0.0",
        "phase": "BATCH_COMPLETE",
        "batch_number": 1,
        "completed_batches": [{"batch_number": 0, "receipt": prior_receipt}],
        "last_receipt": prior_receipt,
    }), encoding="utf-8")
    backend = QueueBackend([
        {
            "disposition": "REPLAN",
            "rationale": "Authorized work remains.",
            "satisfied_criteria": [],
            "unsatisfied_criteria": ["All roadmap work complete"],
        },
        _objective(),
        _plan(),
    ])

    result = run_autonomous_project(
        descriptor_path=tmp_path / "descriptor.json",
        workspace=workspace,
        runtime_dir=runtime,
        routing_policy_path=tmp_path / "policy.json",
        backend_override=backend,
        situation_factory=lambda **kwargs: _situation(),
        batch_executor=lambda **kwargs: {
            "progress": 100.0,
            "completed_task_ids": ["bounded-test"],
            "failed_task_ids": [],
            "ag_invocation_count": 0,
            "production": "NO_GO",
        },
        max_batches=1,
    )

    plan_prompt = backend.requests[2].payload["prompt"]
    assert result["disposition"] == "BOUNDED_RUN_EXHAUSTED"
    assert "Implement endpoint Z next." in plan_prompt
    assert '"completed_read_paths": ["roadmap.md"]' in plan_prompt
    assert 'FILE:{\\"action\\":\\"read_file\\",\\"path\\":\\"ROADMAP.md\\"}' not in plan_prompt


def test_plan_schema_constrains_canonical_run_types():
    assert PLAN_SCHEMA["properties"]["tasks"]["maxItems"] == 4
    run_type = PLAN_SCHEMA["properties"]["tasks"]["items"]["properties"]["run_type"]
    assert set(run_type["enum"]) == {
        "FILE", "PROCESS", "GIT", "TEST", "BUILD", "CI", "BROWSER", "MODEL_REASONING",
    }
    assert "NATIVE_PROCESS" not in run_type["enum"]
    payload = PLAN_SCHEMA["properties"]["tasks"]["items"]["properties"]["payload"]
    assert payload["minProperties"] == 1
    assert {"action", "cmd", "sha", "url", "prompt"}.issubset(payload["properties"])


def test_canonical_excerpt_prioritizes_state_and_roadmap_over_large_history():
    excerpt = _canonical_excerpt({
        "docs/project-control/EVIDENCE.jsonl": "E" * 1000,
        "docs/project-control/DECISIONS.md": "D" * 1000,
        "docs/project-control/ROADMAP_12W.md": "CURRENT-ROADMAP",
        "docs/project-control/STATE.json": '{"next_action":"CURRENT-ACTION"}',
    }, max_chars=200)

    assert "CURRENT-ACTION" in excerpt
    assert "CURRENT-ROADMAP" in excerpt
    assert "E" * 20 not in excerpt


@pytest.mark.parametrize(
    ("run_type", "payload"),
    [
        ("GIT", {}),
        ("GIT", {"args": []}),
        ("GIT", {"action": "git", "args": ["status"]}),
        ("PROCESS", {}),
        ("PROCESS", {"cmd": []}),
        ("PROCESS", {"cmd": ["python", " -c", "print('bad option spacing')"]}),
        ("PROCESS", {"cmd": ["python", "-c", "print('write bypass')"]}),
        ("PROCESS", {"cmd": ["node", "--eval", "require('fs').writeFileSync('x','y')"]}),
    ],
)
def test_plan_rejects_non_executable_worker_payload(run_type, payload):
    plan = _plan()
    plan["tasks"][0]["run_type"] = run_type
    plan["tasks"][0]["payload"] = payload

    with pytest.raises(Exception, match="payload|cmd|git action|inline"):
        _validate_plan_shape(plan, Objective.from_dict(_objective()), _situation())


def test_plan_rejects_git_force_with_lease_payload():
    plan = _plan()
    plan["tasks"][0].update({
        "run_type": "GIT",
        "mutating": True,
        "payload": {"action": "push", "args": ["--force-with-lease", "origin", "candidate"]},
    })

    with pytest.raises(Exception, match="force push"):
        _validate_plan_shape(plan, Objective.from_dict(_objective()), _situation())


def test_mutating_file_plan_requires_and_honors_write_scope():
    plan = _plan()
    plan["tasks"][0].update({
        "run_type": "FILE",
        "mutating": True,
        "payload": {"action": "write_file", "path": "src/feature/result.txt", "content": "ok"},
        "write_scope": [],
    })
    with pytest.raises(Exception, match="write_scope"):
        _validate_plan_shape(plan, Objective.from_dict(_objective()), _situation())

    plan["tasks"][0]["write_scope"] = ["docs"]
    with pytest.raises(Exception, match="outside declared write_scope"):
        _validate_plan_shape(plan, Objective.from_dict(_objective()), _situation())


@pytest.mark.parametrize(
    "path",
    ["notes/next_action.txt", "NEXT_ACTION.md", "notes/status-marker.json", "PHASE2-7_PROGRESS.md"],
)
def test_mutating_file_plan_rejects_synthetic_next_action_artifact(path):
    plan = _plan()
    plan["tasks"][0].update({
        "run_type": "FILE",
        "mutating": True,
        "payload": {"action": "write_file", "path": path, "content": "done"},
        "write_scope": [str(Path(path).parent).replace(".", "") or path],
    })

    with pytest.raises(Exception, match="synthetic bookkeeping"):
        _validate_plan_shape(plan, Objective.from_dict(_objective()), _situation())


def test_plan_compiler_gets_one_bounded_repair_for_invalid_worker_payload(tmp_path):
    invalid = _plan()
    invalid["tasks"][0]["payload"] = {}
    backend = QueueBackend([invalid, _plan()])

    result = compile_execution_plan(
        _situation(),
        Objective.from_dict(_objective()),
        tmp_path / "policy.json",
        tmp_path,
        backend_override=backend,
    )

    assert result["tasks"][0]["payload"]["cmd"][0] == "git"
    assert backend.calls == 2


def test_plan_compiler_provider_wait_resumes_exact_bound_validation_repair(tmp_path):
    invalid = _plan()
    invalid["tasks"][0]["payload"] = {}

    class WaitDuringRepairBackend:
        def __init__(self):
            self.calls = 0

        def execute(self, request):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    status="SUCCESS",
                    evidence_payload={"proposal": invalid, "provider_route": "fake"},
                )
            return SimpleNamespace(
                status="WAITING_FOR_REASONING_PROVIDER",
                evidence_payload={"failure_class": "ALL_ELIGIBLE_REASONING_PROVIDERS_UNAVAILABLE"},
            )

    first_backend = WaitDuringRepairBackend()
    with pytest.raises(planning_kernel.WaitingForReasoningProvider):
        compile_execution_plan(
            _situation(),
            Objective.from_dict(_objective()),
            tmp_path / "policy.json",
            tmp_path,
            backend_override=first_backend,
            batch_number=0,
        )

    artifact = json.loads(
        (tmp_path / "plan-dag-repair-0000.json").read_text(encoding="utf-8")
    )
    assert artifact["status"] == "PENDING"
    assert artifact["previous_invalid_plan"] == invalid

    resumed_backend = QueueBackend([_plan()])
    result = compile_execution_plan(
        _situation(),
        Objective.from_dict(_objective()),
        tmp_path / "policy.json",
        tmp_path,
        backend_override=resumed_backend,
        batch_number=0,
    )

    assert result["tasks"][0]["payload"]["cmd"][0] == "git"
    assert resumed_backend.calls == 1
    assert "VALIDATION_REPAIR_REQUIRED" in resumed_backend.requests[0].payload["prompt"]

    context_changed_backend = QueueBackend([_plan()])
    compile_execution_plan(
        _situation(),
        Objective.from_dict(_objective()),
        tmp_path / "policy.json",
        tmp_path,
        backend_override=context_changed_backend,
        forbidden_task_ids=["different-completed-task"],
        batch_number=0,
    )
    assert "VALIDATION_REPAIR_REQUIRED" not in context_changed_backend.requests[0].payload["prompt"]


def test_exhausted_plan_validation_replans_without_claiming_provider_outage(tmp_path):
    invalid = _plan()
    invalid["tasks"][0]["payload"] = {"cmd": ["python", "-m", "pytest"]}
    backend = QueueBackend([_objective(), invalid, invalid])
    executed = []

    result = run_autonomous_project(
        descriptor_path=tmp_path / "descriptor.json",
        workspace=tmp_path,
        runtime_dir=tmp_path / "runtime",
        routing_policy_path=tmp_path / "policy.json",
        backend_override=backend,
        situation_factory=lambda **kwargs: _situation(),
        batch_executor=lambda **kwargs: executed.append(kwargs),
        max_batches=1,
    )

    checkpoint = json.loads(
        (tmp_path / "runtime" / "planning-kernel-checkpoint.json").read_text(encoding="utf-8")
    )
    assert result["disposition"] == "BOUNDED_RUN_EXHAUSTED"
    assert result["reason"].startswith("PLANNER_VALIDATION_REPAIR_EXHAUSTED:")
    assert checkpoint["phase"] == "BOUNDED_RUN_EXHAUSTED"
    assert checkpoint["replan_reason"].startswith("PLANNER_VALIDATION_REPAIR_EXHAUSTED:")
    assert backend.calls == 3
    assert executed == []
    assert not (tmp_path / "runtime" / "batches" / "batch-0000" / "generated-run-plan.json").exists()
    repair_artifact = json.loads(
        (tmp_path / "runtime" / "plan-dag-repair-0000.json").read_text(encoding="utf-8")
    )
    assert repair_artifact["status"] == "EXHAUSTED"

    # Subsequent retry does not lock into repair mode since status is EXHAUSTED
    retry_backend = QueueBackend([_objective(), _plan()])
    retry_result = run_autonomous_project(
        descriptor_path=tmp_path / "descriptor.json",
        workspace=tmp_path,
        runtime_dir=tmp_path / "runtime",
        routing_policy_path=tmp_path / "policy.json",
        backend_override=retry_backend,
        situation_factory=lambda **kwargs: _situation(),
        batch_executor=lambda **kwargs: {"progress": 100.0, "completed_task_ids": ["task-1"], "failed_task_ids": []},
        max_batches=1,
    )
    assert retry_result["disposition"] == "BOUNDED_RUN_EXHAUSTED"
    assert "VALIDATION_REPAIR_REQUIRED" not in retry_backend.requests[1].payload["prompt"]


def test_plan_compiler_repairs_python_inline_execution_with_guidance(tmp_path):
    invalid = _plan()
    invalid["tasks"][0]["payload"] = {"cmd": ["python", "-c", "import sys; print(sys.version)"]}
    valid = _plan()
    valid["tasks"][0]["payload"] = {"cmd": ["git", "status"]}
    backend = QueueBackend([invalid, valid])

    result = compile_execution_plan(
        _situation(),
        Objective.from_dict(_objective()),
        tmp_path / "policy.json",
        tmp_path,
        backend_override=backend,
        batch_number=0,
    )

    assert result["tasks"][0]["payload"]["cmd"][0] == "git"
    assert backend.calls == 2
    repair_prompt = backend.requests[1].payload["prompt"]
    assert "VALIDATION_REPAIR_REQUIRED" in repair_prompt
    assert "PYTHON_PAYLOAD_RULE" in repair_prompt
    assert "Python inline execution (`-c` or `-m`) is prohibited" in repair_prompt


def test_plan_compiler_salvages_safe_independent_tasks_from_partially_invalid_repair(tmp_path):
    invalid = _plan()
    invalid["tasks"][0]["payload"] = {"cmd": ["python", "-c", "print('invalid')"]}
    repaired = json.loads(json.dumps(invalid))
    valid_task = json.loads(json.dumps(_plan()["tasks"][0]))
    valid_task["node_id"] = "verify-product-diff"
    valid_task["payload"] = {"cmd": ["git", "diff", "--check"]}
    repaired["tasks"].append(valid_task)
    repaired["parallel_safe_groups"] = [["bounded-test", "verify-product-diff"]]
    backend = QueueBackend([invalid, repaired])

    result = compile_execution_plan(
        _situation(),
        Objective.from_dict(_objective()),
        tmp_path / "policy.json",
        tmp_path,
        backend_override=backend,
        workspace=tmp_path,
        batch_number=3,
    )

    assert [task["node_id"] for task in result["tasks"]] == ["verify-product-diff"]
    artifact = json.loads((tmp_path / "plan-dag-pruning-0003.json").read_text(encoding="utf-8"))
    assert artifact["rejected_tasks"] == {"bounded-test": "PYTHON_INLINE_OR_MODULE"}
    assert artifact["retained_task_ids"] == ["verify-product-diff"]



def test_plan_compiler_repairs_allowlisted_but_unavailable_process_binary(tmp_path, monkeypatch):
    invalid = _plan()
    invalid["tasks"][0]["payload"] = {"cmd": ["npm", "test"]}
    backend = QueueBackend([invalid, _plan()])
    monkeypatch.setattr(
        "aos.planning_kernel.shutil.which",
        lambda binary: None if binary == "npm" else f"/available/{binary}",
    )

    result = compile_execution_plan(
        _situation(),
        Objective.from_dict(_objective()),
        tmp_path / "policy.json",
        tmp_path,
        backend_override=backend,
    )

    assert result["tasks"][0]["payload"]["cmd"][0] == "git"
    assert backend.calls == 2
    assert "PROCESS_BINARY_RULE" in backend.requests[1].payload["prompt"]
    assert "AVAILABLE_PROCESS_BINARIES" in backend.requests[1].payload["prompt"]


def test_plan_compiler_repairs_invented_python_script_path(tmp_path):
    invalid = _plan()
    invalid["tasks"][0]["payload"] = {"cmd": ["python", "missing-verifier.py"]}
    backend = QueueBackend([invalid, _plan()])

    result = compile_execution_plan(
        _situation(),
        Objective.from_dict(_objective()),
        tmp_path / "policy.json",
        tmp_path,
        backend_override=backend,
        workspace=tmp_path,
    )

    assert result["tasks"][0]["payload"]["cmd"] == ["git", "diff", "--check"]
    assert backend.calls == 2


def test_plan_compiler_rejects_and_repairs_duplicate_completed_task_identity(tmp_path):
    duplicate = _plan()
    corrected = _plan()
    corrected["tasks"][0]["node_id"] = "new-bounded-test"
    corrected["parallel_safe_groups"] = [["new-bounded-test"]]
    backend = QueueBackend([duplicate, corrected])

    result = compile_execution_plan(
        _situation(),
        Objective.from_dict(_objective()),
        tmp_path / "policy.json",
        tmp_path,
        backend_override=backend,
        forbidden_task_ids=["bounded-test"],
    )

    assert result["tasks"][0]["node_id"] == "new-bounded-test"
    assert backend.calls == 2


def test_plan_compiler_repairs_nonexistent_file_read_before_execution(tmp_path):
    invalid = _plan()
    invalid["tasks"][0].update({
        "run_type": "FILE",
        "payload": {"action": "read_file", "path": "missing-control.md"},
    })
    backend = QueueBackend([invalid, _plan()])

    result = compile_execution_plan(
        _situation(),
        Objective.from_dict(_objective()),
        tmp_path / "policy.json",
        tmp_path,
        backend_override=backend,
        workspace=tmp_path,
    )

    assert result["tasks"][0]["run_type"] == "TEST"
    assert backend.calls == 2


def test_plan_compiler_allows_read_of_declared_upstream_artifact(tmp_path):
    plan = _plan()
    producer = plan["tasks"][0]
    producer["node_id"] = "produce-security-report"
    producer["expected_artifacts"] = ["security_test_report.txt"]
    reader = {
        **producer,
        "node_id": "read-security-report",
        "run_type": "FILE",
        "dependencies": ["produce-security-report"],
        "payload": {"action": "read_file", "path": "security_test_report.txt"},
        "expected_artifacts": [],
        "tests": [],
    }
    plan["tasks"] = [producer, reader]
    plan["parallel_safe_groups"] = [["produce-security-report"], ["read-security-report"]]
    backend = QueueBackend([plan])

    result = compile_execution_plan(
        _situation(),
        Objective.from_dict(_objective()),
        tmp_path / "policy.json",
        tmp_path,
        backend_override=backend,
        workspace=tmp_path,
    )

    assert result["tasks"][1]["payload"]["path"] == "security_test_report.txt"
    assert backend.calls == 1


def test_plan_compiler_rejects_artifact_declared_by_non_dependency(tmp_path):
    invalid = _plan()
    producer = invalid["tasks"][0]
    producer["node_id"] = "unrelated-producer"
    producer["expected_artifacts"] = ["security_test_report.txt"]
    reader = {
        **producer,
        "node_id": "unordered-reader",
        "run_type": "FILE",
        "dependencies": [],
        "payload": {"action": "read_file", "path": "security_test_report.txt"},
        "expected_artifacts": [],
        "tests": [],
    }
    invalid["tasks"] = [producer, reader]
    invalid["parallel_safe_groups"] = [["unrelated-producer", "unordered-reader"]]
    backend = QueueBackend([invalid, _plan()])

    result = compile_execution_plan(
        _situation(),
        Objective.from_dict(_objective()),
        tmp_path / "policy.json",
        tmp_path,
        backend_override=backend,
        workspace=tmp_path,
    )

    assert len(result["tasks"]) == 1
    assert backend.calls == 2


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


def test_provider_wait_retry_reuses_only_exactly_bound_durable_objective(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    situation = _situation()
    prior_situation = situation.to_dict()
    checkpoint = {
        "phase": "WAITING_FOR_REASONING_PROVIDER",
        "batch_number": 0,
        "situation_id": situation.identity(),
        "canonical_source_sha": situation.control_sha,
        "canonical_execution_base_sha": situation.execution_base_sha,
    }
    (runtime / "objective-0000.json").write_text(json.dumps(_objective()), encoding="utf-8")

    recovered = _recover_waiting_objective(runtime, 0, checkpoint, prior_situation, situation)

    assert recovered is not None
    assert recovered.objective_id == "obj-1"
    assert _recover_waiting_objective(
        runtime, 0, {**checkpoint, "canonical_source_sha": "f" * 40}, prior_situation, situation,
    ) is None
    assert _recover_waiting_objective(
        runtime, 0, checkpoint, {**prior_situation, "situation_id": "stale"}, situation,
    ) is None
    (runtime / "objective-0000.json").write_text(
        json.dumps({**_objective(), "unbound_extension": True}), encoding="utf-8",
    )
    assert _recover_waiting_objective(runtime, 0, checkpoint, prior_situation, situation) is None


def test_provider_wait_retry_skips_duplicate_objective_reasoning(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    situation = _situation()
    (runtime / "situation-0000.json").write_text(json.dumps(situation.to_dict()), encoding="utf-8")
    (runtime / "objective-0000.json").write_text(json.dumps(_objective()), encoding="utf-8")
    (runtime / "planning-kernel-checkpoint.json").write_text(json.dumps({
        "schema_version": "1.0.0",
        "phase": "WAITING_FOR_REASONING_PROVIDER",
        "batch_number": 0,
        "completed_batches": [],
        "situation_id": situation.identity(),
        "canonical_source_sha": situation.control_sha,
        "canonical_execution_base_sha": situation.execution_base_sha,
    }), encoding="utf-8")
    backend = QueueBackend([_plan()])

    result = run_autonomous_project(
        descriptor_path=tmp_path / "descriptor.json",
        workspace=tmp_path,
        runtime_dir=runtime,
        routing_policy_path=tmp_path / "policy.json",
        backend_override=backend,
        situation_factory=lambda **kwargs: situation,
        batch_executor=lambda **kwargs: {
            "progress": 100.0,
            "completed_task_ids": ["bounded-test"],
            "failed_task_ids": [],
            "ag_invocation_count": 0,
            "production": "NO_GO",
        },
        max_batches=1,
    )

    assert result["disposition"] == "BOUNDED_RUN_EXHAUSTED"
    assert backend.calls == 1


def test_provider_wait_retry_reuses_exact_bound_replan_and_objective(tmp_path):
    runtime = tmp_path / "runtime"
    batch_runtime = runtime / "batches" / "batch-0000"
    batch_runtime.mkdir(parents=True)
    situation = _situation()
    receipt = {
        "progress": 100.0,
        "completed_task_ids": ["read-roadmap"],
        "failed_task_ids": [],
        "ag_invocation_count": 0,
        "production": "NO_GO",
    }
    completed_plan = _plan()
    completed_plan["tasks"][0].update({
        "node_id": "read-roadmap",
        "run_type": "FILE",
        "payload": {"action": "read_file", "path": "ROADMAP.md"},
    })
    (batch_runtime / "generated-run-plan.json").write_text(json.dumps(completed_plan), encoding="utf-8")
    (tmp_path / "ROADMAP.md").write_text("Implement endpoint Z next.\n", encoding="utf-8")
    receipt["completed_read_observations"] = [
        _read_observation(tmp_path / "ROADMAP.md", _test_generation(situation))
    ]
    (runtime / "situation-0001.json").write_text(json.dumps(situation.to_dict()), encoding="utf-8")
    (runtime / "objective-0001.json").write_text(json.dumps(_objective()), encoding="utf-8")
    replan = {
        "disposition": "REPLAN",
        "rationale": "Authorized work remains.",
        "satisfied_criteria": [],
        "unsatisfied_criteria": ["All roadmap work complete"],
    }
    (runtime / "completion-0001.json").write_text(json.dumps({
        "schema_version": "1.0.0",
        "situation_id": situation.identity(),
        "canonical_source_sha": situation.control_sha,
        "canonical_execution_base_sha": situation.execution_base_sha,
        "recent_receipt_sha256": _receipt_sha256(receipt),
        "completion": replan,
    }), encoding="utf-8")
    (runtime / "planning-kernel-checkpoint.json").write_text(json.dumps({
        "schema_version": "1.0.0",
        "phase": "WAITING_FOR_REASONING_PROVIDER",
        "batch_number": 1,
        "completed_batches": [{"batch_number": 0, "receipt": receipt}],
        "last_receipt": receipt,
        "situation_id": situation.identity(),
        "canonical_source_sha": situation.control_sha,
        "canonical_execution_base_sha": situation.execution_base_sha,
    }), encoding="utf-8")
    backend = QueueBackend([_plan()])

    result = run_autonomous_project(
        descriptor_path=tmp_path / "descriptor.json",
        workspace=tmp_path,
        runtime_dir=runtime,
        routing_policy_path=tmp_path / "policy.json",
        backend_override=backend,
        situation_factory=lambda **kwargs: situation,
        batch_executor=lambda **kwargs: {
            "progress": 100.0,
            "completed_task_ids": ["bounded-test"],
            "failed_task_ids": [],
            "ag_invocation_count": 0,
            "production": "NO_GO",
        },
        max_batches=1,
    )

    assert result["disposition"] == "BOUNDED_RUN_EXHAUSTED"
    assert backend.calls == 1
    assert "Implement endpoint Z next." in backend.requests[0].payload["prompt"]


def test_completion_provider_wait_is_durable_not_terminal_failure(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    situation = _situation()
    receipt = {
        "progress": 100.0,
        "completed_task_ids": ["already-complete"],
        "failed_task_ids": [],
        "ag_invocation_count": 0,
        "production": "NO_GO",
    }
    (runtime / "planning-kernel-checkpoint.json").write_text(json.dumps({
        "schema_version": "1.0.0",
        "phase": "BATCH_COMPLETE",
        "batch_number": 1,
        "completed_batches": [{"batch_number": 0, "receipt": receipt}],
        "last_receipt": receipt,
    }), encoding="utf-8")

    class WaitingBackend:
        def execute(self, request):
            return SimpleNamespace(
                status="WAITING_FOR_REASONING_PROVIDER",
                evidence_payload={"failure_class": "ALL_ELIGIBLE_REASONING_PROVIDERS_UNAVAILABLE"},
            )

    result = run_autonomous_project(
        descriptor_path=tmp_path / "descriptor.json",
        workspace=tmp_path,
        runtime_dir=runtime,
        routing_policy_path=tmp_path / "policy.json",
        backend_override=WaitingBackend(),
        situation_factory=lambda **kwargs: situation,
        batch_executor=lambda **kwargs: pytest.fail("must not execute"),
        max_batches=1,
    )

    checkpoint = json.loads((runtime / "planning-kernel-checkpoint.json").read_text(encoding="utf-8"))
    assert result["disposition"] == "WAITING_FOR_REASONING_PROVIDER"
    assert result["completed_batch_count"] == 1
    assert checkpoint["phase"] == "WAITING_FOR_REASONING_PROVIDER"
    assert checkpoint["last_receipt"]["completed_task_ids"] == ["already-complete"]


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


def test_restart_advances_batch_number_before_replanning(tmp_path):
    runtime = tmp_path / "runtime"
    interrupted = runtime / "batches" / "batch-0001"
    interrupted.mkdir(parents=True)
    interrupted_plan = interrupted / "generated-run-plan.json"
    interrupted_plan.write_text(json.dumps({
        "schema_version": "1.0.0",
        "project_id": "lari",
        "tasks": [{"node_id": "interrupted"}],
    }), encoding="utf-8")
    (runtime / "planning-kernel-checkpoint.json").write_text(json.dumps({
        "schema_version": "1.0.0",
        "phase": "EXECUTING",
        "batch_number": 1,
        "active_plan_path": str(interrupted_plan),
        "active_batch_runtime": str(interrupted),
        "completed_batches": [],
    }), encoding="utf-8")
    calls = []

    def batch_executor(**kwargs):
        calls.append((kwargs["resume"], Path(kwargs["plan_path"])))
        if kwargs["resume"]:
            return {
                "progress": 50.0,
                "completed_task_ids": ["interrupted-read"],
                "failed_task_ids": ["interrupted-missing"],
                "ag_invocation_count": 0,
                "production": "NO_GO",
            }
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
        runtime_dir=runtime,
        routing_policy_path=tmp_path / "policy.json",
        backend_override=QueueBackend([_objective(), _plan()]),
        situation_factory=lambda **kwargs: _situation(),
        batch_executor=batch_executor,
        max_batches=1,
    )

    assert calls[0] == (True, interrupted_plan)
    assert calls[1][0] is False
    assert calls[1][1].parent.name == "batch-0002"
    assert result["completed_batches"][0]["batch_number"] == 1
    assert result["completed_batches"][1]["batch_number"] == 2


def test_extract_authorities_prioritizes_decisions_over_evidence():
    from aos.planning_kernel import _extract_authorities
    contents = {
        "evidence": '{"canonical_decision": "DECISION-020_PROGRAM_V2_FULL_NAME", "summary": "some evidence summary that is long"}\n',
        "decisions": "## DECISION-020_PROGRAM_V2_FULL_NAME\n- **Status**: ACCEPTED\n- **Decision**: Standing authority for program.\n",
    }
    auths = _extract_authorities(contents)
    rec = auths["DECISION-020_PROGRAM_V2_FULL_NAME"]
    assert rec.source_path == "decisions"


def test_canonical_authority_resolver_accepts_hyphenated_sub_lane_project_id():
    sit = _situation()
    object.__setattr__(sit, "project_id", "lari-ui-v2")
    resolver = CanonicalAuthorityResolver(sit)
    task = {
        "authority_id": "DECISION-020",
        "risk_class": "R0",
        "write_scope": ["src/app.tsx"],
    }
    # Should not raise AuthorityDenied because 'LARI' in repo_tokens matches 'LARI' in authority text
    resolver.validate_task(task)


def test_compile_execution_plan_authority_rejection_triggers_repair(tmp_path):
    sit = _situation()
    obj = Objective(
        objective_id="OBJ-TEST",
        title="Test Objective",
        description="Test description",
        authority_id="DECISION-020",
        risk_class="R0",
        rationale="Test rationale",
        scope_tags=("phase-2-7",),
        completion_criteria=("Test criteria",),
        parallel_candidates=(),
    )

    attempts = 0

    class MockBackend:
        def execute(self, request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                # First proposal emits task with dangerous pattern (e.g. force push in payload)
                plan = {
                    "schema_version": "1.0.0",
                    "objective_id": "OBJ-TEST",
                    "parallel_safe_groups": [["t1"]],
                    "rollback_strategy": "revert",
                    "tasks": [{
                        "node_id": "t1",
                        "run_type": "FILE",
                        "authority_id": "DECISION-020",
                        "risk_class": "R0",
                        "mutating": True,
                        "dependencies": [],
                        "scope_tags": ["phase-2-7"],
                        "write_scope": ["SECURITY_TODO.md"],
                        "payload": {
                            "action": "apply_patch",
                            "patch": "--- a/SECURITY_TODO.md\n+++ b/SECURITY_TODO.md\n@@\n-old\n+force-push",
                        },
                        "expected_artifacts": ["SECURITY_TODO.md"],
                        "tests": [],
                        "evidence_requirements": [],
                        "completion_criteria": [],
                    }],
                }
            else:
                # Corrected proposal in repair attempt
                plan = {
                    "schema_version": "1.0.0",
                    "objective_id": "OBJ-TEST",
                    "parallel_safe_groups": [["t1"]],
                    "rollback_strategy": "revert",
                    "tasks": [{
                        "node_id": "t1",
                        "run_type": "FILE",
                        "authority_id": "DECISION-020",
                        "risk_class": "R0",
                        "mutating": True,
                        "dependencies": [],
                        "scope_tags": ["phase-2-7"],
                        "write_scope": ["SECURITY_TODO.md"],
                        "payload": {
                            "action": "apply_patch",
                            "patch": "--- a/SECURITY_TODO.md\n+++ b/SECURITY_TODO.md\n@@\n-old\n+safe-update",
                        },
                        "expected_artifacts": ["SECURITY_TODO.md"],
                        "tests": [],
                        "evidence_requirements": [],
                        "completion_criteria": [],
                    }],
                }
            res = SimpleNamespace(
                status="SUCCESS",
                evidence_payload={"proposal": plan},
            )
            return res

    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{}", encoding="utf-8")

    plan = compile_execution_plan(
        sit,
        obj,
        policy_path,
        tmp_path,
        backend_override=MockBackend(),
        batch_number=1,
    )

    assert attempts == 2
    assert plan["tasks"][0]["node_id"] == "t1"
    assert "safe-update" in plan["tasks"][0]["payload"]["patch"]


def test_durable_batch_history_recovery_and_monotonicity_over_60_batches(tmp_path):
    """Prove durable batch history recovery across WAITING and process restarts for >60 batches.

    Ensures:
    1. Running >30 batches bounds completed_batches display window to 30 while total_completed_batch_count grows monotonically.
    2. WAITING_FOR_REASONING_PROVIDER checkpoint preserves total_completed_batch_count, successful_batch_count, and failed_batch_count.
    3. Restarting from checkpoint and disk reconstructs all executed batches and deduplication context.
    4. Old completed task IDs and signatures remain forbidden and are not re-executed.
    5. Partial/failed batches are tracked accurately and not misclassified as successful.
    """
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{}", encoding="utf-8")

    # Phase 1: Run 35 batches (some successful, some partial/failed)
    total_planned = 35
    executed_batches = []

    def mock_executor(plan_path: Path, batch_runtime: Path, resume: bool):
        b_name = batch_runtime.name
        # Every 5th batch has a failed task (partial/failed)
        batch_idx = int(b_name.split("-")[1])
        if batch_idx % 5 == 0:
            receipt = {
                "progress": 50.0,
                "completed_task_ids": [f"task-{batch_idx}-a"],
                "failed_task_ids": [f"task-{batch_idx}-b"],
                "timestamp": "2026-09-20T00:00:00+00:00",
            }
        else:
            receipt = {
                "progress": 100.0,
                "completed_task_ids": [f"task-{batch_idx}-a", f"task-{batch_idx}-b"],
                "failed_task_ids": [],
                "timestamp": "2026-09-20T00:00:00+00:00",
            }
        # Write receipt to host-receipt.json in batch_runtime so reconstruct_batch_history can scan it
        batch_runtime.mkdir(parents=True, exist_ok=True)
        (batch_runtime / "host-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        executed_batches.append((batch_idx, receipt))
        return receipt

    # Backend that provides objectives and plans for 35 batches
    class TestBackend:
        def __init__(self, wait_at_batch=None):
            self.wait_at_batch = wait_at_batch
            self.current_batch = 0
            self.forbidden_tasks_seen = []

        def execute(self, request):
            prompt = request.payload.get("prompt", "")
            if "completion detector" in prompt or "DAG_EMPTY is NOT PROJECT_COMPLETE" in prompt:
                return SimpleNamespace(status="SUCCESS", evidence_payload={"proposal": {
                    "disposition": "REPLAN",
                    "rationale": "Continue authorized roadmap work",
                    "satisfied_criteria": [],
                    "unsatisfied_criteria": ["All roadmap work complete"],
                }})
            elif "OBJECTIVE_SELECT_TASK" in prompt or "objective selector" in prompt or "Select the next bounded" in prompt:
                obj = _objective()
                obj["objective_id"] = f"OBJ-{self.current_batch}"
                return SimpleNamespace(status="SUCCESS", evidence_payload={"proposal": obj})
            elif "Planner->DAG compiler" in prompt or "TASK_COMPILATION" in prompt or "Compile execution plan" in prompt or "COMPILE_PLAN" in prompt:
                forbidden = request.payload.get("forbidden_task_ids", [])
                self.forbidden_tasks_seen.append(list(forbidden))
                if self.wait_at_batch is not None and self.current_batch == self.wait_at_batch:
                    return SimpleNamespace(
                        status="WAITING_FOR_REASONING_PROVIDER",
                        evidence_payload={"failure_class": "ALL_ELIGIBLE_REASONING_PROVIDERS_UNAVAILABLE"},
                    )
                plan = _plan()
                m = re.search(r'"objective_id":\s*"([^"]+)"', prompt)
                current_obj_id = m.group(1) if m else f"OBJ-{self.current_batch}"
                plan["objective_id"] = current_obj_id
                plan["tasks"] = [
                    {
                        "node_id": f"task-{self.current_batch}-a",
                        "run_type": "PROCESS",
                        "authority_id": "DECISION-020",
                        "risk_class": "R0",
                        "mutating": False,
                        "dependencies": [],
                        "scope_tags": ["phase-1"],
                        "write_scope": [],
                        "payload": {"cmd": ["git", "status"]},
                        "expected_artifacts": [],
                        "tests": [],
                        "evidence_requirements": [],
                        "completion_criteria": [],
                    },
                    {
                        "node_id": f"task-{self.current_batch}-b",
                        "run_type": "PROCESS",
                        "authority_id": "DECISION-020",
                        "risk_class": "R0",
                        "mutating": False,
                        "dependencies": [],
                        "scope_tags": ["phase-1"],
                        "write_scope": [],
                        "payload": {"cmd": ["git", "branch"]},
                        "expected_artifacts": [],
                        "tests": [],
                        "evidence_requirements": [],
                        "completion_criteria": [],
                    },
                ]
                self.current_batch += 1
                return SimpleNamespace(status="SUCCESS", evidence_payload={"proposal": plan})
            return SimpleNamespace(status="SUCCESS", evidence_payload={"proposal": {}})

    backend_p1 = TestBackend()
    res1 = run_autonomous_project(
        descriptor_path=descriptor_path,
        workspace=workspace,
        runtime_dir=runtime_dir,
        routing_policy_path=policy_path,
        backend_override=backend_p1,
        situation_factory=lambda **kwargs: _situation(),
        batch_executor=mock_executor,
        max_batches=35,
    )

    assert res1["disposition"] == "BOUNDED_RUN_EXHAUSTED"
    assert res1["batch_number"] == 35
    assert res1["total_completed_batch_count"] == 35
    assert res1["completed_batch_count"] == 35
    # Window of recent completed batches is bounded to 30
    assert len(res1["completed_batches"]) == 30
    assert len(res1["recent_completed_batches"]) == 30

    ckpt1 = json.loads((runtime_dir / "planning-kernel-checkpoint.json").read_text(encoding="utf-8"))
    assert ckpt1["batch_number"] == 35
    assert ckpt1["total_completed_batch_count"] == 35
    assert len(ckpt1["completed_batches"]) == 30
    # 35 batches: 0, 5, 10, 15, 20, 25, 30 are failed (7 batches), 28 successful
    assert ckpt1["failed_batch_count"] == 7
    assert ckpt1["successful_batch_count"] == 28

    # Phase 2: Encounter WAITING_FOR_REASONING_PROVIDER at batch 35
    backend_p2 = TestBackend(wait_at_batch=35)
    backend_p2.current_batch = 35
    res2 = run_autonomous_project(
        descriptor_path=descriptor_path,
        workspace=workspace,
        runtime_dir=runtime_dir,
        routing_policy_path=policy_path,
        backend_override=backend_p2,
        situation_factory=lambda **kwargs: _situation(),
        batch_executor=mock_executor,
        max_batches=1,
    )

    assert res2["disposition"] == "WAITING_FOR_REASONING_PROVIDER"
    assert res2["batch_number"] == 35
    assert res2["total_completed_batch_count"] == 35
    assert len(res2["completed_batches"]) == 30

    ckpt2 = json.loads((runtime_dir / "planning-kernel-checkpoint.json").read_text(encoding="utf-8"))
    assert ckpt2["phase"] == "WAITING_FOR_REASONING_PROVIDER"
    assert ckpt2["batch_number"] == 35
    assert ckpt2["total_completed_batch_count"] == 35

    # Phase 3: Recover and run 30 more batches (total 65 batches)
    backend_p3 = TestBackend()
    backend_p3.current_batch = 35
    res3 = run_autonomous_project(
        descriptor_path=descriptor_path,
        workspace=workspace,
        runtime_dir=runtime_dir,
        routing_policy_path=policy_path,
        backend_override=backend_p3,
        situation_factory=lambda **kwargs: _situation(),
        batch_executor=mock_executor,
        max_batches=30,
    )

    assert res3["disposition"] == "BOUNDED_RUN_EXHAUSTED"
    assert res3["batch_number"] == 65
    assert res3["total_completed_batch_count"] == 65
    assert res3["completed_batch_count"] == 65
    assert len(res3["completed_batches"]) == 30
    assert len(res3["recent_completed_batches"]) == 30

    # 65 batches: batches 0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60 are failed (13 batches), 52 successful
    assert res3["failed_batch_count"] == 13
    assert res3["successful_batch_count"] == 52

    ckpt3 = json.loads((runtime_dir / "planning-kernel-checkpoint.json").read_text(encoding="utf-8"))
    assert ckpt3["batch_number"] == 65
    assert ckpt3["total_completed_batch_count"] == 65
    assert ckpt3["failed_batch_count"] == 13
    assert ckpt3["successful_batch_count"] == 52
    assert len(ckpt3["completed_batches"]) == 30

    # Verify that reconstruct_batch_history reconstructs all 65 batches accurately
    history = planning_kernel.reconstruct_batch_history(runtime_dir, workspace=workspace)
    assert history.total_executed_batches == 65
    assert history.successful_batches == 52
    assert history.failed_batches == 13
    assert history.highest_batch_number == 64
    assert len(history.recent_completed_batches) == 30
    assert len(history.durable_batches) == 65

    # Verify that task from early batch (e.g. task-0-a) is in cumulative completed task IDs
    assert "task-0-a" in history.completed_task_ids
    assert "task-34-a" in history.completed_task_ids
    assert "task-64-a" in history.completed_task_ids
