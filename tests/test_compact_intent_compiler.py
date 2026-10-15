"""Focused qualification regression suite for AOS RFC-001 Compact Intent Compiler.

Covers:
1. Valid compact intent compilation (BROWSER_VERIFY, RUN_DECLARED_TEST, READ_OBSERVED_SOURCE).
2. Unknown target_ref rejection.
3. Unknown verification_ref rejection.
4. Missing browser assertion specification rejection (SourceGroundedVerificationSpecMissing).
5. Invented route / command rejection.
6. Superseded authority rejection (CanonicalAuthorityResolver).
7. Out-of-scope mutation rejection (mutating must remain False).
8. Duplicate completed-action rejection (_task_signature matching forbidden signatures).
9. Source SHA / generation mismatch rejection.
10. Strict PLAN_SCHEMA validation envelope and tasks.
"""
import copy
import dataclasses
import json
from pathlib import Path
from typing import Any, Dict
import pytest

from jsonschema import Draft202012Validator, FormatChecker

from aos.intent_compiler import (
    PLAN_INTENT_SCHEMA,
    IntentCandidate,
    IntentCatalog,
    IntentCompilerError,
    SourceGroundedVerificationSpecMissing,
    UnknownTargetRefError,
    UnknownVerificationRefError,
    SourceGenerationMismatchError,
    MismatchedRouteVerificationSpecError,
    UnapprovedVerificationSpecError,
    UnverifiedTestCommandError,
    is_safe_test_command,
    build_intent_catalog,
    compile_intent_to_plan,
)
from aos.planning_kernel import (
    PLAN_SCHEMA,
    AuthorityDenied,
    AuthorityRecord,
    CanonicalAuthorityResolver,
    Objective,
    ProjectSituation,
    _normalize_plan_schema_envelope,
    _task_signature,
    _validate_worker_payload,
    is_implementation_objective,
)


@pytest.fixture
def base_situation() -> ProjectSituation:
    auth_valid = AuthorityRecord(
        authority_id="AUTH-VALID-01",
        source_path="docs/auth.md",
        text="Standing authority",
        superseded=False,
        production_allowed=False,
    )
    auth_superseded = AuthorityRecord(
        authority_id="AUTH-SUPERSEDED-02",
        source_path="docs/auth.md",
        text="Old authority",
        superseded=True,
        production_allowed=False,
    )
    return ProjectSituation(
        schema_version="1.0.0",
        project_id="test-proj",
        repository="MertSGI/AOS",
        control_ref="refs/heads/main",
        control_sha="a" * 40,
        repository_head="a" * 40,
        execution_base_sha="a" * 40,
        current_status="ACTIVE",
        current_milestone="M1",
        canonical_next_action="VERIFY",
        canonical_hashes={},
        canonical_excerpt="excerpt",
        working_tree_state="CLEAN",
        ci_state=[],
        accepted_gates=[],
        blocked_gates=[],
        authority_records={
            "AUTH-VALID-01": auth_valid,
            "AUTH-SUPERSEDED-02": auth_superseded,
        },
        goal="Verify UI routes and tests",
        constraints=(),
        red_lines=(),
        completion_criteria=(),
        ambiguity_reasons=(),
        captured_at="2026-10-15T00:00:00Z",
        lane_allowed_scope=("src/",),
    )


@pytest.fixture
def base_objective() -> Objective:
    return Objective(
        objective_id="OBJ-01",
        title="Verify UI Route",
        description="Verify UI route rendering",
        authority_id="AUTH-VALID-01",
        risk_class="R0",
        rationale="Ensure UI route is stable",
        scope_tags=("ui", "verification"),
        completion_criteria=("UI verified",),
        parallel_candidates=(),
    )


