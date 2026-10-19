"""Disk/process/transaction regressions. No provider or task E2E claims."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from aos.v2.lifecycle import LifecycleConflict, LifecycleStore, TrustedGrant


@pytest.mark.parametrize("name", [".GIT/config", "src/NUL.py", "src/trailing."])
def test_grant_rejects_nonportable_or_git_alias_write_scope(tmp_path, name):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    grant = TrustedGrant("aos-disposable", "owner-grant", str(workspace), "a" * 40, (name,))
    with pytest.raises(ValueError):
        grant.binding()


def test_grant_rejects_hardlink_to_outside_file(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    sentinel = tmp_path / "outside.py"
    sentinel.write_text("outside sentinel\n")
    os.link(sentinel, workspace / "one.py")
    grant = TrustedGrant("aos-disposable", "owner-grant", str(workspace), "a" * 40, ("one.py",))
    with pytest.raises(ValueError):
        grant.binding()
    assert sentinel.read_text() == "outside sentinel\n"


@pytest.fixture
def prepared(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "one.py").write_text("broken\n")
    grant = TrustedGrant("aos-disposable", "owner-grant", str(workspace), "a" * 40, ("one.py",))
    store = LifecycleStore(tmp_path / "runtime-v2")
    store.admit("command-one", "request-one", "repair one", ("one-correct",), grant)
    return store, grant


def running(store):
    store.transition("command-one", 0, "PLANNING", reason="trusted plan")
    store.transition("command-one", 1, "RUNNING", reason="validated plan")


def accepted(store):
    running(store)
    assert store.begin_task("command-one", "task-one", pre_fingerprint="b" * 64, write_files=("one.py",)) == "EXECUTE_ONCE"
    store.accept_task("command-one", "task-one", result_sha="c" * 40, artifacts={"one.py": "d" * 64}, verification_ref="simulation-fixture-only")
    store.transition("command-one", 2, "VERIFYING", reason="independent verifier callback fixture")


def test_admission_is_explicit_idempotent_and_immutable(prepared):
    store, grant = prepared
    assert store.admit("ignored-new-id", "request-one", "repair one", ("one-correct",), grant) == "command-one"
    with pytest.raises(LifecycleConflict):
        store.admit("command-two", "request-one", "different goal", ("one-correct",), grant)
    assert len(store.snapshot("command-one")["events"]) == 1


def test_no_success_from_empty_graph_or_missing_criteria(prepared):
    store, _ = prepared
    store.transition("command-one", 0, "PLANNING", reason="plan")
    store.transition("command-one", 1, "VERIFYING", reason="empty graph")
    with pytest.raises(LifecycleConflict):
        store.finalize("command-one", 2, "PROJECT_COMPLETE", {"one-correct": "f" * 64})
    assert store.snapshot("command-one")["admission"] == "ACTIVE"


def test_success_closes_every_plane_and_rejects_reactivation(prepared):
    store, _ = prepared
    accepted(store)
    store.finalize("command-one", 3, "PROJECT_COMPLETE", {"one-correct": "f" * 64})
    snap = store.snapshot("command-one")
    assert snap["state"] == "PROJECT_COMPLETE" and snap["admission"] == "CLOSED"
    assert json.loads(snap["result"])["state"] == "PROJECT_COMPLETE"
    assert [e["kind"] for e in snap["events"]].count("run.finished") == 1
    with pytest.raises(LifecycleConflict):
        store.transition("command-one", 4, "QUEUED", reason="forbidden replay")
    assert len(store.pending_outbox()) == 1
    with pytest.raises(LifecycleConflict):
        store.acknowledge_outbox("command-one", "0" * 64)
    payload = store.pending_outbox()[0]["payload"]
    store.acknowledge_outbox("command-one", hashlib.sha256(payload.encode()).hexdigest())
    assert store.pending_outbox() == []


def test_uncertain_work_never_auto_replays(prepared):
    store, _ = prepared
    running(store)
    assert store.begin_task("command-one", "task-one", pre_fingerprint="b" * 64, write_files=("one.py",)) == "EXECUTE_ONCE"
    restarted = LifecycleStore(store.root)
    assert restarted.begin_task("command-one", "task-one", pre_fingerprint="b" * 64, write_files=("one.py",)) == "RECONCILE_REQUIRED"
    store.transition("command-one", 2, "PAUSED_SAFE", reason="unknown outcome")
    with pytest.raises(LifecycleConflict):
        store.transition("command-one", 3, "QUEUED", reason="unsafe automatic replay")


def test_verified_task_skip_and_identity_conflict(prepared):
    store, _ = prepared
    accepted(store)
    store.transition("command-one", 3, "RUNNING", reason="next task")
    assert store.begin_task("command-one", "task-one", pre_fingerprint="b" * 64, write_files=("one.py",)) == "ALREADY_ACCEPTED"
    with pytest.raises(LifecycleConflict):
        store.begin_task("command-one", "task-one", pre_fingerprint="e" * 64, write_files=("one.py",))


def test_two_connections_cannot_advance_same_revision(prepared):
    store, _ = prepared
    def advance(_):
        try:
            LifecycleStore(store.root).transition("command-one", 0, "PLANNING", reason="race")
            return "winner"
        except LifecycleConflict:
            return "stale"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(advance, range(2))) == ["stale", "winner"]
    assert [e["seq"] for e in store.snapshot("command-one")["events"]] == [1, 2]


@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
def test_actual_process_death_is_atomic(prepared, boundary):
    store, _ = prepared
    accepted(store)
    code = '''import os,sys
from pathlib import Path
from aos.v2.lifecycle import LifecycleStore
s=LifecycleStore(Path(sys.argv[1]))
if sys.argv[2]=='before_commit':
    with s._transaction() as db:
        db.execute("UPDATE commands SET state='PROJECT_COMPLETE',admission='CLOSED' WHERE id='command-one'")
        s._event(db,'command-one','run.finished',{'state':'PROJECT_COMPLETE'})
        os._exit(73)
else:
    s.finalize('command-one',3,'PROJECT_COMPLETE',{'one-correct':'f'*64})
    os._exit(73)
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    proc = subprocess.run([sys.executable, "-c", code, str(store.root), boundary], env=env, timeout=20)
    assert proc.returncode == 73
    restarted = LifecycleStore(store.root)
    snap = restarted.snapshot("command-one")
    finished = [e for e in snap["events"] if e["kind"] == "run.finished"]
    if boundary == "before_commit":
        assert snap["state"] == "VERIFYING" and snap["admission"] == "ACTIVE" and snap["result"] is None
        assert not finished and restarted.pending_outbox() == []
    else:
        assert snap["state"] == "PROJECT_COMPLETE" and snap["admission"] == "CLOSED"
        assert snap["result"] and len(finished) == 1 and len(restarted.pending_outbox()) == 1
    assert [e["seq"] for e in snap["events"]] == list(range(1, len(snap["events"]) + 1))


def test_authority_scope_and_root_fail_closed(prepared, tmp_path):
    store, grant = prepared
    for project, scope in [("lari", ("one.py",)), ("aos-disposable", ()), ("aos-disposable", ("../outside.py",))]:
        bad = TrustedGrant(project, grant.authority_id, grant.workspace, grant.base_sha, scope)
        with pytest.raises(ValueError):
            store.admit("denied", "denied", "repair", ("correct",), bad)
    protected = tmp_path / "runtime-v1"
    protected.mkdir()
    before = list(protected.iterdir())
    with pytest.raises(ValueError):
        LifecycleStore(protected)
    assert list(protected.iterdir()) == before
    running(store)
    with pytest.raises(ValueError):
        store.begin_task("command-one", "escape", pre_fingerprint="b" * 64, write_files=("outside.py",))


def test_symlink_scope_and_workspace_runtime_overlap_denied(prepared, tmp_path):
    store, grant = prepared
    link = Path(grant.workspace) / "linked.py"
    try:
        link.symlink_to(Path(grant.workspace) / "one.py")
    except OSError:
        pytest.skip("Host does not permit creation of symlinks")
    bad = TrustedGrant(grant.project_id, grant.authority_id, grant.workspace, grant.base_sha, ("linked.py",))
    with pytest.raises(ValueError):
        store.admit("denied", "denied", "repair", ("correct",), bad)
    inner_store = LifecycleStore(Path(grant.workspace) / "nested-runtime")
    with pytest.raises(ValueError):
        inner_store.admit("denied", "denied", "repair", ("correct",), grant)
