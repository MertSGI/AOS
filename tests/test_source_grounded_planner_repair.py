import dataclasses
import hashlib
import json
import pytest
from pathlib import Path

from aos.planning_kernel import (
    Objective,
    PlanningKernelError,
    ProjectSituation,
    AuthorityRecord,
    _bounded_workspace_source_context,
    _validate_file_patch_tasks,
    _PLANNER_READ_CONTEXT_SUFFIXES,
)


def _make_situation(workspace_dir: Path, *, project_id="lari-ui-v2") -> ProjectSituation:
    rec = AuthorityRecord(
        authority_id="DECISION-024",
        source_path="DECISIONS.md",
        text="DECISION-024 STANDING AUTHORITY MarketingLayout accessibility MertSGI/Randapp-main lari-ui-v2 R0 R1",
        superseded=False,
        production_allowed=False,
    )
    return ProjectSituation(
        schema_version="1.0.0",
        project_id=project_id,
        repository="MertSGI/Randapp-main",
        control_ref="control/lari-project-control-plane",
        control_sha="14589d3a68a1654281ceeec2f3b9445fbbaa807a",
        repository_head="06ebf176e411faf6820ded34987f894cc5e7dc9e",
        execution_base_sha="06ebf176e411faf6820ded34987f894cc5e7dc9e",
        current_status="ACTIVE",
        current_milestone="UI V2 Convergence",
        canonical_next_action="Address MarketingLayout accessibility",
        canonical_hashes={},
        canonical_excerpt="",
        working_tree_state="CLEAN",
        ci_state=[],
        accepted_gates=["NODE2=ACCEPTED"],
        blocked_gates=[],
        authority_records={"DECISION-024": rec},
        goal="Complete MarketingLayout accessibility correction.",
        constraints=(),
        red_lines=("production activation",),
        completion_criteria=("MarketingLayout corrected",),
        ambiguity_reasons=(),
        captured_at="2026-10-10T00:00:00+00:00",
        lane_allowed_scope=("components/layouts/MarketingLayout.tsx", "big_file.ts"),
    )


def _make_objective(
    obj_id: str = "OBJ-A11Y",
    title: str = "MarketingLayout accessibility",
    scope_tags: tuple = ("components/layouts/MarketingLayout.tsx",),
) -> Objective:
    return Objective(
        objective_id=obj_id,
        title=title,
        description="Fix accessibility issues",
        authority_id="DECISION-024",
        risk_class="R0",
        rationale="Product accessibility",
        scope_tags=scope_tags,
        completion_criteria=("Applied cleanly",),
        parallel_candidates=(),
    )


def _sample_marketing_layout_content() -> str:
    return (
        "import React, { useState } from 'react';\n"
        "import { Link, useLocation } from 'react-router-dom';\n"
        "import { useLanguage } from '../../contexts/LanguageContext';\n"
        "\n"
        "export const MarketingLayout: React.FC = () => {\n"
        "  const { language } = useLanguage();\n"
        "  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);\n"
        "  const closeMobileMenu = () => setMobileMenuOpen(false);\n"
        "\n"
        "  return (\n"
        "    <div>\n"
        "      <nav aria-label=\"Main navigation\">\n"
        "        <Link to=\"/\">Home</Link>\n"
        "      </nav>\n"
        "    </div>\n"
        "  );\n"
        "};\n"
    )


# 1. Current TSX working-tree source is selected and SHA-bound.
def test_source_grounding_selects_tsx_and_binds_sha(tmp_path: Path):
    target = tmp_path / "components" / "layouts" / "MarketingLayout.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    content = _sample_marketing_layout_content()
    target.write_text(content, encoding="utf-8")
    expected_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    objective = _make_objective()
    situation = _make_situation(tmp_path)

    ctx = _bounded_workspace_source_context(tmp_path, objective, situation)
    assert ctx["status"] == "AVAILABLE"
    assert ctx["captured_count"] == 1
    file_info = ctx["files"][0]
    assert file_info["path"] == "components/layouts/MarketingLayout.tsx"
    assert file_info["raw_sha256"] == expected_sha
    assert "closeMobileMenu" in file_info["content"]


