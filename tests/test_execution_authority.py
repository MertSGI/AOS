"""Tests for AOS-3 Execution-Base Authority contract and validator."""

import json
from pathlib import Path
from aos.execution_authority import validate_execution_authority
from aos.source_adapter import ProjectSourceAdapter
from aos.validate import validate_document, validate_file

DESCRIPTOR_PATH = Path(__file__).parent.parent / "descriptors" / "lari.descriptor.json"


def make_valid_snapshot(
    project_id: str = "lari",
    exec_base_sha: str = "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
    current_milestone: str = "AOS-3"
):
    return {
        "schema_version": "0.1.0",
        "project_id": project_id,
        "repository": "MertSGI/Randapp-main",
        "source_ref": "control/lari-project-control-plane",
        "source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
        "current_status": "CORE_SOFTWARE_RC_CLOSED_PROVEN",
        "current_milestone": current_milestone,
        "canonical_next_action": "Execute Block 3 Workspace / UI implementation",
        "target_base_sha": "65a53427f52c21e60aa8f92e02a17d693a201601",
        "next_action_execution_base_sha": exec_base_sha,
        "has_ambiguity": False,
        "ambiguity_reasons": [],
        "input_file_hashes": {"state": "0000000000000000000000000000000000000000000000000000000000000000"}
    }


def make_valid_task(
    project_id: str = "lari",
    base_sha: str = "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
    gate: str = "AOS-3",
    risk_class: str = "R1"
):
    return {
        "schema_version": "0.1.0",
        "project_id": project_id,
        "task_id": "AOS-TASK-301",
        "gate": gate,
        "title": "Block 3 Workspace UI",
        "description": "Controlled execution of Block 3 Workspace UI",
        "risk_class": risk_class,
        "base_sha": base_sha,
        "branch_name": "feature/lari-clinic-foundation",
        "allowed_scope": {
            "paths": ["src/"]
        },
        "worker_requirements": {
            "adapter": "antigravity",
            "environment": "non_production",
            "isolated_worktree": True
        },
        "evidence_requirements": {
            "minimum_level": "E3_ISOLATED_RUNTIME_PROVEN"
        },
        "retry_policy": {
            "max_retries": 1
        }
    }


class TestExecutionBaseDescriptorAndSnapshotSchema:
    def test_old_aos2_descriptor_fixtures_remain_valid(self):
        """Existing descriptor schema accepts descriptors without execution-base pointer."""
        res, code = validate_file("project_descriptor", str(DESCRIPTOR_PATH))
        assert code == 0
        assert res.is_valid is True

    def test_descriptor_may_define_execution_base_pointer(self):
        """Descriptor schema accepts optional next_action_execution_base_sha_pointer and required flag."""
        with open(DESCRIPTOR_PATH, "r", encoding="utf-8") as f:
            desc = json.load(f)

        desc["projection"]["next_action_execution_base_sha_pointer"] = "/next_action_execution_base_sha"
        desc["projection"]["next_action_execution_base_sha_required"] = True

        res = validate_document("project_descriptor", desc)
        assert res.is_valid is True

    def test_snapshot_schema_accepts_execution_base_sha(self):
        """Canonical project snapshot schema accepts next_action_execution_base_sha string."""
        snapshot = make_valid_snapshot()
        res = validate_document("canonical_project_snapshot", snapshot)
        assert res.is_valid is True


