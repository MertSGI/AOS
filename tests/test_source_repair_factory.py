import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

import aos.source_repair_factory as factory
from aos.knowledge.hooks import ledger_for_runtime
from aos.knowledge.receipts import record_implementation_receipt, record_verification_receipt
from aos.platform_recovery import SourceRepairResourceUnavailable


REPAIR_SHA = "a" * 40
OTHER_SHA = "b" * 40


def _authority_config(tmp_path: Path):
    operations_repo = tmp_path / "aos-operations"
    authoritative_repo = tmp_path / "aos-authoritative"
    lari_workspace = tmp_path / "lari-workspace"
    maintenance_workspace = tmp_path / "maintenance-workspace"
    for path in (
        operations_repo,
        authoritative_repo,
        lari_workspace,
        maintenance_workspace,
    ):
        path.mkdir()
    product_policy = tmp_path / "lari-policy.json"
    product_policy.write_text("{}", encoding="utf-8")
    maintenance_policy = tmp_path / "aos-maintenance-policy.json"
    maintenance_policy.write_text("{}", encoding="utf-8")
    return {
        "runtime_root": str(tmp_path / "runtime-v1" / "state"),
        "operations_repo_path": str(operations_repo),
        "authoritative_repo_path": str(authoritative_repo),
        "candidate_source_sha": REPAIR_SHA,
        "default_project": "lari",
        "projects": {
            "lari": {
                "workspace": str(lari_workspace),
                "routing_policy_path": str(product_policy),
            },
            "lari-ui-v2": {
                "workspace": str(tmp_path / "lari-ui-v2-workspace"),
                "routing_policy_path": str(product_policy),
            },
            "aos-maintenance": {
                "workspace": str(maintenance_workspace),
                "routing_policy_path": str(maintenance_policy),
            },
        },
        "production": "NO_GO",
        "paid_api_fallback": "DISABLED",
    }


def test_source_repair_authority_prefers_operations_repo_and_maintenance_policy(
    monkeypatch, tmp_path
):
    config = _authority_config(tmp_path)
    original = deepcopy(config)
    git_calls = []
    monkeypatch.setattr(
        factory,
        "_git",
        lambda repository, *args, **_kwargs: (
            git_calls.append((repository, args)) or "true\n"
        ),
    )
    captured = {}
    sentinel = object()
    monkeypatch.setattr(
        factory,
        "create_source_repair_executor",
        lambda **kwargs: captured.update(kwargs) or sentinel,
    )

    result = factory.create_source_repair_executor_from_config(config)

    operations_repo = Path(config["operations_repo_path"]).resolve()
    lari_workspace = Path(config["projects"]["lari"]["workspace"]).resolve()
    maintenance_policy = Path(
        config["projects"]["aos-maintenance"]["routing_policy_path"]
    ).resolve()
    assert result is sentinel
    assert captured["repository"] == operations_repo
    assert captured["repository"] != lari_workspace
    assert captured["policy_path"] == maintenance_policy
    assert git_calls == [
        (operations_repo, ("rev-parse", "--is-inside-work-tree")),
        (operations_repo, ("cat-file", "-e", f"{REPAIR_SHA}^{{commit}}")),
    ]
    assert config == original
    assert config["production"] == "NO_GO"
    assert config["paid_api_fallback"] == "DISABLED"


def test_source_repair_authority_fails_closed_without_repository(monkeypatch, tmp_path):
    config = _authority_config(tmp_path)
    config.pop("operations_repo_path")
    config.pop("authoritative_repo_path")
    monkeypatch.setattr(
        factory,
        "create_source_repair_executor",
        lambda **_kwargs: pytest.fail("factory must not run without AOS authority"),
    )

    assert factory.create_source_repair_executor_from_config(config) is None


def test_source_repair_authority_fails_closed_when_base_sha_is_absent(
    monkeypatch, tmp_path
):
    config = _authority_config(tmp_path)

    def fake_git(_repository, *args, **_kwargs):
        if args[:2] == ("rev-parse", "--is-inside-work-tree"):
            return "true\n"
        raise RuntimeError("missing commit")

    monkeypatch.setattr(factory, "_git", fake_git)
    monkeypatch.setattr(
        factory,
        "create_source_repair_executor",
        lambda **_kwargs: pytest.fail("factory must not run for absent base SHA"),
    )

    assert factory.create_source_repair_executor_from_config(config) is None