# 2. Actual uncommitted file content takes precedence over stale Git HEAD content.
def test_uncommitted_working_tree_content_precedence(tmp_path: Path):
    target = tmp_path / "components" / "layouts" / "MarketingLayout.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Working tree has an uncommitted modification
    uncommitted_content = _sample_marketing_layout_content() + "// UNCOMMITTED WORKTREE EDIT\n"
    target.write_text(uncommitted_content, encoding="utf-8")
    uncommitted_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    objective = _make_objective()
    situation = _make_situation(tmp_path)

    ctx = _bounded_workspace_source_context(tmp_path, objective, situation)
    assert ctx["files"][0]["raw_sha256"] == uncommitted_sha
    assert "UNCOMMITTED WORKTREE EDIT" in ctx["files"][0]["content"]


# 3. A patch referencing nonexistent React imports is rejected before execution.
def test_patch_with_nonexistent_imports_rejected(tmp_path: Path):
    target = tmp_path / "components" / "layouts" / "MarketingLayout.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_sample_marketing_layout_content(), encoding="utf-8")

    # Hallucinated import patch from Nemotron failure
    hallucinated_patch = (
        "--- a/components/layouts/MarketingLayout.tsx\n"
        "+++ b/components/layouts/MarketingLayout.tsx\n"
        "@@ -1,5 +1,5 @@\n"
        " import React, { useState, useEffect } from 'react';\n"
        "-import { Link, useLocation } from 'react-router-dom';\n"
        "+import { Link, useLocation, NavLink } from 'react-router-dom';\n"
        " import { useLanguage } from '../../contexts/LanguageContext';\n"
        " import { useAuth } from '../../contexts/AuthContext';\n"
    )

    tasks = [{
        "node_id": "apply-hallucinated-patch",
        "run_type": "FILE",
        "mutating": True,
        "write_scope": ["components/layouts/MarketingLayout.tsx"],
        "payload": {
            "action": "apply_patch",
            "patch": hallucinated_patch,
        },
    }]

    with pytest.raises(PlanningKernelError) as exc_info:
        _validate_file_patch_tasks(tasks, tmp_path)
    assert "context mismatch against current working-tree content" in str(exc_info.value)


# 4. A valid patch grounded in exact current TSX source is admitted.
def test_valid_grounded_patch_admitted(tmp_path: Path):
    target = tmp_path / "components" / "layouts" / "MarketingLayout.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_sample_marketing_layout_content(), encoding="utf-8")
    actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    valid_patch = (
        "--- a/components/layouts/MarketingLayout.tsx\n"
        "+++ b/components/layouts/MarketingLayout.tsx\n"
        "@@ -7,3 +7,2 @@\n"
        "   const [mobileMenuOpen, setMobileMenuOpen] = useState(false);\n"
        "-  const closeMobileMenu = () => setMobileMenuOpen(false);\n"
        " \n"
        "@@ -11,4 +10,4 @@\n"
        "     <div>\n"
        "-      <nav aria-label=\"Main navigation\">\n"
        "+      <nav aria-label={language === 'tr' ? 'Ana gezinme' : 'Main navigation'}>\n"
        "         <Link to=\"/\">Home</Link>\n"
        "\n"
    )

    tasks = [{
        "node_id": "apply-valid-patch",
        "run_type": "FILE",
        "mutating": True,
        "write_scope": ["components/layouts/MarketingLayout.tsx"],
        "payload": {
            "action": "apply_patch",
            "patch": valid_patch,
            "precondition_shas": {
                "components/layouts/MarketingLayout.tsx": actual_sha,
            },
        },
    }]

    # Should not raise
    _validate_file_patch_tasks(tasks, tmp_path)


# 5. A no-op patch is rejected.
def test_noop_patch_rejected(tmp_path: Path):
    target = tmp_path / "components" / "layouts" / "MarketingLayout.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_sample_marketing_layout_content(), encoding="utf-8")

    # Valid context, but zero replacement difference (no-op patch)
    noop_patch = (
        "--- a/components/layouts/MarketingLayout.tsx\n"
        "+++ b/components/layouts/MarketingLayout.tsx\n"
        "@@ -10,5 +10,5 @@\n"
        "   return (\n"
        "     <div>\n"
        "-      <nav aria-label=\"Main navigation\">\n"
        "+      <nav aria-label=\"Main navigation\">\n"
        "         <Link to=\"/\">Home</Link>\n"
        "       </nav>\n"
        "\n"
    )

    tasks = [{
        "node_id": "apply-noop-patch",
        "run_type": "FILE",
        "mutating": True,
        "write_scope": ["components/layouts/MarketingLayout.tsx"],
        "payload": {
            "action": "apply_patch",
            "patch": noop_patch,
        },
    }]

    with pytest.raises(PlanningKernelError) as exc_info:
        _validate_file_patch_tasks(tasks, tmp_path)
    assert "NO_OP_PATCH" in str(exc_info.value)


