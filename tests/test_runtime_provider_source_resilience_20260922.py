import json
import ssl
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from aos import runtime_worker
from aos.provenance import (
    validate_materialized_runtime_provenance,
)
from aos.provider_circuit import (
    ProviderCircuitBreakerRegistry,
)
from aos.runtime_contract import (
    ContinueProjectCommand,
    NONTERMINAL_STATES,
    ProjectProfile,
)
from aos.runtime_store import RuntimeStore
from aos.source_adapter import ProjectSourceAdapter


def _command(tmp_path: Path):
    descriptor = tmp_path / "lari.autonomous-host.descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")

    policy = tmp_path / "nemotron.planner-policy.json"
    policy.write_text("{}", encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    profile = ProjectProfile(
        project_id="lari",
        descriptor_path=str(descriptor),
        workspace=str(workspace),
        routing_policy_path=str(policy),
    )

    return ContinueProjectCommand.from_mapping(
        {
            "goal": "continue",
            "continuous": True,
        },
        project=profile,
    )


@patch("urllib.request.urlopen")
def test_github_503_exact_revision_uses_read_only_immutable_fallback(
    mock_urlopen,
):
    api_error = urllib.error.HTTPError(
        "https://api.github.com/example",
        503,
        "Service Unavailable",
        {},
        None,
    )

    html_response = MagicMock()
    html_response.status = 200
    html_response.__enter__.return_value = html_response

    mock_urlopen.side_effect = [
        api_error,
        html_response,
    ]

    adapter = ProjectSourceAdapter(
        "MertSGI/Randapp-main",
        "control/lari-project-control-plane",
        tls_context=ssl.create_default_context(),
    )

    target = (
        "80ee72dbaa93d95a742995770c7bd80f69f0aaf2"
    )

    assert (
        adapter.resolve_exact_revision(
            target
        )
        ==
        target
    )

    assert mock_urlopen.call_count == 2


def test_source_transport_wait_is_nonterminal_contract_state():
    assert (
        "WAITING_FOR_SOURCE_TRANSPORT"
        in
        NONTERMINAL_STATES
    )


def test_transient_source_transport_does_not_terminalize_lineage(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"

    store = RuntimeStore(
        runtime_root
    )

    command = _command(
        tmp_path
    )

    store.create_command(
        command.to_dict()
    )

    project_runtime = (
        store.command_dir(
            command.command_id
        )
        / "project-runtime"
    )

    project_runtime.mkdir(
        parents=True
    )

    (
        project_runtime
        / "planning-kernel-checkpoint.json"
    ).write_text(
        json.dumps({
            "phase":
                "EXECUTING",

            "batch_number":
                232,

            "total_completed_batch_count":
                232,

            "canonical_source_sha":
                "9ec158f7d5c52ee65f9ecad36b76c78a99e9c1d2",

            "canonical_execution_base_sha":
                "80ee72dbaa93d95a742995770c7bd80f69f0aaf2",
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        runtime_worker,
        "hydrate_environment",
        lambda overwrite=True: {},
    )

    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: (
            _ for _ in ()
        ).throw(
            RuntimeError(
                "Failed to resolve revision "
                "'80ee72dbaa93d95a742995770c7bd80f69f0aaf2' "
                "in repository 'MertSGI/Randapp-main': "
                "HTTP Error 503: Service Unavailable"
            )
        ),
    )

    result = runtime_worker.execute_command(
        runtime_root,
        command.command_id,
    )

    state = store.read_state(
        command.command_id
    )

    assert (
        result["state"]
        ==
        "WAITING_FOR_SOURCE_TRANSPORT"
    )

    assert (
        state["state"]
        ==
        "WAITING_FOR_SOURCE_TRANSPORT"
    )

    assert (
        state["completed_batch_count"]
        ==
        232
    )

    assert (
        state["failure_class"]
        ==
        "SOURCE_TRANSPORT_UNAVAILABLE"
    )

    assert (
        state["retry_after_epoch"]
        >
        time.time()
    )

    assert (
        store.read_events(
            command.command_id
        )[-1]["event_type"]
        ==
        "run.waiting_for_source_transport"
    )


def test_worker_rebinds_runtime_owned_policy_to_active_candidate(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"

    store = RuntimeStore(
        runtime_root
    )

    command = _command(
        tmp_path
    )

    store.create_command(
        command.to_dict()
    )

    slot = tmp_path / "candidate"
    descriptors = slot / "descriptors"
    descriptors.mkdir(
        parents=True
    )

    active_descriptor = (
        descriptors
        / Path(
            command.project.descriptor_path
        ).name
    )

    active_policy = (
        descriptors
        / Path(
            command.project.routing_policy_path
        ).name
    )

    active_descriptor.write_text(
        "{}",
        encoding="utf-8",
    )

    active_policy.write_text(
        "{}",
        encoding="utf-8",
    )

    monkeypatch.setenv(
        "AOS_RUNTIME_SLOT_ROOT",
        str(slot),
    )

    monkeypatch.setattr(
        runtime_worker,
        "hydrate_environment",
        lambda overwrite=True: {},
    )

    captured = {}

    def fake_run(**kwargs):
        captured.update(
            kwargs
        )

        return {
            "disposition":
                "PROJECT_COMPLETE",

            "completed_batch_count":
                0,

            "canonical_source_sha":
                "a" * 40,

            "canonical_execution_base_sha":
                "b" * 40,
        }

    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        fake_run,
    )

    result = runtime_worker.execute_command(
        runtime_root,
        command.command_id,
    )

    assert (
        result["state"]
        ==
        "PROJECT_COMPLETE"
    )

    assert (
        captured[
            "descriptor_path"
        ].resolve()
        ==
        active_descriptor.resolve()
    )

    assert (
        captured[
            "routing_policy_path"
        ].resolve()
        ==
        active_policy.resolve()
    )

    assert (
        Path(
            captured[
                "workspace"
            ]
        ).resolve()
        ==
        Path(
            command.project.workspace
        ).resolve()
    )


def test_explicit_empty_provider_retry_set_ignores_unrelated_open_circuits(
    tmp_path,
):
    registry = ProviderCircuitBreakerRegistry(
        tmp_path
        / "circuits.json"
    )

    registry.record_failure(
        "openai_paid_safety",
        "UNKNOWN",
        now=1000.0,
    )

    retry = registry.earliest_next_probe(
        [],
        now=2000.0,
    )

    assert retry == 2060.0


def test_materialized_runtime_missing_build_evidence_is_unproven_not_fail():
    sha = (
        "0fbb6abcb6bb2166581ee21cd11da73a9eb6a66e"
    )

    result = (
        validate_materialized_runtime_provenance(
            manifest={
                "candidate_source_sha":
                    sha,
            },
            runtime_source_sha=sha,
            runtime_asset_tree_sha256=None,
        )
    )

    assert result["valid"] is False
    assert result["status"] == "UNPROVEN"

    assert (
        "BUILD_SOURCE_SHA_MISSING"
        in
        result["errors"]
    )


def test_materialized_runtime_conflicting_proof_is_fail():
    sha_a = (
        "6f044bf0cd37fdc09ad5b0b19bb66e5f32170b68"
    )

    sha_b = (
        "7f044bf0cd37fdc09ad5b0b19bb66e5f32170b69"
    )

    tree = "a" * 64

    result = (
        validate_materialized_runtime_provenance(
            manifest={
                "provenance":
                    "PROVEN",

                "candidate_source_sha":
                    sha_a,

                "build_source_sha":
                    sha_b,

                "ci_run_id":
                    35641444287,

                "candidate_tree_sha256":
                    tree,
            },
            runtime_source_sha=sha_a,
            runtime_asset_tree_sha256=tree,
        )
    )

    assert result["valid"] is False
    assert result["status"] == "FAIL"

    assert (
        "MATERIALIZED_RUNTIME_SOURCE_SHA_MISMATCH"
        in
        result["errors"]
    )


def test_materialized_runtime_provenance_survives_source_checkout_drift():
    sha = (
        "6f044bf0cd37fdc09ad5b0b19bb66e5f32170b68"
    )

    tree = "a" * 64

    result = (
        validate_materialized_runtime_provenance(
            manifest={
                "provenance":
                    "PROVEN",

                "candidate_source_sha":
                    sha,

                "build_source_sha":
                    sha,

                "ci_run_id":
                    35641444287,

                "candidate_tree_sha256":
                    tree,
            },
            runtime_source_sha=sha,
            runtime_asset_tree_sha256=tree,
        )
    )

    assert result["valid"] is True
    assert result["status"] == "PROVEN"


def test_provider_policy_uses_current_free_routes():
    root = Path(
        __file__
    ).resolve().parents[1]

    policy = json.loads(
        (
            root
            / "descriptors"
            / "nemotron.planner-policy.json"
        ).read_text(
            encoding="utf-8"
        )
    )

    providers = policy[
        "providers"
    ]

    assert (
        providers[
            "openrouter_free"
        ][
            "model_id"
        ]
        ==
        "openrouter/free"
    )

    assert (
        providers[
            "cerebras"
        ][
            "model_id"
        ]
        ==
        "gpt-oss-120b"
    )

    assert (
        providers[
            "huggingface_router"
        ][
            "model_id"
        ]
        ==
        "openai/gpt-oss-120b:fastest"
    )