@pytest.mark.parametrize("product_id", ["lari", "lari-ui-v2"])
def test_source_repair_authority_rejects_product_workspace_identity(
    monkeypatch, tmp_path, product_id
):
    config = _authority_config(tmp_path)
    config["operations_repo_path"] = config["projects"][product_id]["workspace"]
    monkeypatch.setattr(
        factory,
        "_git",
        lambda *_args, **_kwargs: pytest.fail("product repository must be rejected before git"),
    )

    assert factory.create_source_repair_executor_from_config(config) is None


def _result(*, status, evidence, errors=None):
    return SimpleNamespace(
        status=status,
        evidence_payload=evidence,
        sanitized_errors=list(errors or []),
    )


class _Worker:
    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        if len(self.results) == 1:
            return self.results[0]
        return self.results.pop(0)


def test_publisher_independently_observes_remote_sha(monkeypatch, tmp_path):
    worker = _Worker([_result(status="SUCCESS", evidence={})])
    observed_commands = []

    def fake_git(repository, *args, **_kwargs):
        observed_commands.append((repository, args))
        return f"{REPAIR_SHA}\trefs/heads/repair/aos-system-test\n"

    monkeypatch.setattr(factory, "_git", fake_git)
    publication = factory._make_publisher(worker)(
        tmp_path, "repair/aos-system-test", REPAIR_SHA
    )

    assert publication["remote_sha"] == REPAIR_SHA
    assert publication["remote_observation"] == "git-ls-remote"
    assert observed_commands == [(
        tmp_path,
        (
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/repair/aos-system-test",
        ),
    )]
    assert worker.requests[0].payload["args"] == [
        "origin", "repair/aos-system-test"
    ]


def test_publisher_fails_closed_on_remote_sha_mismatch(monkeypatch, tmp_path):
    worker = _Worker([_result(status="SUCCESS", evidence={})])
    monkeypatch.setattr(
        factory,
        "_git",
        lambda *_args, **_kwargs: (
            f"{OTHER_SHA}\trefs/heads/repair/aos-system-test\n"
        ),
    )

    with pytest.raises(ValueError, match="SHA mismatch"):
        factory._make_publisher(worker)(
            tmp_path, "repair/aos-system-test", REPAIR_SHA
        )


def test_certifier_polls_then_invokes_real_stage_materializer_under_runtime_home(
    monkeypatch, tmp_path
):
    runtime_home = tmp_path / "runtime-v1"
    workspace = tmp_path / "repair-worktree"
    workspace.mkdir()
    ledger = ledger_for_runtime(runtime_home)
    record_implementation_receipt(
        ledger, project_id="AOS", idempotency_key="certifier-test-impl",
        agent_class="CODEX", tool_name="pytest", base_sha=OTHER_SHA,
        result_sha=REPAIR_SHA, changed_paths=["src/aos/example.py"],
    )
    record_verification_receipt(
        ledger, project_id="AOS", idempotency_key="certifier-test-verify",
        agent_class="CODEX", tool_name="pytest", result_sha=REPAIR_SHA,
        changed_paths=["src/aos/example.py"], verification={"status": "SUCCESS"},
    )
    worker = _Worker([
        _result(
            status="DEGRADED",
            evidence={
                "sha": REPAIR_SHA,
                "status": "queued",
                "conclusion": None,
                "run_id": 71,
            },
        ),
        _result(
            status="SUCCESS",
            evidence={
                "sha": REPAIR_SHA,
                "status": "completed",
                "conclusion": "success",
                "run_id": 71,
            },
        ),
    ])
    materialize_calls = []

    class Materializer:
        @staticmethod
        def materialize(
            source_sha,
            ci_run_id,
            *,
            repo_root,
            remote_repo,
            candidate_base,
        ):
            materialize_calls.append({
                "source_sha": source_sha,
                "ci_run_id": ci_run_id,
                "repo_root": repo_root,
                "remote_repo": remote_repo,
                "candidate_base": candidate_base,
            })
            candidate = Path(candidate_base) / source_sha
            candidate.mkdir(parents=True)
            (candidate / "candidate-manifest.json").write_text(
                json.dumps({
                    "candidate_source_sha": source_sha,
                    "build_source_sha": source_sha,
                    "ci_run_id": ci_run_id,
                    "provenance": "PROVEN",
                    "files": {"proof.txt": "hash"},
                }),
                encoding="utf-8",
            )
            return candidate

    validation_calls = []

    def fake_validate(observed_runtime_home, source_sha):
        validation_calls.append((observed_runtime_home, source_sha))
        return {
            "validation": "PASS",
            "candidate": str(observed_runtime_home / "candidate" / source_sha),
            "source_sha": source_sha,
            "production": "NO_GO",
        }

    monkeypatch.setattr(factory.runtime_deploy, "_load_materializer", lambda _: Materializer)
    monkeypatch.setattr(factory.runtime_deploy, "validate", fake_validate)
    monkeypatch.setattr(
        factory.runtime_deploy,
        "activate",
        lambda *_args, **_kwargs: pytest.fail("candidate must not be activated"),
    )

    certification = factory._make_certifier(
        worker,
        runtime_home,
        ci_timeout_seconds=2,
        ci_poll_interval_seconds=0.1,
    )(workspace, "repair/aos-system-test", REPAIR_SHA, {"remote_sha": REPAIR_SHA})

    candidate = runtime_home.resolve() / "candidate" / REPAIR_SHA
    assert len(worker.requests) == 2
    assert all(request.payload["sha"] == REPAIR_SHA for request in worker.requests)
    assert materialize_calls == [{
        "source_sha": REPAIR_SHA,
        "ci_run_id": 71,
        "repo_root": workspace,
        "remote_repo": "MertSGI/AOS",
        "candidate_base": runtime_home.resolve() / "candidate",
    }]
    assert validation_calls == [
        (runtime_home.resolve(), REPAIR_SHA),
        (runtime_home.resolve(), REPAIR_SHA),
    ]
    assert Path(certification["evidence"]["candidate_manifest_path"]) == (
        candidate / "candidate-manifest.json"
    )
    assert certification["candidate_manifest"]["candidate_source_sha"] == REPAIR_SHA
    assert certification["candidate_manifest"]["build_source_sha"] == REPAIR_SHA
    assert certification["candidate_materialized"] is True
    assert certification["evidence"]["ci_poll_attempts"] == 2
    assert "activation_performed" not in certification
    assert "promotion_performed" not in certification