# 6. Source SHA changes between planning and execution fail closed.
def test_source_sha_mismatch_fails_closed(tmp_path: Path):
    target = tmp_path / "components" / "layouts" / "MarketingLayout.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_sample_marketing_layout_content(), encoding="utf-8")

    valid_patch = (
        "--- a/components/layouts/MarketingLayout.tsx\n"
        "+++ b/components/layouts/MarketingLayout.tsx\n"
        "@@ -7,3 +7,2 @@\n"
        "   const [mobileMenuOpen, setMobileMenuOpen] = useState(false);\n"
        "-  const closeMobileMenu = () => setMobileMenuOpen(false);\n"
        " \n"
    )

    tasks = [{
        "node_id": "apply-patch-drift",
        "run_type": "FILE",
        "mutating": True,
        "write_scope": ["components/layouts/MarketingLayout.tsx"],
        "payload": {
            "action": "apply_patch",
            "patch": valid_patch,
            "precondition_shas": {
                "components/layouts/MarketingLayout.tsx": "0000000000000000000000000000000000000000000000000000000000000000",
            },
        },
    }]

    with pytest.raises(PlanningKernelError) as exc_info:
        _validate_file_patch_tasks(tasks, tmp_path)
    assert "precondition SHA mismatch" in str(exc_info.value)


# 7. Path escape and sensitive-file attempts are denied.
def test_path_escape_and_sensitive_file_denied(tmp_path: Path):
    secret = tmp_path / ".env"
    secret.write_text("SECRET_KEY=12345\n", encoding="utf-8")

    objective = _make_objective(
        obj_id="OBJ-EVIL",
        title="Steal .env",
        scope_tags=(".env", "../outside.txt"),
    )
    situation = _make_situation(tmp_path)

    ctx = _bounded_workspace_source_context(tmp_path, objective, situation)
    # Sensitive file .env and escaped paths must not be captured
    assert ctx["captured_count"] == 0

    escape_patch = (
        "--- a/../escaped.txt\n"
        "+++ b/../escaped.txt\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    tasks = [{
        "node_id": "escape-task",
        "run_type": "FILE",
        "mutating": True,
        "write_scope": ["../escaped.txt"],
        "payload": {
            "action": "apply_patch",
            "patch": escape_patch,
        },
    }]
    with pytest.raises(PlanningKernelError) as exc_info:
        _validate_file_patch_tasks(tasks, tmp_path)
    assert "escapes managed workspace" in str(exc_info.value) or "outside declared write_scope" in str(exc_info.value)


# 8. Source context size limits are enforced.
def test_source_context_size_limits_enforced(tmp_path: Path):
    target = tmp_path / "big_file.ts"
    # Write 20KB of TS code
    target.write_text("const x = 1;\n" * 1500, encoding="utf-8")

    objective = _make_objective(
        obj_id="OBJ-BIG",
        title="Big file",
        scope_tags=("big_file.ts",),
    )
    situation = _make_situation(tmp_path)

    ctx = _bounded_workspace_source_context(
        tmp_path, objective, situation, max_chars_per_file=500, max_total_chars=1000
    )
    assert ctx["captured_count"] == 1
    assert len(ctx["files"][0]["content"]) <= 500


# 9. Previously supported extensions and planning scenarios remain functional.
def test_extension_support_and_backwards_compatibility():
    for ext in [".md", ".json", ".yaml", ".tsx", ".ts", ".jsx", ".js", ".mjs", ".py"]:
        assert ext in _PLANNER_READ_CONTEXT_SUFFIXES


# ==============================================================================
# PHASE C: MANDATORY END-TO-END PLANNER-TO-DAG-TO-NATIVEFILEWORKER INTEGRATION TESTS
# ==============================================================================

from aos.planning_kernel import compile_execution_plan, PlannerValidationExhausted
from extensions.autonomy_fabric.native_workers import NativeFileWorker
from extensions.autonomy_fabric.execution_backend import ExecutionRequest, ExecutionCapability
from types import SimpleNamespace


class _FakeQueueBackend:
    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        if not self.proposals:
            raise AssertionError("Unexpected additional reasoning call")
        proposal = self.proposals.pop(0)
        return SimpleNamespace(
            status="SUCCESS",
            evidence_payload={"proposal": proposal, "provider_route": "fake"},
        )