@pytest.fixture
def sample_catalog() -> IntentCatalog:
    candidates = (
        IntentCandidate(
            intent_type="BROWSER_VERIFY",
            target_ref="#/appointment/manage",
            verification_ref="#/appointment/manage",
            description="Verify UI route #/appointment/manage",
            payload_preview={
                "url": "http://127.0.0.1:5173/#/appointment/manage",
                "expected_route": "#/appointment/manage",
            },
        ),
        IntentCandidate(
            intent_type="RUN_DECLARED_TEST",
            target_ref="test:unit",
            description="Run declared test script: npm run test:unit",
            payload_preview={"cmd": ["npm", "run", "test:unit"]},
        ),
        IntentCandidate(
            intent_type="READ_OBSERVED_SOURCE",
            target_ref="src/app.tsx",
            description="Read observed workspace source: src/app.tsx",
            payload_preview={"action": "read_file", "path": "src/app.tsx"},
        ),
    )
    specs = {
        "#/appointment/manage": {
            "route": "#/appointment/manage",
            "assertions": [
                {"type": "selector_visible", "target": "#root"},
                {"type": "route_exact", "target": "#/appointment/manage"},
            ],
        }
    }
    return IntentCatalog(
        objective_id="OBJ-01",
        candidates=candidates,
        browser_verification_specs=specs,
        render_origin="http://127.0.0.1:5173",
    )