def test_ci_success_alone_cannot_claim_candidate_materialization(monkeypatch, tmp_path):
    worker = _Worker([_result(
        status="SUCCESS",
        evidence={
            "sha": REPAIR_SHA,
            "status": "completed",
            "conclusion": "success",
            "run_id": 72,
        },
    )])
    monkeypatch.setattr(
        factory.runtime_deploy,
        "stage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("materialization failed")
        ),
    )
    ledger = ledger_for_runtime(tmp_path / "runtime-v1")
    record_implementation_receipt(
        ledger, project_id="AOS", idempotency_key="failed-stage-impl",
        agent_class="CODEX", tool_name="pytest", base_sha=OTHER_SHA,
        result_sha=REPAIR_SHA, changed_paths=["src/aos/example.py"],
    )
    record_verification_receipt(
        ledger, project_id="AOS", idempotency_key="failed-stage-verify",
        agent_class="CODEX", tool_name="pytest", result_sha=REPAIR_SHA,
        changed_paths=["src/aos/example.py"], verification={"status": "SUCCESS"},
    )

    certifier = factory._make_certifier(worker, tmp_path / "runtime-v1")
    with pytest.raises(RuntimeError, match="materialization failed"):
        certifier(tmp_path, "repair/test", REPAIR_SHA, {"remote_sha": REPAIR_SHA})


def test_failed_ci_never_materializes(monkeypatch, tmp_path):
    worker = _Worker([_result(
        status="FAILED",
        evidence={
            "sha": REPAIR_SHA,
            "status": "completed",
            "conclusion": "failure",
            "run_id": 73,
        },
    )])
    stage_calls = []
    monkeypatch.setattr(
        factory.runtime_deploy,
        "stage",
        lambda *_args, **_kwargs: stage_calls.append(True),
    )

    certifier = factory._make_certifier(worker, tmp_path / "runtime-v1")
    with pytest.raises(RuntimeError, match="EXACT_SHA_CI_FAILED"):
        certifier(tmp_path, "repair/test", REPAIR_SHA, {"remote_sha": REPAIR_SHA})
    assert stage_calls == []


def test_exact_sha_ci_wait_is_bounded_and_typed(tmp_path):
    worker = _Worker([_result(
        status="FAILED",
        evidence={"sha": REPAIR_SHA, "conclusion": "NO_RUNS"},
        errors=[f"NO_BOUND_CI_RUN_FOR_SHA: {REPAIR_SHA}"],
    )])
    now = [0.0]

    with pytest.raises(SourceRepairResourceUnavailable, match="EXACT_SHA_CI_TIMEOUT"):
        factory._wait_for_exact_sha_ci(
            worker,
            tmp_path,
            REPAIR_SHA,
            "MertSGI/AOS",
            timeout_seconds=1,
            poll_interval_seconds=0.4,
            monotonic=lambda: now[0],
            sleep=lambda duration: now.__setitem__(0, now[0] + duration),
        )

    assert now[0] == pytest.approx(1.0)
    assert all(request.payload["sha"] == REPAIR_SHA for request in worker.requests)