def _base_plan_template(task_dict: dict) -> dict:
    return {
        "schema_version": "1.0.0",
        "objective_id": "OBJ-A11Y",
        "tasks": [task_dict],
        "parallel_safe_groups": [[task_dict["node_id"]]],
        "rollback_strategy": "Safe revert",
    }


# Case A: Compile a fake-model plan containing a hallucinated MarketingLayout import.
# Verify rejection before NativeFileWorker executes.
def test_e2e_hallucinated_patch_rejected_before_worker_execution(tmp_path: Path):
    target = tmp_path / "components" / "layouts" / "MarketingLayout.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_sample_marketing_layout_content(), encoding="utf-8")
    original_bytes = target.read_bytes()

    hallucinated_patch = (
        "--- a/components/layouts/MarketingLayout.tsx\n"
        "+++ b/components/layouts/MarketingLayout.tsx\n"
        "@@ -1,5 +1,5 @@\n"
        " import React, { useState, useEffect } from 'react';\n"
        "-import { Link, useLocation } from 'react-router-dom';\n"
        "+import { Link, useLocation, NavLink } from 'react-router-dom';\n"
        " import { useLanguage } from '../../contexts/LanguageContext';\n"
        " import { useAuth } from '../../contexts/AuthContext';\n"
    )

    bad_plan = _base_plan_template({
        "node_id": "hallucinated-patch-task",
        "run_type": "FILE",
        "authority_id": "DECISION-024",
        "risk_class": "R0",
        "mutating": True,
        "dependencies": [],
        "scope_tags": ["components/layouts/MarketingLayout.tsx"],
        "write_scope": ["components/layouts/MarketingLayout.tsx"],
        "payload": {
            "action": "apply_patch",
            "patch": hallucinated_patch,
        },
        "expected_artifacts": ["components/layouts/MarketingLayout.tsx"],
        "tests": ["self"],
        "evidence_requirements": ["clean patch"],
        "completion_criteria": ["patched"],
    })

    # Both initial and repair attempts offer the bad plan to verify bounded exhaustion
    backend = _FakeQueueBackend([bad_plan, bad_plan])
    situation = _make_situation(tmp_path)
    objective = _make_objective()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{}", encoding="utf-8")

    with pytest.raises(PlannerValidationExhausted) as exc_info:
        compile_execution_plan(
            situation,
            objective,
            policy_path,
            tmp_path,
            backend_override=backend,
            workspace=tmp_path,
        )

    assert "context mismatch against current working-tree content" in str(exc_info.value)
    # File must be untouched
    assert target.read_bytes() == original_bytes


# Case B: Compile a source-grounded valid patch.
# Verify that the accepted DAG carries the correct raw-file SHA256 precondition.
def test_e2e_valid_patch_carries_raw_sha256_precondition(tmp_path: Path):
    target = tmp_path / "components" / "layouts" / "MarketingLayout.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_sample_marketing_layout_content(), encoding="utf-8")
    actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    valid_patch = (
        "--- a/components/layouts/MarketingLayout.tsx\n"
        "+++ b/components/layouts/MarketingLayout.tsx\n"
        "@@ -7,3 +7,2 @@\n"
        "   const [mobileMenuOpen, setMobileMenuOpen] = useState(false);\n"
        "-  const closeMobileMenu = () => setMobileMenuOpen(false);\n"
        " \n"
    )

    valid_plan = _base_plan_template({
        "node_id": "valid-patch-task",
        "run_type": "FILE",
        "authority_id": "DECISION-024",
        "risk_class": "R0",
        "mutating": True,
        "dependencies": [],
        "scope_tags": ["components/layouts/MarketingLayout.tsx"],
        "write_scope": ["components/layouts/MarketingLayout.tsx"],
        "payload": {
            "action": "apply_patch",
            "patch": valid_patch,
        },
        "expected_artifacts": ["components/layouts/MarketingLayout.tsx"],
        "tests": ["self"],
        "evidence_requirements": ["clean patch"],
        "completion_criteria": ["patched"],
    })

    backend = _FakeQueueBackend([valid_plan])
    situation = _make_situation(tmp_path)
    objective = _make_objective()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{}", encoding="utf-8")

    plan = compile_execution_plan(
        situation,
        objective,
        policy_path,
        tmp_path,
        backend_override=backend,
        workspace=tmp_path,
    )

    task_payload = plan["tasks"][0]["payload"]
    assert "precondition_shas" in task_payload
    assert task_payload["precondition_shas"]["components/layouts/MarketingLayout.tsx"] == actual_sha

    # Dispatch to real NativeFileWorker and verify execution success
    worker = NativeFileWorker()
    req = ExecutionRequest(
        task_id="valid-patch-task",
        project_id="lari-ui-v2",
        workspace=str(tmp_path),
        operation_class="FILE_PATCH",
        required_capabilities=[ExecutionCapability.FILE_WRITE],
        authority_id="DECISION-024",
        write_scope=["components/layouts/MarketingLayout.tsx"],
        payload=task_payload,
    )
    result = worker.execute(req)
    assert result.status == "SUCCESS"
    assert "closeMobileMenu" not in target.read_text(encoding="utf-8")