class TestSourceAdapterExecutionBaseResolution:
    def test_missing_execution_base_with_required_false_remains_valid_null(self):
        """Missing execution base pointer when required=False yields null without ambiguity."""
        adapter = ProjectSourceAdapter("repo", "ref")
        state_content = json.dumps({"current_milestone": "M1", "next_action": "Act"})
        snap = adapter.build_normalized_snapshot(
            "p1", "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            {"state": state_content}, {},
            projection_config={
                "current_status_pointer": "/current_status",
                "current_milestone_pointer": "/current_milestone",
                "canonical_next_action_pointer": "/next_action",
                "next_action_execution_base_sha_pointer": "/missing_sha",
                "next_action_execution_base_sha_required": False
            }
        )
        assert snap["next_action_execution_base_sha"] is None
        assert snap["has_ambiguity"] is False

    def test_missing_execution_base_with_required_true_becomes_ambiguous(self):
        """Missing execution base pointer when required=True marks snapshot ambiguous (fail-closed)."""
        adapter = ProjectSourceAdapter("repo", "ref")
        state_content = json.dumps({"current_milestone": "M1", "next_action": "Act"})
        snap = adapter.build_normalized_snapshot(
            "p1", "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            {"state": state_content}, {},
            projection_config={
                "current_status_pointer": "/current_status",
                "current_milestone_pointer": "/current_milestone",
                "canonical_next_action_pointer": "/next_action",
                "next_action_execution_base_sha_pointer": "/missing_sha",
                "next_action_execution_base_sha_required": True
            }
        )
        assert snap["next_action_execution_base_sha"] is None
        assert snap["has_ambiguity"] is True
        assert any("Missing required execution base SHA" in r for r in snap["ambiguity_reasons"])

    def test_malformed_execution_base_is_rejected(self):
        """Malformed execution base string (non-40 hex) marks snapshot ambiguous."""
        adapter = ProjectSourceAdapter("repo", "ref")
        state_content = json.dumps({"current_milestone": "M1", "next_action": "Act", "exec_sha": "not-a-valid-sha"})
        snap = adapter.build_normalized_snapshot(
            "p1", "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            {"state": state_content}, {},
            projection_config={
                "current_status_pointer": "/current_status",
                "current_milestone_pointer": "/current_milestone",
                "canonical_next_action_pointer": "/next_action",
                "next_action_execution_base_sha_pointer": "/exec_sha",
                "next_action_execution_base_sha_required": True
            }
        )
        assert snap["next_action_execution_base_sha"] is None
        assert snap["has_ambiguity"] is True
        assert any("Malformed execution base SHA" in r for r in snap["ambiguity_reasons"])

    def test_source_adapter_resolves_execution_base_declaratively(self):
        """Source adapter extracts execution base via JSON pointer."""
        adapter = ProjectSourceAdapter("repo", "ref")
        state_content = json.dumps({
            "current_milestone": "M1",
            "next_action": "Act",
            "execution_base_sha": "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637"
        })
        snap = adapter.build_normalized_snapshot(
            "p1", "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            {"state": state_content}, {},
            projection_config={
                "current_status_pointer": "/current_status",
                "current_milestone_pointer": "/current_milestone",
                "canonical_next_action_pointer": "/next_action",
                "next_action_execution_base_sha_pointer": "/execution_base_sha",
                "next_action_execution_base_sha_required": True
            }
        )
        assert snap["next_action_execution_base_sha"] == "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637"
        assert snap["has_ambiguity"] is False

    def test_target_base_sha_never_silently_substituted_for_execution_base(self):
        """target_base_sha is never used as execution base if execution base pointer is missing."""
        adapter = ProjectSourceAdapter("repo", "ref")
        state_content = json.dumps({
            "current_milestone": "M1",
            "next_action": "Act",
            "target_sha": "65a53427f52c21e60aa8f92e02a17d693a201601"
        })
        snap = adapter.build_normalized_snapshot(
            "p1", "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            {"state": state_content}, {},
            projection_config={
                "current_status_pointer": "/current_status",
                "current_milestone_pointer": "/current_milestone",
                "canonical_next_action_pointer": "/next_action",
                "target_base_sha_pointer": "/target_sha"
            }
        )
        assert snap["target_base_sha"] == "65a53427f52c21e60aa8f92e02a17d693a201601"
        assert snap["next_action_execution_base_sha"] is None

    def test_next_action_prose_sha_never_parsed_as_authority(self):
        """SHA in next_action prose text is never automatically extracted as execution base."""
        adapter = ProjectSourceAdapter("repo", "ref")
        state_content = json.dumps({
            "current_milestone": "M1",
            "next_action": "Do something from 5e935ed049ffe08a6797643ec9cc2b7d4e6ae637"
        })
        snap = adapter.build_normalized_snapshot(
            "p1", "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            {"state": state_content}, {}
        )
        assert snap["next_action_execution_base_sha"] is None

    def test_no_hardcoded_lari_clinic_path_in_core(self):
        """Core source adapter codebase contains no LARI clinic hardcoded paths."""
        source_code = (Path(__file__).parent.parent / "src" / "aos" / "source_adapter.py").read_text(encoding="utf-8")
        assert "clinic_block2_operational_integration" not in source_code
        assert "clinic_block1_authority" not in source_code


