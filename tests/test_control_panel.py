import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import aos.control_panel as control_panel
from aos.control_panel import _HTML, _get_sanitized_providers, build_status, configure_provider, submit_job
from aos.platform_recovery import SourceRepairResult
from aos.self_diagnosis import SelfDiagnosisEngine, ShadowRepairProposal
from aos.self_repair import AUTHORITY_AUTO_REPAIR_ELIGIBLE


def _config(tmp_path: Path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return {
        "schema_version": "1.0.0",
        "authorized_roots": [str(tmp_path)],
        "runtime_root": str(runtime),
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }


def _job(tmp_path: Path):
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {
        "schema_version": "1.0.0",
        "job_id": "panel-job-1",
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "descriptor_path": str(descriptor),
        "workspace": str(workspace),
        "routing_policy_path": str(policy),
        "run_plan": {
            "schema_version": "1.0.0",
            "project_id": "test",
            "bound_source_sha": "a" * 40,
            "tasks": [{"node_id": "x", "run_type": "TEST", "authority_id": "AUTH"}],
        },
    }


def test_submit_job_queues_valid_envelope(tmp_path):
    cfg = _config(tmp_path)
    result = submit_job(_job(tmp_path), cfg)
    assert result["accepted"] is True
    assert result["production"] == "NO_GO"
    assert result["ag_backend_enabled"] is False
    assert (Path(cfg["runtime_root"]) / "inbox" / "panel-job-1.aosjob.json").is_file()


def test_control_panel_normal_construction_wires_source_repair_executor(
    tmp_path, monkeypatch
):
    runtime_root = tmp_path / "runtime-v1" / "state"
    runtime_root.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    host_config = tmp_path / "control-panel-host-config.json"
    host_config.write_text(json.dumps({
        "schema_version": "1.0.0",
        "authorized_roots": [str(tmp_path)],
        "runtime_root": str(runtime_root),
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "default_project": "aos",
        "projects": {
            "aos": {
                "workspace": str(workspace),
                "routing_policy_path": str(policy),
            }
        },
    }), encoding="utf-8")
    panel_config = tmp_path / "control-panel-config.json"
    runtime_home = tmp_path / "runtime-v1"
    sentinel = object()
    captured = {}

    monkeypatch.setattr(
        control_panel,
        "create_source_repair_executor",
        lambda **kwargs: captured.update(kwargs) or sentinel,
    )
    monkeypatch.setattr(control_panel, "default_runtime_home", lambda: runtime_home)
    monkeypatch.setattr(
        control_panel,
        "serve",
        lambda host, panel, *, source_repair_executor: (
            captured.update({
                "served_host": host,
                "served_panel": panel,
                "served_executor": source_repair_executor,
            })
            or 0
        ),
    )

    result = control_panel.main([
        "--host-config", str(host_config),
        "--panel-config", str(panel_config),
    ])

    assert result == 0
    assert captured["repository"] == workspace.resolve()
    assert captured["runtime_dir"] == runtime_root.resolve()
    assert captured["runtime_home"] == runtime_home
    assert captured["served_executor"] is sentinel


def test_submit_job_rejects_duplicate(tmp_path):
    cfg = _config(tmp_path)
    job = _job(tmp_path)
    submit_job(job, cfg)
    with pytest.raises(ValueError, match="already exists"):
        submit_job(job, cfg)


def test_submit_job_rejects_secret_bearing_key(tmp_path):
    cfg = _config(tmp_path)
    job = _job(tmp_path)
    job["api_key"] = "never-store"
    with pytest.raises(ValueError, match="secret"):
        submit_job(job, cfg)


def test_platform_recovery_route_passes_configured_source_executor(tmp_path):
    source_sha = "a" * 40
    relay_root = tmp_path / "relay"
    config = _config(tmp_path)
    config.update({
        "controller_relay_dir": str(relay_root),
        "candidate_source_sha": source_sha,
    })
    diagnosis = SelfDiagnosisEngine(relay_root / "self-diagnosis", config)
    finding = diagnosis.record_or_update_finding(
        component="telemetry",
        failure_class="TELEMETRY_DEFECT",
        symptom="Source repair required",
        severity="MEDIUM",
        autonomy_impact="DEGRADED",
        affected_lane_ids=[],
        evidence_refs=["source-proof"],
        evidence_class="SOURCE_PROOF",
        confidence=0.99,
        suspected_root_cause="Bounded source defect",
        repair_authority=AUTHORITY_AUTO_REPAIR_ELIGIBLE,
        requires_candidate=True,
        proposed_repair=ShadowRepairProposal(
            problem="Bounded source defect",
            evidence=["source-proof"],
            root_cause_hypothesis="Wiring defect",
            minimal_change="Patch one source file",
            files_likely_affected=["src/aos/control_panel.py"],
            tests_required=["tests/test_control_panel.py"],
            ci_required=True,
            runtime_proof_required="EXACT_SHA_CI_SUCCESS",
            rollback_plan="Retain base SHA",
            authority_class=AUTHORITY_AUTO_REPAIR_ELIGIBLE,
        ),
    )

    def executor(request):
        repair_sha = "b" * 40
        return SourceRepairResult(
            isolated_worktree=str(tmp_path / "isolated"),
            branch="repair/aos-system-test",
            base_sha=request["required_base_sha"],
            repair_sha=repair_sha,
            workspace_fingerprint="workspace-fingerprint",
            resource_backend_id="codex_cli",
            attempt_telemetry={"attempt_count": 1},
            git_diff_sha256="d" * 64,
            candidate_manifest={"source_sha": repair_sha, "immutable": True},
            rollback_information={"base_sha": source_sha},
            tests_passed=True,
            evidence_valid=True,
            exact_sha_ci_status="SUCCESS",
            candidate_materialized=True,
        )

    server = ThreadingHTTPServer(("127.0.0.1", 0), control_panel._Handler)
    server.aos_config = config  # type: ignore[attr-defined]
    server.aos_token = "x" * 40  # type: ignore[attr-defined]
    server.aos_source_repair_executor = executor  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps({"finding_id": finding.finding_id}).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/platform-recovery",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AOS-Panel-Token": "x" * 40,
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
        assert result["disposition"] == "PROMOTION_READY"
        assert result["source_repair"]["base_sha"] == source_sha
        assert result["promotion_permitted"] is False
        assert result["activation_permitted"] is False
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_build_status_is_fail_closed_and_ag_disabled(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    status = build_status(cfg)
    assert status["production"] == "NO_GO"
    assert status["ag_backend_enabled"] is False
    assert status["host_state"] == "UNKNOWN"


def test_configure_provider_never_returns_secret(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        control_panel,
        "write_provider_secret",
        lambda provider, secret: captured.update(provider=provider, secret=secret),
    )
    result = configure_provider({
        "provider": "GEMINI",
        "action": "save",
        "secret": "super-secret-value",
    })
    assert captured == {"provider": "GEMINI", "secret": "super-secret-value"}
    assert result["ready"] is True
    assert result["secret_returned"] is False
    assert "super-secret-value" not in repr(result)


def test_configure_provider_delete(monkeypatch):
    monkeypatch.setattr(control_panel, "delete_provider_secret", lambda provider: True)
    result = configure_provider({"provider": "GROQ", "action": "delete"})
    assert result["provider"] == "GROQ"
    assert result["deleted"] is True
    assert result["secret_returned"] is False


def test_build_status_includes_deliberation_and_lane_telemetry(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    # Unconfigured case contains zeroed deliberation structure
    status = build_status(cfg)
    assert "deliberation" in status
    assert status["deliberation"]["total_samples"] == 0
    assert status["deliberation"]["trigger_reasons"] == {}

    # Configured runtime case with deliberation ledger in store
    state_dir = tmp_path / "state"
    commands_dir = state_dir / "commands"
    cmd_dir = commands_dir / "cmd-123"
    delib_dir = cmd_dir / "project-runtime" / "deliberation"
    delib_dir.mkdir(parents=True)
    ledger_file = delib_dir / "deliberation-shadow-ledger.jsonl"
    entry = {
        "decision_id": "dec-1",
        "council_trigger_reason": "MATERIAL_AMBIGUITY_OR_HIGH_IMPACT",
        "quorum_obtained": True,
        "council_agreement": True,
    }
    ledger_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    # Mock runtime_configured and runtime_status
    monkeypatch.setattr(control_panel, "runtime_configured", lambda c: True)
    monkeypatch.setattr(
        control_panel,
        "runtime_status",
        lambda c: {
            "host_state": "RUNNING",
            "runtime_v1": {
                "runtime_state": "HEALTHY",
                "active_commands": ["cmd-123"],
                "waiting_commands": [],
            },
        },
    )
    # Set LOCALAPPDATA to point to tmp_path so it scans state_dir
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path.parent))
    fake_aos_state = tmp_path.parent / "AOS" / "runtime-v1" / "state"
    fake_aos_state.mkdir(parents=True, exist_ok=True)
    (fake_aos_state / "commands").mkdir(exist_ok=True)

    # Point cfg runtime_root to tmp_path
    cfg["runtime_root"] = str(tmp_path)
    status2 = build_status(cfg)
    assert status2["deliberation"]["total_samples"] == 1
    assert status2["deliberation"]["quorum_count"] == 1
    assert status2["deliberation"]["agreement_count"] == 1
    assert status2["deliberation"]["trigger_reasons"]["MATERIAL_AMBIGUITY_OR_HIGH_IMPACT"] == 1


def test_build_status_projects_newest_durable_lineage_not_lexicographic_id(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    commands_root = state_root / "commands"

    def write_command(command_id, project_id, *, updated_at, completed, state, failure_class=None):
        command_root = commands_root / command_id
        command_root.mkdir(parents=True)
        (command_root / "command.json").write_text(json.dumps({
            "command_id": command_id,
            "created_at": updated_at,
            "goal": f"Continue {project_id}",
            "project": {"project_id": project_id},
        }), encoding="utf-8")
        (command_root / "state.json").write_text(json.dumps({
            "command_id": command_id,
            "state": state,
            "disposition": state,
            "failure_class": failure_class,
            "completed_batch_count": completed,
            "attempts": 1,
            "updated_at": updated_at,
        }), encoding="utf-8")

    write_command(
        "continue-zzz-stale-lari",
        "lari",
        updated_at="2026-09-16T08:02:28+00:00",
        completed=1,
        state="FAILED",
        failure_class="RUNTIME_EXECUTION_FAILURE",
    )
    write_command(
        "continue-b181ddc574c25c2aa0f2a6b9",
        "lari",
        updated_at="2026-09-24T16:36:41+00:00",
        completed=436,
        state="HUMAN_REQUIRED",
        failure_class="RECOVERY_CHURN_GUARD",
    )
    write_command(
        "continue-zzz-stale-ui",
        "lari-ui-v2",
        updated_at="2026-09-17T12:09:45+00:00",
        completed=0,
        state="HUMAN_REQUIRED",
    )
    write_command(
        "continue-61be4ab1af53cfa646d773ce",
        "lari-ui-v2",
        updated_at="2026-09-24T16:23:41+00:00",
        completed=111,
        state="HUMAN_REQUIRED",
        failure_class="RECOVERY_CHURN_GUARD",
    )

    monkeypatch.setattr(control_panel, "runtime_configured", lambda _config: True)
    monkeypatch.setattr(
        control_panel,
        "runtime_status",
        lambda _config: {
            "host_state": "HEALTHY",
            "runtime_v1": {
                "runtime_state": "HEALTHY",
                "active_commands": [],
                "waiting_commands": [],
                "latest_command": {
                    "command_id": "continue-b181ddc574c25c2aa0f2a6b9",
                },
            },
        },
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "unrelated"))

    status = build_status({"runtime_root": str(state_root)})

    assert status["lanes"]["lari"]["command_id"] == "continue-b181ddc574c25c2aa0f2a6b9"
    assert status["lanes"]["lari"]["completed_batches"] == 436
    assert status["lanes"]["lari"]["current_blocker"] == "RECOVERY_CHURN_GUARD"
    assert status["lanes"]["lari-ui-v2"]["command_id"] == "continue-61be4ab1af53cfa646d773ce"
    assert status["lanes"]["lari-ui-v2"]["completed_batches"] == 111


def test_provider_telemetry_uses_credential_identity_and_paid_remains_disabled(monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    policy_path = repo_root / "descriptors" / "nemotron.planner-policy.json"
    expected_identities = {
        "nemotron": "NVIDIA",
        "gemini": "GEMINI",
        "groq": "GROQ",
        "cloudflare": "CLOUDFLARE",
        "openrouter_free": "OPENROUTER",
        "cerebras": "CEREBRAS",
        "huggingface_router": "HUGGINGFACE",
        "openai_paid_safety": "OPENAI",
    }
    for env_var in (
        "NVIDIA_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "CLOUDFLARE_API_TOKEN",
        "OPENROUTER_API_KEY", "CEREBRAS_API_KEY", "HF_TOKEN", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(env_var, raising=False)
    rows = _get_sanitized_providers(
        {"default_project": {"routing_policy_path": str(policy_path)}},
        {identity: True for identity in expected_identities.values()},
    )
    by_id = {row["provider_id"]: row for row in rows}
    for provider_id in expected_identities:
        assert by_id[provider_id]["configured"] is True
    assert by_id["openai_paid_safety"]["enabled"] is False


def test_goal_composer_uses_project_selector_readonly_profile_and_checks_http_status():
    assert 'id="goal-project"' in _HTML
    assert 'id="goal-descriptor" type="text" readonly' in _HTML
    assert 'id="goal-workspace" type="text" readonly' in _HTML
    assert 'id="goal-policy" type="text" readonly' in _HTML
    assert "project_id: projectEl ? projectEl.value : ''" in _HTML
    assert "if (!r.ok)" in _HTML
    assert _HTML.index("if (!r.ok)") < _HTML.index("toast('Autonomous Goal Accepted')")


def test_dashboard_defines_tracked_lane_count_before_using_it():
    definition = "const trackedLanesCount = laneKeys.length;"
    usage = "if (trackedLanesCount === 0)"

    assert definition in _HTML
    assert usage in _HTML
    assert _HTML.index(definition) < _HTML.index(usage)

    assert "const activeStates = new Set([" in _HTML
    assert "'WAITING_FOR_REASONING_PROVIDER'" in _HTML
    assert "'WAITING_FOR_SOURCE_TRANSPORT'" in _HTML


def test_mobile_lane_summary_wraps_long_blockers():
    assert 'class="overview-lane-detail"' in _HTML
    assert ".overview-lane-detail { grid-template-columns: minmax(0, 1fr) !important;" in _HTML
    assert "overflow-wrap: anywhere;" in _HTML


def test_resource_operations_matrix_and_lane_hold_review_controls(tmp_path, monkeypatch):
    from aos.control_panel import get_resource_operations_matrix

    capability_dir = tmp_path / "AOS" / "capabilities"
    capability_dir.mkdir(parents=True)
    (capability_dir / "antigravity.json").write_text(
        json.dumps({
            "capability_status": "PROVEN",
            "reported_cli_version": "test-1.2.10",
            "executable_filename": "antigravity-test",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    matrix = get_resource_operations_matrix()
    assert len(matrix) >= 5
    by_name = {m["name"]: m for m in matrix}
    assert "Antigravity" in by_name
    assert "Codex CLI" in by_name
    assert "Cline" in by_name
    assert "Qwen Local" in by_name
    assert "Nemotron" in by_name

    # Check Antigravity first class attributes
    ag = by_name["Antigravity"]
    assert ag["resource_type"] == "FIRST_CLASS_AGENTIC"
    assert ag["cost_class"] == "SUBSCRIPTION_INCLUDED"
    assert ag["general_health"] == "AVAILABLE"
    assert ag["current_blocker"] == "NONE"
    assert ag["eligibility_by_task_class"]["agentic_coding"] is True

    # Codex availability is attestation-derived; quota is not hard-coded.
    cdx = by_name["Codex CLI"]
    assert cdx["resource_type"] == "FIRST_CLASS_AGENTIC"
    assert cdx["general_health"] in {"AVAILABLE", "UNKNOWN"}
    assert cdx["quota_status"] == "UNKNOWN"
    assert cdx["lifecycle_state"] == "PRESERVED_STANDBY"

    # Check Cline prerequisite / live attested capability
    cline = by_name["Cline"]
    assert cline["current_blocker"] in {"OFFICIAL_CLINE_CLI_NOT_INSTALLED", "NONE"}
    assert cline["general_health"] in {"NOT_OPERATIONALLY_PROVEN", "HEALTHY", "OPERATIONAL_BOUNDED"}

    # HTML contains Resource Operations Matrix and Lane Hold/Resume controls
    assert 'id="resource-operations-container"' in _HTML
    assert "Review " in _HTML
    assert "Resume " in _HTML
    assert "Running / " in _HTML


def test_resource_operations_matrix_requires_antigravity_attestation(tmp_path, monkeypatch):
    from aos.control_panel import get_resource_operations_matrix

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    matrix = get_resource_operations_matrix()
    ag = next(row for row in matrix if row["name"] == "Antigravity")

    assert ag["general_health"] == "UNPROVEN"
    assert ag["current_blocker"] == "ATTESTATION_REQUIRED"
    assert ag["eligibility_by_task_class"]["agentic_coding"] is False