# Case C: Change the source bytes after plan acceptance but before NativeFileWorker execution.
# Verify the actual worker fails closed without mutation.
def test_e2e_drift_after_acceptance_fails_closed_in_worker(tmp_path: Path):
    target = tmp_path / "components" / "layouts" / "MarketingLayout.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_sample_marketing_layout_content(), encoding="utf-8")

    valid_patch = (
        "--- a/components/layouts/MarketingLayout.tsx\n"
        "+++ b/components/layouts/MarketingLayout.tsx\n"
        "@@ -7,3 +7,2 @@\n"
        "   const [mobileMenuOpen, setMobileMenuOpen] = useState(false);\n"
        "-  const closeMobileMenu = () => setMobileMenuOpen(false);\n"
        " \n"
    )

    valid_plan = _base_plan_template({
        "node_id": "valid-patch-task",
        "run_type": "FILE",
        "authority_id": "DECISION-024",
        "risk_class": "R0",
        "mutating": True,
        "dependencies": [],
        "scope_tags": ["components/layouts/MarketingLayout.tsx"],
        "write_scope": ["components/layouts/MarketingLayout.tsx"],
        "payload": {
            "action": "apply_patch",
            "patch": valid_patch,
        },
        "expected_artifacts": ["components/layouts/MarketingLayout.tsx"],
        "tests": ["self"],
        "evidence_requirements": ["clean patch"],
        "completion_criteria": ["patched"],
    })

    backend = _FakeQueueBackend([valid_plan])
    situation = _make_situation(tmp_path)
    objective = _make_objective()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{}", encoding="utf-8")

    plan = compile_execution_plan(
        situation,
        objective,
        policy_path,
        tmp_path,
        backend_override=backend,
        workspace=tmp_path,
    )
    task_payload = plan["tasks"][0]["payload"]

    # Source bytes change externally (DRIFT) before worker execution
    drifted_content = _sample_marketing_layout_content() + "\n// EXTERNAL DRIFT\n"
    target.write_text(drifted_content, encoding="utf-8")
    drifted_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    worker = NativeFileWorker()
    req = ExecutionRequest(
        task_id="valid-patch-task",
        project_id="lari-ui-v2",
        workspace=str(tmp_path),
        operation_class="FILE_PATCH",
        required_capabilities=[ExecutionCapability.FILE_WRITE],
        authority_id="DECISION-024",
        write_scope=["components/layouts/MarketingLayout.tsx"],
        payload=task_payload,
    )
    result = worker.execute(req)
    assert result.status == "FAILED"
    assert any("Precondition SHA mismatch" in err for err in result.sanitized_errors)
    # Drifted content is preserved; no corrupted patch applied
    assert target.read_text(encoding="utf-8") == drifted_content