class TestExecutionAuthorityValidator:
    def test_task_gate_matches_snapshot_milestone_aos3_accept(self):
        """A. Task gate 'AOS-3' + snapshot milestone 'AOS-3' returns ACCEPT."""
        snapshot = make_valid_snapshot(current_milestone="AOS-3")
        task = make_valid_task(gate="AOS-3")
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is True
        assert res.disposition == "ACCEPT"
        assert res.execution_base_sha == "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637"

    def test_task_gate_matches_snapshot_milestone_aos4_accept(self):
        """B. Task gate 'AOS-4' + snapshot milestone 'AOS-4' returns ACCEPT."""
        snapshot = make_valid_snapshot(current_milestone="AOS-4")
        task = make_valid_task(gate="AOS-4")
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is True
        assert res.disposition == "ACCEPT"
        assert res.execution_base_sha == "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637"

    def test_arbitrary_project_milestone_m1_accept(self):
        """C. Arbitrary project milestone 'M1' + task gate 'M1' returns ACCEPT."""
        snapshot = make_valid_snapshot(current_milestone="M1")
        task = make_valid_task(gate="M1")
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is True
        assert res.disposition == "ACCEPT"

    def test_snapshot_milestone_m1_task_gate_m2_holds(self):
        """D. Snapshot milestone 'M1' + task gate 'M2' returns HOLD."""
        snapshot = make_valid_snapshot(current_milestone="M1")
        task = make_valid_task(gate="M2")
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is False
        assert res.disposition == "HOLD"
        assert any("Task gate 'M2' does not match canonical snapshot milestone 'M1'" in e for e in res.errors)

    def test_aos4_task_against_aos3_snapshot_holds(self):
        """E. AOS-4 task against AOS-3 snapshot returns HOLD."""
        snapshot = make_valid_snapshot(current_milestone="AOS-3")
        task = make_valid_task(gate="AOS-4")
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is False
        assert res.disposition == "HOLD"
        assert any("Task gate 'AOS-4' does not match canonical snapshot milestone 'AOS-3'" in e for e in res.errors)

    def test_malformed_missing_milestone_fails_closed(self):
        """F. Malformed or missing milestone in snapshot fails closed."""
        snapshot = make_valid_snapshot()
        del snapshot["current_milestone"]
        task = make_valid_task(gate="AOS-3")
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is False
        assert res.disposition == "HOLD"
        assert any("Canonical project snapshot schema validation failed" in e for e in res.errors)

    def test_missing_gate_holds_via_schema_validation(self):
        """Task with missing gate fails canonical task schema validation and returns HOLD."""
        snapshot = make_valid_snapshot()
        task = make_valid_task()
        del task["gate"]
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is False
        assert res.disposition == "HOLD"
        assert any("Canonical task schema validation failed" in e for e in res.errors)

    def test_structurally_incomplete_task_holds(self):
        """Structurally incomplete task fails schema validation and returns HOLD."""
        snapshot = make_valid_snapshot()
        task = {"project_id": "lari", "risk_class": "R1"}
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is False
        assert res.disposition == "HOLD"
        assert any("Canonical task schema validation failed" in e for e in res.errors)

    def test_structurally_incomplete_snapshot_holds(self):
        """Structurally incomplete snapshot fails schema validation and returns HOLD."""
        snapshot = {"project_id": "lari", "next_action_execution_base_sha": "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637"}
        task = make_valid_task()
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is False
        assert res.disposition == "HOLD"
        assert any("Canonical project snapshot schema validation failed" in e for e in res.errors)

    def test_malformed_task_base_sha_holds(self):
        """Malformed task base SHA fails task schema validation and returns HOLD."""
        snapshot = make_valid_snapshot()
        task = make_valid_task(base_sha="not-a-valid-sha")
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is False
        assert res.disposition == "HOLD"
        assert any("Canonical task schema validation failed" in e for e in res.errors)

    def test_malformed_snapshot_execution_base_holds(self):
        """Malformed snapshot execution base SHA fails snapshot schema validation and returns HOLD."""
        snapshot = make_valid_snapshot(exec_base_sha="invalid-sha")
        task = make_valid_task()
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is False
        assert res.disposition == "HOLD"
        assert any("Canonical project snapshot schema validation failed" in e for e in res.errors)

    def test_project_mismatch_holds(self):
        """Task project_id != snapshot project_id returns HOLD."""
        snapshot = make_valid_snapshot(project_id="lari")
        task = make_valid_task(project_id="aos")
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is False
        assert res.disposition == "HOLD"
        assert any("Task project_id 'aos' != snapshot project_id 'lari'" in e for e in res.errors)

    def test_base_mismatch_holds(self):
        """Task base_sha != snapshot execution base SHA returns HOLD."""
        snapshot = make_valid_snapshot(exec_base_sha="5e935ed049ffe08a6797643ec9cc2b7d4e6ae637")
        task = make_valid_task(base_sha="65a53427f52c21e60aa8f92e02a17d693a201601")
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is False
        assert res.disposition == "HOLD"
        assert any("Task base_sha" in e for e in res.errors)

    def test_snapshot_ambiguity_holds(self):
        """Snapshot with has_ambiguity=True returns HOLD."""
        snapshot = make_valid_snapshot()
        snapshot["has_ambiguity"] = True
        snapshot["ambiguity_reasons"] = ["Control file mismatch"]
        task = make_valid_task()
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is False
        assert res.disposition == "HOLD"
        assert any("Canonical snapshot has ambiguity" in e for e in res.errors)
