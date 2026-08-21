"""Tests for AOS-3 Execution-Base Authority contract and validator."""

import json
from pathlib import Path
from aos.execution_authority import validate_execution_authority
from aos.source_adapter import ProjectSourceAdapter
from aos.validate import validate_document, validate_file

DESCRIPTOR_PATH = Path(__file__).parent.parent / "descriptors" / "lari.descriptor.json"
EXPECTATION_PATH = Path(__file__).parent.parent / "descriptors" / "lari.shadow-expectation.json"


class TestExecutionBaseDescriptorAndSnapshotSchema:
    def test_old_aos2_descriptor_fixtures_remain_valid(self):
        """1. Existing descriptor schema accepts descriptors without execution-base pointer."""
        res, code = validate_file("project_descriptor", str(DESCRIPTOR_PATH))
        assert code == 0
        assert res.is_valid is True

    def test_descriptor_may_define_execution_base_pointer(self):
        """2. Descriptor schema accepts optional next_action_execution_base_sha_pointer and required flag."""
        with open(DESCRIPTOR_PATH, "r", encoding="utf-8") as f:
            desc = json.load(f)

        desc["projection"]["next_action_execution_base_sha_pointer"] = "/next_action_execution_base_sha"
        desc["projection"]["next_action_execution_base_sha_required"] = True

        res = validate_document("project_descriptor", desc)
        assert res.is_valid is True

    def test_snapshot_schema_accepts_execution_base_sha(self):
        """3. Canonical project snapshot schema accepts next_action_execution_base_sha string."""
        snapshot = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "repository": "MertSGI/Randapp-main",
            "source_ref": "control/lari-project-control-plane",
            "source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            "current_status": "CORE_SOFTWARE_RC_CLOSED_PROVEN",
            "current_milestone": "LARİ Clinic",
            "canonical_next_action": "Action",
            "target_base_sha": "65a53427f52c21e60aa8f92e02a17d693a201601",
            "next_action_execution_base_sha": "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
            "has_ambiguity": False,
            "ambiguity_reasons": [],
            "input_file_hashes": {"state": "0000000000000000000000000000000000000000000000000000000000000000"}
        }
        res = validate_document("canonical_project_snapshot", snapshot)
        assert res.is_valid is True


class TestSourceAdapterExecutionBaseResolution:
    def test_missing_execution_base_with_required_false_remains_valid_null(self):
        """4. Missing execution base pointer when required=False yields null without ambiguity."""
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
        """5. Missing execution base pointer when required=True marks snapshot ambiguous (fail-closed)."""
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
        """6. Malformed execution base string (non-40 hex) marks snapshot ambiguous."""
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
        """7. Source adapter extracts execution base via JSON pointer."""
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
        """8. target_base_sha is never used as execution base if execution base pointer is missing."""
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
        """9. SHA in next_action prose text is never automatically extracted as execution base."""
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
        """10. Core source adapter codebase contains no LARI clinic hardcoded paths."""
        source_code = (Path(__file__).parent.parent / "src" / "aos" / "source_adapter.py").read_text(encoding="utf-8")
        assert "clinic_block2_operational_integration" not in source_code
        assert "clinic_block1_authority" not in source_code


class TestExecutionAuthorityValidator:
    def test_validator_accepts_matching_task_base_sha(self):
        """11. Validator returns ACCEPT when task matches snapshot execution base."""
        snapshot = {
            "project_id": "lari",
            "next_action_execution_base_sha": "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
            "has_ambiguity": False,
            "ambiguity_reasons": []
        }
        task = {
            "project_id": "lari",
            "base_sha": "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
            "risk_class": "R1",
            "gate": "AOS-3"
        }
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is True
        assert res.disposition == "ACCEPT"
        assert res.execution_base_sha == "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637"

    def test_validator_holds_on_task_base_mismatch(self):
        """12. Validator returns HOLD when task base_sha does not match snapshot execution base."""
        snapshot = {
            "project_id": "lari",
            "next_action_execution_base_sha": "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
            "has_ambiguity": False,
            "ambiguity_reasons": []
        }
        task = {
            "project_id": "lari",
            "base_sha": "65a53427f52c21e60aa8f92e02a17d693a201601",  # Wrong base!
            "risk_class": "R1",
            "gate": "AOS-3"
        }
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is False
        assert res.disposition == "HOLD"
        assert any("Task base_sha" in e for e in res.errors)

    def test_validator_holds_when_execution_authority_missing(self):
        """13. Validator returns HOLD when snapshot missing next_action_execution_base_sha."""
        snapshot = {
            "project_id": "lari",
            "next_action_execution_base_sha": None,
            "has_ambiguity": False,
            "ambiguity_reasons": []
        }
        task = {
            "project_id": "lari",
            "base_sha": "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
            "risk_class": "R1",
            "gate": "AOS-3"
        }
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is False
        assert res.disposition == "HOLD"
        assert any("missing next_action_execution_base_sha" in e for e in res.errors)

    def test_validator_holds_on_project_mismatch(self):
        """14. Validator returns HOLD when task project_id != snapshot project_id."""
        snapshot = {
            "project_id": "lari",
            "next_action_execution_base_sha": "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
            "has_ambiguity": False,
            "ambiguity_reasons": []
        }
        task = {
            "project_id": "other_project",
            "base_sha": "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
            "risk_class": "R1",
            "gate": "AOS-3"
        }
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is False
        assert res.disposition == "HOLD"
        assert any("Task project_id" in e for e in res.errors)

    def test_validator_holds_on_incompatible_risk_class(self):
        """15. Validator returns HOLD for high risk classes like R2/R3."""
        snapshot = {
            "project_id": "lari",
            "next_action_execution_base_sha": "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
            "has_ambiguity": False,
            "ambiguity_reasons": []
        }
        task = {
            "project_id": "lari",
            "base_sha": "5e935ed049ffe08a6797643ec9cc2b7d4e6ae637",
            "risk_class": "R3",  # High risk
            "gate": "AOS-3"
        }
        res = validate_execution_authority(snapshot, task)
        assert res.is_valid is False
        assert res.disposition == "HOLD"
        assert any("risk_class" in e for e in res.errors)