# Case D: Verify that a source-bound existing-file write cannot bypass the SHA requirement,
# while authorized creation of genuinely new files succeeds.
def test_e2e_existing_file_write_sha_binding_and_new_file_creation(tmp_path: Path):
    target = tmp_path / "components" / "layouts" / "MarketingLayout.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_sample_marketing_layout_content(), encoding="utf-8")
    actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    # 1. Existing file write without SHA provided: planner binds actual raw SHA256
    write_plan = _base_plan_template({
        "node_id": "write-existing-task",
        "run_type": "FILE",
        "authority_id": "DECISION-024",
        "risk_class": "R0",
        "mutating": True,
        "dependencies": [],
        "scope_tags": ["components/layouts/MarketingLayout.tsx"],
        "write_scope": ["components/layouts/MarketingLayout.tsx"],
        "payload": {
            "action": "write_file",
            "path": "components/layouts/MarketingLayout.tsx",
            "content": "// Brand new complete content\nexport const MarketingLayout = () => null;\n",
        },
        "expected_artifacts": ["components/layouts/MarketingLayout.tsx"],
        "tests": ["self"],
        "evidence_requirements": ["write complete"],
        "completion_criteria": ["written"],
    })

    backend = _FakeQueueBackend([write_plan])
    situation = _make_situation(tmp_path)
    objective = _make_objective()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{}", encoding="utf-8")

    plan = compile_execution_plan(
        situation,
        objective,
        policy_path,
        tmp_path,
        backend_override=backend,
        workspace=tmp_path,
    )
    assert plan["tasks"][0]["payload"]["precondition_sha"] == actual_sha

    # 2. Existing file write with mismatched SHA: planner rejects
    bad_sha_plan = _base_plan_template({
        "node_id": "write-bad-sha-task",
        "run_type": "FILE",
        "authority_id": "DECISION-024",
        "risk_class": "R0",
        "mutating": True,
        "dependencies": [],
        "scope_tags": ["components/layouts/MarketingLayout.tsx"],
        "write_scope": ["components/layouts/MarketingLayout.tsx"],
        "payload": {
            "action": "write_file",
            "path": "components/layouts/MarketingLayout.tsx",
            "content": "// Mismatched sha write\n",
            "precondition_sha": "f" * 64,
        },
        "expected_artifacts": ["components/layouts/MarketingLayout.tsx"],
        "tests": ["self"],
        "evidence_requirements": ["write complete"],
        "completion_criteria": ["written"],
    })
    backend_bad = _FakeQueueBackend([bad_sha_plan, bad_sha_plan])
    with pytest.raises(PlannerValidationExhausted) as exc_info:
        compile_execution_plan(
            situation,
            objective,
            policy_path,
            tmp_path,
            backend_override=backend_bad,
            workspace=tmp_path,
        )
    assert "precondition SHA mismatch" in str(exc_info.value)

    # 3. Genuinely new file write: accepted without precondition_sha requirement
    situation_new = _make_situation(tmp_path)
    # allow new file in lane_allowed_scope
    situation_new = dataclasses.replace(
        situation_new, lane_allowed_scope=("components/layouts/",)
    )
    new_file_plan = _base_plan_template({
        "node_id": "write-new-task",
        "run_type": "FILE",
        "authority_id": "DECISION-024",
        "risk_class": "R0",
        "mutating": True,
        "dependencies": [],
        "scope_tags": ["components/layouts/NewFile.tsx"],
        "write_scope": ["components/layouts/NewFile.tsx"],
        "payload": {
            "action": "write_file",
            "path": "components/layouts/NewFile.tsx",
            "content": "export const NewComponent = () => null;\n",
        },
        "expected_artifacts": ["components/layouts/NewFile.tsx"],
        "tests": ["self"],
        "evidence_requirements": ["write complete"],
        "completion_criteria": ["written"],
    })
    backend_new = _FakeQueueBackend([new_file_plan])
    plan_new = compile_execution_plan(
        situation_new,
        objective,
        policy_path,
        tmp_path,
        backend_override=backend_new,
        workspace=tmp_path,
    )
    assert plan_new["tasks"][0]["payload"].get("precondition_sha") is None

    # Worker executes new file successfully
    worker = NativeFileWorker()
    req_new = ExecutionRequest(
        task_id="write-new-task",
        project_id="lari-ui-v2",
        workspace=str(tmp_path),
        operation_class="FILE_WRITE",
        required_capabilities=[ExecutionCapability.FILE_WRITE],
        authority_id="DECISION-024",
        write_scope=["components/layouts/NewFile.tsx"],
        payload=plan_new["tasks"][0]["payload"],
    )
    res_new = worker.execute(req_new)
    assert res_new.status == "SUCCESS"
    assert (tmp_path / "components" / "layouts" / "NewFile.tsx").is_file()