class TestCompactIntentCompiler:
    def test_valid_browser_verify_intent_compilation(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective, sample_catalog: IntentCatalog
    ):
        """Intent compiler compiles valid BROWSER_VERIFY intent into complete valid PLAN_SCHEMA task."""
        intent = {
            "schema_version": "1.0.0",
            "objective_id": "OBJ-01",
            "intent_type": "BROWSER_VERIFY",
            "target_ref": "#/appointment/manage",
            "verification_ref": "#/appointment/manage",
        }
        plan = compile_intent_to_plan(
            intent, sample_catalog, base_situation, base_objective, tmp_path, tmp_path
        )
        assert plan["schema_version"] == "1.0.0"
        assert plan["objective_id"] == "OBJ-01"
        assert len(plan["tasks"]) == 1
        task = plan["tasks"][0]
        assert task["run_type"] == "BROWSER"
        assert task["mutating"] is False
        assert task["risk_class"] == "R0"
        assert task["authority_id"] == "AUTH-VALID-01"
        assert task["payload"]["url"] == "http://127.0.0.1:5173/#/appointment/manage"
        assert task["payload"]["expected_route"] == "#/appointment/manage"
        assert len(task["payload"]["assertions"]) == 2

        # Validate against PLAN_SCHEMA
        validator = Draft202012Validator(PLAN_SCHEMA, format_checker=FormatChecker())
        validator.validate(plan)
        _validate_worker_payload(task["node_id"], task["run_type"], task["payload"])
        CanonicalAuthorityResolver(base_situation).validate_task(task)

    def test_valid_run_declared_test_compilation(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective, sample_catalog: IntentCatalog
    ):
        """Intent compiler compiles valid RUN_DECLARED_TEST intent into complete valid PLAN_SCHEMA task."""
        intent = {
            "schema_version": "1.0.0",
            "objective_id": "OBJ-01",
            "intent_type": "RUN_DECLARED_TEST",
            "target_ref": "test:unit",
        }
        plan = compile_intent_to_plan(
            intent, sample_catalog, base_situation, base_objective, tmp_path, tmp_path
        )
        assert len(plan["tasks"]) == 1
        task = plan["tasks"][0]
        assert task["run_type"] == "PROCESS"
        assert task["mutating"] is False
        assert task["payload"]["cmd"] == ["npm", "run", "test:unit"]
        Draft202012Validator(PLAN_SCHEMA).validate(plan)
        _validate_worker_payload(task["node_id"], task["run_type"], task["payload"])
        CanonicalAuthorityResolver(base_situation).validate_task(task)

    def test_valid_read_observed_source_compilation(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective, sample_catalog: IntentCatalog
    ):
        """Intent compiler compiles valid READ_OBSERVED_SOURCE intent into complete valid PLAN_SCHEMA task."""
        intent = {
            "schema_version": "1.0.0",
            "objective_id": "OBJ-01",
            "intent_type": "READ_OBSERVED_SOURCE",
            "target_ref": "src/app.tsx",
        }
        plan = compile_intent_to_plan(
            intent, sample_catalog, base_situation, base_objective, tmp_path, tmp_path
        )
        assert len(plan["tasks"]) == 1
        task = plan["tasks"][0]
        assert task["run_type"] == "FILE"
        assert task["mutating"] is False
        assert task["payload"]["action"] == "read_file"
        assert task["payload"]["path"] == "src/app.tsx"
        Draft202012Validator(PLAN_SCHEMA).validate(plan)
        _validate_worker_payload(task["node_id"], task["run_type"], task["payload"])
        CanonicalAuthorityResolver(base_situation).validate_task(task)

    def test_unknown_target_ref_rejected(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective, sample_catalog: IntentCatalog
    ):
        """Model cannot hallucinate an arbitrary uncataloged target_ref."""
        intent = {
            "schema_version": "1.0.0",
            "objective_id": "OBJ-01",
            "intent_type": "RUN_DECLARED_TEST",
            "target_ref": "malicious:eval",
        }
        with pytest.raises(UnknownTargetRefError):
            compile_intent_to_plan(
                intent, sample_catalog, base_situation, base_objective, tmp_path, tmp_path
            )

    def test_unknown_verification_ref_rejected(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective, sample_catalog: IntentCatalog
    ):
        """Model cannot provide a hallucinated verification_ref."""
        intent = {
            "schema_version": "1.0.0",
            "objective_id": "OBJ-01",
            "intent_type": "BROWSER_VERIFY",
            "target_ref": "#/appointment/manage",
            "verification_ref": "hallucinated-spec-id",
        }
        with pytest.raises(UnknownVerificationRefError):
            compile_intent_to_plan(
                intent, sample_catalog, base_situation, base_objective, tmp_path, tmp_path
            )

    def test_missing_browser_assertion_spec_rejected(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective
    ):
        """If accepted browser specification does not exist, compiler raises SourceGroundedVerificationSpecMissing."""
        catalog_empty_specs = IntentCatalog(
            objective_id="OBJ-01",
            candidates=(
                IntentCandidate(
                    intent_type="BROWSER_VERIFY",
                    target_ref="#/unverified/route",
                    description="Verify unverified route",
                    payload_preview={"url": "http://127.0.0.1:5173/#/unverified/route"},
                ),
            ),
            browser_verification_specs={},
            render_origin="http://127.0.0.1:5173",
        )
        intent = {
            "schema_version": "1.0.0",
            "objective_id": "OBJ-01",
            "intent_type": "BROWSER_VERIFY",
            "target_ref": "#/unverified/route",
        }
        with pytest.raises(SourceGroundedVerificationSpecMissing):
            compile_intent_to_plan(
                intent, catalog_empty_specs, base_situation, base_objective, tmp_path, tmp_path
            )

    def test_invented_route_and_command_rejection(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective, sample_catalog: IntentCatalog
    ):
        """Arbitrary invented routes or commands outside catalog are rejected."""
        intent = {
            "schema_version": "1.0.0",
            "objective_id": "OBJ-01",
            "intent_type": "BROWSER_VERIFY",
            "target_ref": "/admin/danger_zone",
        }
        with pytest.raises(UnknownTargetRefError):
            compile_intent_to_plan(
                intent, sample_catalog, base_situation, base_objective, tmp_path, tmp_path
            )

    def test_superseded_authority_rejected(
        self, tmp_path: Path, base_situation: ProjectSituation, sample_catalog: IntentCatalog
    ):
        """Tasks compiled under superseded authority are rejected by CanonicalAuthorityResolver."""
        obj_superseded = Objective(
            objective_id="OBJ-01",
            title="Verify UI Route",
            description="Verify UI route",
            authority_id="AUTH-SUPERSEDED-02",
            risk_class="R0",
            rationale="Rationale",
            scope_tags=("ui",),
            completion_criteria=(),
            parallel_candidates=(),
        )
        intent = {
            "schema_version": "1.0.0",
            "objective_id": "OBJ-01",
            "intent_type": "RUN_DECLARED_TEST",
            "target_ref": "test:unit",
        }
        plan = compile_intent_to_plan(
            intent, sample_catalog, base_situation, obj_superseded, tmp_path, tmp_path
        )
        resolver = CanonicalAuthorityResolver(base_situation)
        with pytest.raises(AuthorityDenied):
            resolver.validate_task(plan["tasks"][0])

    def test_mutating_must_remain_false(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective, sample_catalog: IntentCatalog
    ):
        """Compiler strictly sets mutating=False and write_scope=[] for all supported compact intents."""
        for c in sample_catalog.candidates:
            intent = {
                "schema_version": "1.0.0",
                "objective_id": "OBJ-01",
                "intent_type": c.intent_type,
                "target_ref": c.target_ref,
            }
            if c.verification_ref:
                intent["verification_ref"] = c.verification_ref
            plan = compile_intent_to_plan(
                intent, sample_catalog, base_situation, base_objective, tmp_path, tmp_path
            )
            task = plan["tasks"][0]
            assert task["mutating"] is False
            assert task["write_scope"] == []
            assert task["risk_class"] == "R0"

    def test_duplicate_completed_action_rejected_in_catalog(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective
    ):
        """Completed actions in forbidden_signatures are excluded from catalog and deduplicated."""
        sig = "PROCESS:" + json.dumps({"cmd": ["npm", "run", "test:unit"]}, sort_keys=True, separators=(",", ":"))
        catalog = build_intent_catalog(
            situation=base_situation,
            objective=base_objective,
            workspace=tmp_path,
            runtime_dir=tmp_path,
            verification_capabilities={
                "verification_scripts": {"test:unit": "vitest run", "test:e2e": "playwright test"}
            },
            forbidden_signatures={sig},
        )
        assert all(c.target_ref != "test:unit" for c in catalog.candidates)
        assert any(c.target_ref == "test:e2e" for c in catalog.candidates)

    def test_source_sha_mismatch_rejected(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective, sample_catalog: IntentCatalog
    ):
        """Invalid or drifting control_sha raises SourceGenerationMismatchError."""
        bad_situation = dataclasses.replace(
            base_situation,
            control_sha="invalid-short-sha",
        )
        intent = {
            "schema_version": "1.0.0",
            "objective_id": "OBJ-01",
            "intent_type": "RUN_DECLARED_TEST",
            "target_ref": "test:unit",
        }
        with pytest.raises(SourceGenerationMismatchError):
            compile_intent_to_plan(
                intent, sample_catalog, bad_situation, base_objective, tmp_path, tmp_path
            )

    def test_strict_plan_schema_shape_and_envelope(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective, sample_catalog: IntentCatalog
    ):
        """Strict PLAN_SCHEMA validation envelope requires 13 required fields per task and standard envelope."""
        intent = {
            "schema_version": "1.0.0",
            "objective_id": "OBJ-01",
            "intent_type": "RUN_DECLARED_TEST",
            "target_ref": "test:unit",
        }
        plan = compile_intent_to_plan(
            intent, sample_catalog, base_situation, base_objective, tmp_path, tmp_path
        )
        normalized = _normalize_plan_schema_envelope(plan, base_objective, base_situation, tmp_path)
        assert normalized["schema_version"] == "1.0.0"
        assert normalized["objective_id"] == "OBJ-01"
        assert len(normalized["tasks"]) == 1
        t = normalized["tasks"][0]
        required_task_fields = {
            "node_id", "run_type", "authority_id", "risk_class", "mutating",
            "dependencies", "scope_tags", "write_scope", "payload", "expected_artifacts",
            "tests", "evidence_requirements", "completion_criteria",
        }
        assert required_task_fields.issubset(set(t.keys()))

    def test_route_a_paired_with_route_b_assertion_spec_rejected(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective
    ):
        """Negative regression: Route A paired with Route B's assertion specification is strictly rejected."""
        mismatched_catalog = IntentCatalog(
            objective_id="OBJ-01",
            candidates=(
                IntentCandidate(
                    intent_type="BROWSER_VERIFY",
                    target_ref="#/route/alpha",
                    verification_ref="spec-beta",
                    description="Verify route alpha",
                    payload_preview={"url": "http://127.0.0.1:5173/#/route/alpha"},
                ),
            ),
            browser_verification_specs={
                "spec-beta": {
                    "route": "#/route/beta",
                    "assertions": [
                        {"type": "selector_visible", "target": "#beta-card"},
                        {"type": "route_exact", "target": "#/route/beta"},
                    ],
                }
            },
            render_origin="http://127.0.0.1:5173",
        )
        intent = {
            "schema_version": "1.0.0",
            "objective_id": "OBJ-01",
            "intent_type": "BROWSER_VERIFY",
            "target_ref": "#/route/alpha",
            "verification_ref": "spec-beta",
        }
        with pytest.raises(MismatchedRouteVerificationSpecError):
            compile_intent_to_plan(
                intent, mismatched_catalog, base_situation, base_objective, tmp_path, tmp_path
            )

    def test_unapproved_local_verification_json_rejected(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective
    ):
        """Negative regression: Unapproved local verification JSON without approved: true or contract is rejected."""
        aos_dir = tmp_path / ".aos"
        aos_dir.mkdir(parents=True, exist_ok=True)
        unapproved_file = aos_dir / "browser-verification-specs.json"
        unapproved_file.write_text(
            json.dumps({
                "#/unapproved": {
                    "route": "#/unapproved",
                    "assertions": [{"type": "selector_visible", "target": "#evil"}],
                }
            }),
            encoding="utf-8",
        )
        catalog = build_intent_catalog(
            situation=base_situation,
            objective=base_objective,
            workspace=tmp_path,
            runtime_dir=tmp_path,
        )
        # Should not discover specs from unapproved file
        assert "#/unapproved" not in catalog.browser_verification_specs
        assert all(c.target_ref != "#/unapproved" for c in catalog.candidates)

    def test_stale_source_sha_rejected(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective
    ):
        """Negative regression: Stale but correctly formatted 40-char source SHA is rejected against catalog."""
        catalog_with_sha = IntentCatalog(
            objective_id="OBJ-01",
            candidates=(
                IntentCandidate(
                    intent_type="READ_OBSERVED_SOURCE",
                    target_ref="src/app.tsx",
                    description="Read source",
                    payload_preview={"action": "read_file", "path": "src/app.tsx"},
                ),
            ),
            control_sha="a" * 40,
        )
        # Situation has a valid 40-char SHA but differing from catalog
        stale_situation = dataclasses.replace(base_situation, control_sha="b" * 40)
        intent = {
            "schema_version": "1.0.0",
            "objective_id": "OBJ-01",
            "intent_type": "READ_OBSERVED_SOURCE",
            "target_ref": "src/app.tsx",
        }
        with pytest.raises(SourceGenerationMismatchError):
            compile_intent_to_plan(
                intent, catalog_with_sha, stale_situation, base_objective, tmp_path, tmp_path
            )

    def test_changed_workspace_generation_rejected(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective
    ):
        """Negative regression: Changed workspace source generation raises SourceGenerationMismatchError."""
        catalog_with_gen = IntentCatalog(
            objective_id="OBJ-01",
            candidates=(
                IntentCandidate(
                    intent_type="READ_OBSERVED_SOURCE",
                    target_ref="src/app.tsx",
                    description="Read source",
                    payload_preview={"action": "read_file", "path": "src/app.tsx"},
                ),
            ),
            control_sha="a" * 40,
            workspace_source_generation="gen-alpha-12345",
        )
        intent = {
            "schema_version": "1.0.0",
            "objective_id": "OBJ-01",
            "intent_type": "READ_OBSERVED_SOURCE",
            "target_ref": "src/app.tsx",
        }
        with pytest.raises(SourceGenerationMismatchError):
            compile_intent_to_plan(
                intent,
                catalog_with_gen,
                base_situation,
                base_objective,
                tmp_path,
                tmp_path,
                workspace_source_generation="gen-beta-67890",
            )

    def test_potentially_mutating_package_script_rejected(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective
    ):
        """Negative regression: Potentially mutating scripts (build, lint:fix, sed, rm) are excluded from catalog."""
        scripts = {
            "test:unit": "vitest run",
            "test:e2e": "playwright test",
            "build": "vite build",
            "lint:fix": "eslint --fix .",
            "clean": "rm -rf dist",
            "mutate:node": "node scripts/mutate.js",
            "format": "prettier --write .",
        }
        catalog = build_intent_catalog(
            situation=base_situation,
            objective=base_objective,
            workspace=tmp_path,
            runtime_dir=tmp_path,
            verification_capabilities={"verification_scripts": scripts},
        )
        admitted = {c.target_ref for c in catalog.candidates if c.intent_type == "RUN_DECLARED_TEST"}
        assert "test:unit" in admitted
        assert "test:e2e" in admitted
        assert "build" not in admitted
        assert "lint:fix" not in admitted
        assert "clean" not in admitted
        assert "mutate:node" not in admitted
        assert "format" not in admitted

    def test_implementation_objective_classification_gate(
        self, base_situation: ProjectSituation
    ):
        """Negative regression: Implementation objectives are strictly classified and not routed to verification."""
        impl_obj1 = Objective(
            objective_id="OBJ-IMPL-01",
            title="Implement responsive navbar component",
            description="Create navbar with brand logo and mobile menu",
            authority_id="AUTH-VALID-01",
            risk_class="R0",
            rationale="Feature requirement",
            scope_tags=("ui", "frontend"),
            completion_criteria=("Navbar rendered",),
            parallel_candidates=(),
            kind="IMPLEMENTATION",
        )
        assert is_implementation_objective(impl_obj1, base_situation) is True

        impl_obj2 = Objective(
            objective_id="OBJ-IMPL-02",
            title="Wire backend appointment booking endpoint",
            description="Add database query handler",
            authority_id="AUTH-VALID-01",
            risk_class="R0",
            rationale="Backend feature",
            scope_tags=("backend", "feature"),
            completion_criteria=("Endpoint returns 200",),
            parallel_candidates=(),
        )
        assert is_implementation_objective(impl_obj2, base_situation) is True

        verif_obj = Objective(
            objective_id="OBJ-VERIF-01",
            title="Verify appointment management UI route",
            description="Check that root container renders and route matches",
            authority_id="AUTH-VALID-01",
            risk_class="R0",
            rationale="QA check",
            scope_tags=("ui", "verification"),
            completion_criteria=("Route verified",),
            parallel_candidates=(),
            kind="VERIFICATION",
        )
        assert is_implementation_objective(verif_obj, base_situation) is False

    def test_completed_action_under_different_task_id_deduplicated(
        self, tmp_path: Path, base_situation: ProjectSituation, base_objective: Objective
    ):
        """Negative regression: Action previously completed under task-id TASK-OLD is excluded by semantic signature."""
        # Signature of running npm run test:unit
        sig = "PROCESS:" + json.dumps({"cmd": ["npm", "run", "test:unit"]}, sort_keys=True, separators=(",", ":"))
        catalog = build_intent_catalog(
            situation=base_situation,
            objective=base_objective,
            workspace=tmp_path,
            runtime_dir=tmp_path,
            verification_capabilities={"verification_scripts": {"test:unit": "vitest run"}},
            # Completed under a different task ID, but signature is tracked in forbidden_signatures
            forbidden_ids={"TASK-HISTORICAL-BATCH-999"},
            forbidden_signatures={sig},
        )
        assert all(c.target_ref != "test:unit" for c in catalog.candidates)

