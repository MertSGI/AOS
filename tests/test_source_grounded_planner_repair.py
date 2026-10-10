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
        text="DECISION-024 MarketingLayout accessibility",
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