# Case E: Verify that invalid generated patches use at most one bounded repair attempt.
def test_e2e_invalid_patch_bounded_repair_attempt(tmp_path: Path):
    target = tmp_path / "components" / "layouts" / "MarketingLayout.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_sample_marketing_layout_content(), encoding="utf-8")

    invalid_patch = (
        "--- a/components/layouts/MarketingLayout.tsx\n"
        "+++ b/components/layouts/MarketingLayout.tsx\n"
        "@@ -1,3 +1,3 @@\n"
        "-nonexistent context\n"
        "+replacement\n"
    )

    valid_patch = (
        "--- a/components/layouts/MarketingLayout.tsx\n"
        "+++ b/components/layouts/MarketingLayout.tsx\n"
        "@@ -7,3 +7,2 @@\n"
        "   const [mobileMenuOpen, setMobileMenuOpen] = useState(false);\n"
        "-  const closeMobileMenu = () => setMobileMenuOpen(false);\n"
        " \n"
    )

    invalid_plan = _base_plan_template({
        "node_id": "patch-task-1",
        "run_type": "FILE",
        "authority_id": "DECISION-024",
        "risk_class": "R0",
        "mutating": True,
        "dependencies": [],
        "scope_tags": ["components/layouts/MarketingLayout.tsx"],
        "write_scope": ["components/layouts/MarketingLayout.tsx"],
        "payload": {"action": "apply_patch", "patch": invalid_patch},
        "expected_artifacts": ["components/layouts/MarketingLayout.tsx"],
        "tests": ["self"],
        "evidence_requirements": ["clean"],
        "completion_criteria": ["patched"],
    })

    corrected_plan = _base_plan_template({
        "node_id": "patch-task-1",
        "run_type": "FILE",
        "authority_id": "DECISION-024",
        "risk_class": "R0",
        "mutating": True,
        "dependencies": [],
        "scope_tags": ["components/layouts/MarketingLayout.tsx"],
        "write_scope": ["components/layouts/MarketingLayout.tsx"],
        "payload": {"action": "apply_patch", "patch": valid_patch},
        "expected_artifacts": ["components/layouts/MarketingLayout.tsx"],
        "tests": ["self"],
        "evidence_requirements": ["clean"],
        "completion_criteria": ["patched"],
    })

    backend = _FakeQueueBackend([invalid_plan, corrected_plan])
    situation = _make_situation(tmp_path)
    objective = _make_objective()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{}", encoding="utf-8")

    plan = compile_execution_plan(
        situation,
        objective,
        policy_path,
        tmp_path,
        backend_override=backend,
        workspace=tmp_path,
    )
    # Exactly one repair attempt was used (calls == 2: attempt 0 rejected, attempt 1 succeeded)
    assert backend.calls == 2
    assert plan["tasks"][0]["node_id"] == "patch-task-1"


# Case F: Verify valid patch context, source scope, canonical identity, and existing behavior are preserved.
def test_e2e_valid_patch_context_and_scope_preserved(tmp_path: Path):
    target = tmp_path / "components" / "layouts" / "MarketingLayout.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_sample_marketing_layout_content(), encoding="utf-8")

    valid_patch = (
        "--- a/components/layouts/MarketingLayout.tsx\n"
        "+++ b/components/layouts/MarketingLayout.tsx\n"
        "@@ -10,5 +10,5 @@\n"
        "   return (\n"
        "     <div>\n"
        "-      <nav aria-label=\"Main navigation\">\n"
        "+      <nav aria-label=\"Updated navigation\">\n"
        "         <Link to=\"/\">Home</Link>\n"
    )

    plan_dict = _base_plan_template({
        "node_id": "valid-patch-scope-task",
        "run_type": "FILE",
        "authority_id": "DECISION-024",
        "risk_class": "R0",
        "mutating": True,
        "dependencies": [],
        "scope_tags": ["components/layouts/MarketingLayout.tsx"],
        "write_scope": ["components/layouts/MarketingLayout.tsx"],
        "payload": {"action": "apply_patch", "patch": valid_patch},
        "expected_artifacts": ["components/layouts/MarketingLayout.tsx"],
        "tests": ["self"],
        "evidence_requirements": ["clean"],
        "completion_criteria": ["patched"],
    })

    backend = _FakeQueueBackend([plan_dict])
    situation = _make_situation(tmp_path)
    objective = _make_objective()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{}", encoding="utf-8")

    plan = compile_execution_plan(
        situation,
        objective,
        policy_path,
        tmp_path,
        backend_override=backend,
        workspace=tmp_path,
    )
    assert plan["project_id"] == "lari-ui-v2"
    assert plan["tasks"][0]["authority_id"] == "DECISION-024"
    assert plan["tasks"][0]["write_scope"] == ["components/layouts/MarketingLayout.tsx"]


