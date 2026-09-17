import dataclasses
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import aos.planning_kernel as planning_kernel

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
        "aos.planning_kernel.subprocess.run",
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

    context = _bounded_completed_read_context(
        runtime,
        workspace,
        [{"batch_number": 0, "receipt": {"completed_task_ids": ["read-roadmap", "read-env"]}}],
    )

    assert context["status"] == "AVAILABLE"
    assert context["completed_read_paths"] == ["ROADMAP.md"]
    assert context["files"][0]["redacted_excerpt"] == "Phase 2: implement the bounded API.\n"
    assert context["files"][0]["content_sha256"] == hashlib.sha256(roadmap.read_bytes()).hexdigest()
    assert "must-not-appear" not in json.dumps(context)


def test_completed_read_context_keeps_only_most_recent_bounded_file(tmp_path):
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    completed_batches = []
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
            "receipt": {"completed_task_ids": [f"read-{batch_number}"]},
        })

    context = _bounded_completed_read_context(runtime, workspace, completed_batches)

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

    result = compile_execution_plan(
        _situation(),
        Objective.from_dict(_objective()),
        tmp_path / "policy.json",
        tmp_path,
        backend_override=backend,
        forbidden_read_paths=["roadmap.md"],
        workspace=tmp_path,
    )

    assert result["tasks"][0]["run_type"] == "TEST"
    assert backend.calls == 2


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
    assert '"completed_read_paths": ["ROADMAP.md"]' in plan_prompt
    assert 'FILE:{\\"action\\":\\"read_file\\",\\"path\\":\\"ROADMAP.md\\"}' in plan_prompt


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


@pytest.mark.parametrize("path", ["notes/next_action.txt", "NEXT_ACTION.md", "notes/status-marker.json"])
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


def test_exhausted_plan_validation_waits_without_executing_invalid_tasks(tmp_path):
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
    assert result["disposition"] == "WAITING_FOR_REASONING_PROVIDER"
    assert result["reason"].startswith("PLANNER_VALIDATION_REPAIR_EXHAUSTED:")
    assert checkpoint["phase"] == "WAITING_FOR_REASONING_PROVIDER"
    assert backend.calls == 3
    assert executed == []
    assert not (tmp_path / "runtime" / "batches" / "batch-0000" / "generated-run-plan.json").exists()


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