# Case G: Verify Git index-header handling and dependent same-file mutations fail safely.
def test_e2e_git_index_header_and_multiple_mutations_fail_safely(tmp_path: Path):
    target = tmp_path / "components" / "layouts" / "MarketingLayout.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_sample_marketing_layout_content(), encoding="utf-8")

    # 1. Patch with git index header (7-char blob sha)
    git_index_patch = (
        "diff --git a/components/layouts/MarketingLayout.tsx b/components/layouts/MarketingLayout.tsx\n"
        "index abc1234..def5678 100644\n"
        "--- a/components/layouts/MarketingLayout.tsx\n"
        "+++ b/components/layouts/MarketingLayout.tsx\n"
        "@@ -7,3 +7,2 @@\n"
        "   const [mobileMenuOpen, setMobileMenuOpen] = useState(false);\n"
        "-  const closeMobileMenu = () => setMobileMenuOpen(false);\n"
        " \n"
    )

    index_plan = _base_plan_template({
        "node_id": "index-patch-task",
        "run_type": "FILE",
        "authority_id": "DECISION-024",
        "risk_class": "R0",
        "mutating": True,
        "dependencies": [],
        "scope_tags": ["components/layouts/MarketingLayout.tsx"],
        "write_scope": ["components/layouts/MarketingLayout.tsx"],
        "payload": {"action": "apply_patch", "patch": git_index_patch},
        "expected_artifacts": ["components/layouts/MarketingLayout.tsx"],
        "tests": ["self"],
        "evidence_requirements": ["clean"],
        "completion_criteria": ["patched"],
    })

    backend_index = _FakeQueueBackend([index_plan, index_plan])
    situation = _make_situation(tmp_path)
    objective = _make_objective()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{}", encoding="utf-8")

    with pytest.raises(PlannerValidationExhausted) as exc_info:
        compile_execution_plan(
            situation,
            objective,
            policy_path,
            tmp_path,
            backend_override=backend_index,
            workspace=tmp_path,
        )
    assert "GIT_INDEX_HEADER_INCOMPATIBLE" in str(exc_info.value)

    # 2. Multiple mutations targeting the same file within one DAG
    valid_patch_1 = (
        "--- a/components/layouts/MarketingLayout.tsx\n"
        "+++ b/components/layouts/MarketingLayout.tsx\n"
        "@@ -7,3 +7,2 @@\n"
        "   const [mobileMenuOpen, setMobileMenuOpen] = useState(false);\n"
        "-  const closeMobileMenu = () => setMobileMenuOpen(false);\n"
        " \n"
    )
    multi_mutation_plan = {
        "schema_version": "1.0.0",
        "objective_id": "OBJ-A11Y",
        "tasks": [
            {
                "node_id": "task-patch-1",
                "run_type": "FILE",
                "authority_id": "DECISION-024",
                "risk_class": "R0",
                "mutating": True,
                "dependencies": [],
                "scope_tags": ["components/layouts/MarketingLayout.tsx"],
                "write_scope": ["components/layouts/MarketingLayout.tsx"],
                "payload": {"action": "apply_patch", "patch": valid_patch_1},
                "expected_artifacts": ["components/layouts/MarketingLayout.tsx"],
                "tests": ["self"],
                "evidence_requirements": ["clean"],
                "completion_criteria": ["patched"],
            },
            {
                "node_id": "task-write-2",
                "run_type": "FILE",
                "authority_id": "DECISION-024",
                "risk_class": "R0",
                "mutating": True,
                "dependencies": ["task-patch-1"],
                "scope_tags": ["components/layouts/MarketingLayout.tsx"],
                "write_scope": ["components/layouts/MarketingLayout.tsx"],
                "payload": {
                    "action": "write_file",
                    "path": "components/layouts/MarketingLayout.tsx",
                    "content": "// Second mutation to same file in DAG\n",
                },
                "expected_artifacts": ["components/layouts/MarketingLayout.tsx"],
                "tests": ["self"],
                "evidence_requirements": ["clean"],
                "completion_criteria": ["written"],
            },
        ],
        "parallel_safe_groups": [["task-patch-1"], ["task-write-2"]],
        "rollback_strategy": "Safe revert",
    }

    backend_multi = _FakeQueueBackend([multi_mutation_plan, multi_mutation_plan])
    with pytest.raises(PlannerValidationExhausted) as exc_info:
        compile_execution_plan(
            situation,
            objective,
            policy_path,
            tmp_path,
            backend_override=backend_multi,
            workspace=tmp_path,
        )
    assert "MULTIPLE_FILE_MUTATIONS_NOT_PERMITTED" in str(exc_info.value)
