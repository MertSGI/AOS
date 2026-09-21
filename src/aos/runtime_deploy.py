"""Transactional deployment commands for self-contained Runtime V1 candidates."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import socket
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from aos.process_utils import background_python_executable, launch_background_python_script, launch_startup_authority, popen_headless, process_alive, process_tree_snapshot, run_headless
from aos.runtime_contract import CONTRACT_VERSION, utc_now, validate_configured_project_profiles
from aos.runtime_maintenance import persist_maintenance
from aos.runtime_slots import SlotManager, SlotRecord
from aos.runtime_store import atomic_json, read_json


class DeploymentError(RuntimeError):
    pass


def default_runtime_home() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AOS" / "runtime-v1"


def default_startup_dir() -> Path:
    appdata = Path(os.environ.get("APPDATA", str(Path.home())))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate(runtime_home: Path, source_sha: str) -> Path:
    return runtime_home / "candidate" / source_sha


def _candidate_runtime_config(config: Dict[str, Any], candidate: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    updated = json.loads(json.dumps(config))
    projects = updated.get("projects") or {}
    for project_id, profile in projects.items():
        if not isinstance(profile, dict):
            raise DeploymentError(f"Invalid project profile: {project_id}")
        for key in ("descriptor_path", "routing_policy_path"):
            basename = Path(str(profile.get(key) or "")).name
            replacement = candidate / "descriptors" / basename
            if not basename or not replacement.is_file():
                raise DeploymentError(f"Candidate project asset missing for {project_id}.{key}: {basename}")
            profile[key] = str(replacement)
    roots = [str(Path(item).expanduser().resolve()) for item in updated.get("authorized_roots", [])]
    if str(candidate) not in roots:
        roots.append(str(candidate))
    updated.update({
        "authorized_roots": roots,
        "candidate_source_sha": manifest["candidate_source_sha"],
        "build_source_sha": manifest["build_source_sha"],
        "runtime_slot_id": manifest["candidate_slot_id"],
        "runtime_slot_root": str(candidate),
        "runtime_asset_tree_sha256": manifest["candidate_tree_sha256"],
        "production": "NO_GO",
        "ag_backend_enabled": False,
    })
    return validate_configured_project_profiles(updated)


def _load_materializer(repo_root: Path) -> Any:
    path = repo_root / "materialize_slot.py"
    spec = importlib.util.spec_from_file_location("aos_deploy_materializer", path)
    if spec is None or spec.loader is None:
        raise DeploymentError(f"Cannot load materializer: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stage(runtime_home: Path, repo_root: Path, source_sha: str, ci_run_id: int, repo: str) -> Dict[str, Any]:
    runtime_home = runtime_home.expanduser().resolve()
    module = _load_materializer(repo_root.expanduser().resolve())
    candidate = module.materialize(
        source_sha,
        ci_run_id,
        repo_root=repo_root,
        remote_repo=repo,
        candidate_base=runtime_home / "candidate",
    )
    result = validate(runtime_home, source_sha)
    result["stage"] = "PASS"
    return result


def validate(runtime_home: Path, source_sha: str) -> Dict[str, Any]:
    candidate = _candidate(runtime_home.expanduser().resolve(), source_sha)
    manifest = read_json(candidate / "candidate-manifest.json", {})
    if manifest.get("candidate_source_sha") != source_sha or manifest.get("build_source_sha") != source_sha:
        raise DeploymentError("Candidate exact-SHA provenance mismatch")
    if manifest.get("provenance") != "PROVEN":
        raise DeploymentError("Candidate provenance is not PROVEN")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise DeploymentError("Candidate inventory is missing")
    for rel, expected in files.items():
        path = candidate / str(rel)
        if not path.is_file() or _sha256(path) != expected:
            raise DeploymentError(f"Candidate integrity mismatch: {rel}")
    for required in ("site/aos", "site/extensions", "schemas", "descriptors"):
        if not (candidate / required).is_dir():
            raise DeploymentError(f"Candidate required root missing: {required}")
    inventoried = {str(x).replace("\\", "/") for x in files}
    for root_name in ("site/aos", "site/extensions", "schemas", "descriptors"):
        for path in (candidate / root_name).rglob("*"):
            if (
                path.is_file()
                and path.suffix != ".pyc"
                and "__pycache__" not in path.parts
                and path.relative_to(candidate).as_posix() not in inventoried
            ):
                raise DeploymentError(f"Candidate file is not inventoried: {path.relative_to(candidate)}")
    forbidden = {".env", "runtime-api.token", "credentials.json", "secrets.json"}
    leaked = [str(p.relative_to(candidate)) for p in candidate.rglob("*") if p.is_file() and p.name.lower() in forbidden]
    if leaked:
        raise DeploymentError(f"Secret-bearing files copied into candidate: {leaked}")

    code = """import importlib,json,sys
from pathlib import Path
site=Path(sys.argv[1]).resolve(); sys.path.insert(0,str(site))
names=['aos','aos.runtime_server','aos.runtime_supervisor','aos.runtime_worker','aos.control_panel','extensions','extensions.autonomy_fabric.native_workers']
result={}
for name in names:
 m=importlib.import_module(name); origin=Path(m.__file__).resolve(); origin.relative_to(site); result[name]=str(origin)
from aos.validate import validate_file
candidate=site.parent
for descriptor in ('lari.autonomous-host.descriptor.json','lari-ui-v2.autonomous-host.descriptor.json'):
 verdict,_=validate_file('project_descriptor',candidate/'descriptors'/descriptor)
 if not verdict.is_valid: raise RuntimeError(f'invalid descriptor {descriptor}: {verdict.errors}')
verdict,_=validate_file('planner_routing_policy',candidate/'descriptors'/'nemotron.planner-policy.json')
if not verdict.is_valid: raise RuntimeError(f'invalid routing policy: {verdict.errors}')
print(json.dumps(result,sort_keys=True))
"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    proof = run_headless(
        [sys.executable, "-B", "-I", "-c", code, str(candidate / "site")],
        timeout=30,
        env=env,
    )
    if proof.returncode != 0:
        raise DeploymentError(f"Candidate import isolation failed: {(proof.stderr or proof.stdout)[-1000:]}")
    imports = json.loads(proof.stdout)
    for descriptor in (candidate / "descriptors").glob("*.json"):
        json.loads(descriptor.read_text(encoding="utf-8"))
    config = read_json(runtime_home / "runtime-config.json", {})
    if config and config.get("production") != "NO_GO":
        raise DeploymentError("Runtime config must remain production NO_GO")
    if config and config.get("projects"):
        _candidate_runtime_config(config, candidate, manifest)
    return {
        "validation": "PASS",
        "candidate": str(candidate),
        "source_sha": source_sha,
        "ci_run_id": manifest.get("ci_run_id"),
        "candidate_tree_sha256": manifest.get("candidate_tree_sha256"),
        "file_count": len(files),
        "import_origins": imports,
        "production": "NO_GO",
    }


def _startup_path(startup_dir: Path) -> Path:
    return startup_dir / "AOS-Runtime-V1-Supervisor.pyw"


def validate_startup_ownership(startup_dir: Path, expected: Optional[Path] = None) -> Dict[str, Any]:
    startup_dir.mkdir(parents=True, exist_ok=True)
    enabled_suffixes = {".cmd", ".bat", ".lnk", ".pyw", ".py", ".vbs", ".exe"}
    entries = [p for p in startup_dir.iterdir() if "aos" in p.name.lower() and p.suffix.lower() in enabled_suffixes]
    allowed = {expected.resolve()} if expected is not None and expected.exists() else set()
    duplicates = [str(p) for p in entries if p.resolve() not in allowed]
    if duplicates:
        raise DeploymentError(f"Duplicate enabled AOS startup authorities: {duplicates}")
    return {"startup_authority_count": len(entries), "entries": [str(p) for p in entries]}


def _install_startup(startup_dir: Path, candidate: Path) -> Path:
    path = _startup_path(startup_dir)
    validate_startup_ownership(startup_dir, path if path.exists() else None)
    code = (
        "import runpy\n"
        f"runpy.run_path({str(candidate / 'launch_supervisor.py')!r}, run_name='__main__')\n"
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(code, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    validate_startup_ownership(startup_dir, path)
    return path


def _health(url: str, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    if port in (8765, 8770):
        return _free_port()
    return port


def smoke(runtime_home: Path, source_sha: str) -> Dict[str, Any]:
    """Run a real, isolated supervisor/runtime/panel acceptance."""
    runtime_home = runtime_home.expanduser().resolve()
    if runtime_home == default_runtime_home().expanduser().resolve():
        raise DeploymentError("Smoke refuses the live Runtime V1 root")
    validation = validate(runtime_home, source_sha)
    candidate = Path(validation["candidate"])
    smoke_id = uuid.uuid4().hex
    smoke_root = runtime_home / "smoke" / smoke_id
    state = smoke_root / "state"
    workspace = smoke_root / "workspace"
    workspace.mkdir(parents=True)
    runtime_port = _free_port()
    panel_port = _free_port()
    token = secrets.token_urlsafe(48)
    token_path = runtime_home / "runtime-api.token"
    token_path.write_text(token + "\n", encoding="utf-8")
    manifest = read_json(candidate / "candidate-manifest.json", {})
    project = {
        "project_id": "lari",
        "descriptor_path": str(candidate / "descriptors" / "lari.autonomous-host.descriptor.json"),
        "workspace": str(workspace),
        "routing_policy_path": str(candidate / "descriptors" / "nemotron.planner-policy.json"),
        "standing_authority": True,
    }
    config = {
        "contract_version": CONTRACT_VERSION,
        "runtime_root": str(state),
        "runtime_token_path": str(token_path),
        "authorized_roots": [str(candidate), str(smoke_root)],
        "projects": {"lari": project},
        "default_project": "lari",
        "bind_host": "127.0.0.1",
        "port": runtime_port,
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "candidate_source_sha": source_sha,
        "build_source_sha": source_sha,
        "runtime_slot_id": manifest["candidate_slot_id"],
        "runtime_slot_root": str(candidate),
        "runtime_asset_tree_sha256": manifest["candidate_tree_sha256"],
        "controller_relay_dir": str(smoke_root / "relay"),
    }
    atomic_json(runtime_home / "runtime-config.json", config)
    atomic_json(runtime_home / "control-panel-host-config.json", {
        "schema_version": "1.0.0",
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "authorized_roots": config["authorized_roots"],
        "runtime_root": str(state),
        "runtime_api_url": f"http://127.0.0.1:{runtime_port}",
        "runtime_token_path": str(token_path),
        "default_project": project,
        "default_project_id": "lari",
        "projects": {"lari": project},
        "authoritative_repo_path": str(candidate),
    })
    atomic_json(runtime_home / "control-panel-config.json", {
        "schema_version": "1.0.0",
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "bind_host": "127.0.0.1",
        "port": panel_port,
        "panel_token": secrets.token_urlsafe(48),
    })
    supervisor_root = smoke_root / "supervisor"
    atomic_json(runtime_home / "supervisor-config.json", {
        "contract_version": CONTRACT_VERSION,
        "supervisor_root": str(supervisor_root),
        "runtime_config_path": str(runtime_home / "runtime-config.json"),
        "panel_host_config_path": str(runtime_home / "control-panel-host-config.json"),
        "panel_config_path": str(runtime_home / "control-panel-config.json"),
        "panel_health_url": f"http://127.0.0.1:{panel_port}/health",
        "panel_enabled": True,
        "poll_seconds": 1,
        "health_grace_seconds": 15,
        "candidate_restart_limit": 2,
        "controller_relay_dir": str(smoke_root / "relay"),
        "singleton_name": f"Local\\AOS.RuntimeV1.Smoke.{smoke_id}",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    })
    slots = SlotManager(supervisor_root)
    slot = SlotRecord(
        slot_id=manifest["candidate_slot_id"],
        kind="runtime_v1",
        command=(background_python_executable(sys.executable), str(candidate / "launch_runtime_server.py")),
        source_sha=source_sha,
        health_url=f"http://127.0.0.1:{runtime_port}/v1/health",
        config_path=str(runtime_home / "runtime-config.json"),
        created_at=utc_now(),
    )
    slots.write_slot(slot)
    slots.initialize(stable_slot_id=slot.slot_id, candidate_slot_id=slot.slot_id, active="stable")
    atomic_json(supervisor_root / "control-request.json", {"action": "START", "requested_at": utc_now()})
    persist_maintenance(state, paused=True, reason="synthetic_smoke_default")
    for command_id, command_state in (
        ("synthetic-active", {"state": "RUNNING", "worker_pid": None}),
        ("synthetic-waiting", {"state": "WAITING_FOR_REASONING_PROVIDER", "retry_after_epoch": time.time() + 3600}),
    ):
        command = state / "commands" / command_id
        command.mkdir(parents=True)
        atomic_json(command / "command.json", {"command_id": command_id, "project": project})
        atomic_json(command / "state.json", command_state)
    for index in range(550):
        command = state / "commands" / f"synthetic-history-{index:04d}"
        command.mkdir(parents=True)
        atomic_json(command / "command.json", {"command_id": command.name, "project": project})
        atomic_json(command / "state.json", {"state": "PROJECT_COMPLETE"})

    evidence_dir = smoke_root / "evidence"
    evidence_dir.mkdir(parents=True)
    before: list[Dict[str, Any]] = []
    atomic_json(evidence_dir / "process-tree-before.json", {"processes": before})
    supervisor_proc = None
    runtime_url = f"http://127.0.0.1:{runtime_port}/v1/health"
    panel_url = f"http://127.0.0.1:{panel_port}/health"
    def post(path: str, payload: bytes = b"{}") -> Dict[str, Any]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{runtime_port}{path}",
            data=payload,
            headers={"Content-Type": "application/json", "X-AOS-Runtime-Token": token},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    try:
        supervisor_proc = popen_headless([background_python_executable(sys.executable), str(candidate / "launch_supervisor.py")], detached=True)
        deadline = time.monotonic() + 30
        runtime_health = panel_health = None
        startup_samples: list[float] = []
        ready_samples: list[float] = []
        while time.monotonic() < deadline:
            started = time.perf_counter()
            runtime_health = _health(runtime_url)
            startup_samples.append(time.perf_counter() - started)
            panel_health = _health(panel_url)
            if runtime_health and panel_health:
                break
            time.sleep(0.1)
        if not runtime_health or runtime_health.get("paused") is not True:
            raise DeploymentError("Synthetic runtime did not boot persisted PAUSED_SAFE")
        if runtime_health.get("runtime_source_sha") != source_sha:
            raise DeploymentError("Synthetic runtime exact-SHA identity failed")
        if not panel_health or panel_health.get("panel_state") != "HEALTHY":
            raise DeploymentError("Synthetic panel health failed")
        atomic_json(state / "commands" / "synthetic-active" / "state.json", {
            "state": "RUNNING",
            "worker_pid": runtime_health["pid"],
        })
        for _ in range(25):
            started = time.perf_counter()
            if not _health(runtime_url):
                raise DeploymentError("Synthetic health request failed")
            ready_samples.append(time.perf_counter() - started)
        supervisor_state = read_json(supervisor_root / "supervisor-state.json", {})
        tree_roots = [supervisor_proc.pid]

        for raw_pid in (
            supervisor_state.get("supervisor_pid"),
            runtime_health.get("pid"),
            panel_health.get("pid"),
        ):
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                continue
            if pid > 0 and pid not in tree_roots:
                tree_roots.append(pid)

        during = process_tree_snapshot(tree_roots)
        atomic_json(
            evidence_dir / "process-tree-during.json",
            {"root_pids": tree_roots, "processes": during},
        )

        conhosts = [
            row for row in during
            if str(row.get("name") or "").lower() == "conhost.exe"
        ]

        if conhosts:
            raise DeploymentError(
                f"Synthetic AOS tree owns conhost descendants: {conhosts}"
            )

        # Resume only the synthetic history long enough to prove the supervisor's
        # slow relay enrichment runs off the lifecycle thread, then pause again.
        post("/v1/commands/resume")
        relay_latest = smoke_root / "relay" / "LATEST.json"
        relay_deadline = time.monotonic() + 25
        while not relay_latest.is_file() and time.monotonic() < relay_deadline:
            time.sleep(0.1)
        if not relay_latest.is_file():
            raise DeploymentError("Synthetic supervisor relay cycle did not complete")

        after_resume = process_tree_snapshot(tree_roots)
        atomic_json(
            evidence_dir / "process-tree-after-resume.json",
            {"root_pids": tree_roots, "processes": after_resume},
        )

        resume_conhosts = [
            row for row in after_resume
            if str(row.get("name") or "").lower() == "conhost.exe"
        ]

        if resume_conhosts:
            raise DeploymentError(
                f"Synthetic resumed AOS tree owns conhost descendants: {resume_conhosts}"
            )

        post("/v1/commands/pause-safe")

        # Shut down the complete owned tree, restart the supervisor, and prove
        # persistent PAUSED_SAFE survives the restart.
        shutdown_result = post("/v1/commands/shutdown", b'{"timeout_seconds":1}')
        deadline = time.monotonic() + 15
        while supervisor_proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        supervisor_proc.close()
        atomic_json(supervisor_root / "control-request.json", {"action": "START", "requested_at": utc_now()})
        supervisor_proc = popen_headless([background_python_executable(sys.executable), str(candidate / "launch_supervisor.py")], detached=True)
        deadline = time.monotonic() + 20
        restarted = None
        while time.monotonic() < deadline:
            restarted = _health(runtime_url)
            if restarted:
                break
            time.sleep(0.1)
        if not restarted or restarted.get("paused") is not True:
            raise DeploymentError("Restart while paused did not remain PAUSED_SAFE")
        shutdown_result = post("/v1/commands/shutdown", b'{"timeout_seconds":1}')
    finally:
        if supervisor_proc is not None:
            supervisor_proc.close()
    after = process_tree_snapshot([supervisor_proc.pid] if supervisor_proc else [])
    atomic_json(evidence_dir / "process-tree-after.json", {"processes": after})
    orphans = [row for row in after if process_alive(row["pid"])]
    if orphans:
        raise DeploymentError(f"Synthetic AOS orphan processes remain: {orphans}")
    return {
        **validation,
        "smoke": "PASS",
        "smoke_id": smoke_id,
        "synthetic_root": str(smoke_root),
        "runtime_port": runtime_port,
        "panel_port": panel_port,
        "historical_command_count": 550,
        "synthetic_active_command_count": 1,
        "synthetic_waiting_command_count": 1,
        "health_ready_max_seconds": max(ready_samples),
        "startup_probe_max_seconds": max(startup_samples),
        "process_tree_before": str(evidence_dir / "process-tree-before.json"),
        "process_tree_during": str(evidence_dir / "process-tree-during.json"),
        "process_tree_after": str(evidence_dir / "process-tree-after.json"),
        "shutdown": shutdown_result,
        "conhost_descendant_count": 0,
        "orphan_count": 0,
        "production": "NO_GO",
    }


def activate(
    runtime_home: Path,
    source_sha: str,
    startup_dir: Path,
    *,
    launch: bool = True,
    install_startup: bool = True,
    proof_timeout: float = 45.0,
) -> Dict[str, Any]:
    runtime_home = runtime_home.expanduser().resolve()
    validation = validate(runtime_home, source_sha)
    candidate = Path(validation["candidate"])
    manifest = read_json(candidate / "candidate-manifest.json", {})
    config_path = runtime_home / "runtime-config.json"
    config = read_json(config_path, {})
    if not config:
        raise DeploymentError(f"Runtime config missing: {config_path}")
    if config.get("production") != "NO_GO":
        raise DeploymentError("Activation is forbidden unless production is NO_GO")
    runtime_root = Path(config["runtime_root"]).expanduser().resolve()
    persist_maintenance(runtime_root, paused=True, reason="candidate_activation_default")

    supervisor_root = Path(read_json(runtime_home / "supervisor-config.json", {}).get("supervisor_root") or (runtime_home / "supervisor"))
    slots = SlotManager(supervisor_root)
    previous = read_json(slots.pointer, {})
    if not previous:
        raise DeploymentError("Activation requires an existing accepted slot for deterministic rollback")
    old_key = "candidate_slot_id" if previous.get("active") == "candidate" else "stable_slot_id"
    previous_slot = str(previous.get(old_key) or "")
    if not previous_slot:
        raise DeploymentError("Existing active slot is invalid")

    txid = f"activate-{int(time.time())}-{secrets.token_hex(4)}"
    backup = runtime_home / "deployment-backups" / txid
    backup.mkdir(parents=True, exist_ok=False)
    for path in (config_path, slots.pointer):
        if path.is_file():
            shutil.copy2(path, backup / path.name)
    startup_dir = startup_dir.expanduser().resolve()
    startup_path = _startup_path(startup_dir)

    if install_startup:
        validate_startup_ownership(
            startup_dir,
            startup_path if startup_path.exists() else None,
        )
    else:
        # Maintenance-only activation requires Startup to remain completely
        # disabled. Any enabled AOS authority fails closed.
        validate_startup_ownership(startup_dir, None)

    startup_existed = startup_path.is_file()

    if install_startup and startup_existed:
        shutil.copy2(startup_path, backup / "startup-authority.pyw")

    atomic_json(backup / "transaction.json", {
        "transaction_id": txid,
        "source_sha": source_sha,
        "previous_slot_id": previous_slot,
        "supervisor_root": str(supervisor_root),
        "startup_path": str(startup_path),
        "startup_existed": startup_existed,
        "startup_managed": install_startup,
        "created_at": utc_now(),
    })

    try:
        config = _candidate_runtime_config(config, candidate, manifest)
        atomic_json(config_path, config)
        slot = SlotRecord(
            slot_id=manifest["candidate_slot_id"],
            kind="runtime_v1",
            command=(background_python_executable(sys.executable), str(candidate / "launch_runtime_server.py")),
            source_sha=source_sha,
            health_url=f"http://127.0.0.1:{int(config['port'])}/v1/health",
            config_path=str(config_path),
            created_at=utc_now(),
        )
        slots.write_slot(slot)
        atomic_json(slots.pointer, {
            "contract_version": CONTRACT_VERSION,
            "stable_slot_id": previous_slot,
            "candidate_slot_id": slot.slot_id,
            "active": "candidate",
            "promotion_state": "TRIAL_MAINTENANCE",
            "updated_at": utc_now(),
        })
        atomic_json(
            supervisor_root / "control-request.json",
            {"action": "START", "requested_at": utc_now()},
        )

        startup = None

        if install_startup:
            startup = _install_startup(startup_dir, candidate)

        if not launch:
            return {
                **validation,
                "activation": (
                    "STAGED_MAINTENANCE"
                    if install_startup
                    else "STAGED_MAINTENANCE_NO_STARTUP"
                ),
                "transaction_id": txid,
                "startup": str(startup) if startup else None,
                "startup_installed": bool(startup),
            }

        if startup is not None:
            launch_startup_authority(startup)
        else:
            launch_background_python_script(
                candidate / "launch_supervisor.py"
            )
        deadline = time.monotonic() + max(5.0, proof_timeout)
        health_url = slot.health_url or ""
        panel_port = int(read_json(runtime_home / "control-panel-config.json", {}).get("port", 8765))
        panel_url = f"http://127.0.0.1:{panel_port}/health"
        health = panel = None
        while time.monotonic() < deadline:
            health = _health(health_url)
            panel = _health(panel_url)
            if (
                health and health.get("runtime_source_sha") == source_sha
                and health.get("paused") is True and panel and panel.get("panel_state") == "HEALTHY"
            ):
                break
            time.sleep(0.25)
        if not health or health.get("runtime_source_sha") != source_sha or health.get("paused") is not True:
            raise DeploymentError("Activation exact-SHA paused health proof failed")
        if not panel or panel.get("panel_state") != "HEALTHY":
            raise DeploymentError("Activation panel proof failed")
        return {
            **validation,
            "activation": "PASS_MAINTENANCE",
            "transaction_id": txid,
            "runtime_health": health,
            "panel_health": panel,
            "startup": str(startup) if startup else None,
            "startup_installed": bool(startup),
        }
    except Exception as exc:
        try:
            _restore_transaction(runtime_home, backup, startup_dir)
        except Exception as rollback_error:
            raise DeploymentError(f"Activation failed ({exc}); automatic rollback also failed ({rollback_error})") from exc
        raise


def _restore_transaction(runtime_home: Path, backup: Path, startup_dir: Path) -> str:
    tx = read_json(backup / "transaction.json", {})
    if not tx:
        raise DeploymentError(f"Rollback transaction metadata missing: {backup}")
    current = read_json(runtime_home / "runtime-config.json", {})
    if current.get("runtime_root"):
        persist_maintenance(Path(current["runtime_root"]), paused=True, reason="deployment_rollback")
    supervisor = Path(tx["supervisor_root"]).expanduser().resolve()
    atomic_json(supervisor / "control-request.json", {"action": "SHUTDOWN", "requested_at": utc_now()})
    for name, target in (
        ("runtime-config.json", runtime_home / "runtime-config.json"),
        ("active-slot.json", supervisor / "active-slot.json"),
    ):
        source = backup / name
        if source.is_file():
            atomic_json(target, read_json(source, {}))
    if bool(tx.get("startup_managed", True)):
        startup = _startup_path(startup_dir.expanduser().resolve())
        startup_backup = backup / "startup-authority.pyw"

        if bool(tx.get("startup_existed")) and startup_backup.is_file():
            tmp = startup.with_suffix(startup.suffix + ".rollback.tmp")
            shutil.copy2(startup_backup, tmp)
            os.replace(tmp, startup)
        elif startup.is_file():
            startup.unlink()
    return str(tx.get("previous_slot_id") or "")


def rollback(runtime_home: Path, transaction_id: str, startup_dir: Path) -> Dict[str, Any]:
    runtime_home = runtime_home.expanduser().resolve()
    backup = runtime_home / "deployment-backups" / transaction_id
    tx = read_json(backup / "transaction.json", {})
    if not tx:
        raise DeploymentError(f"Rollback transaction not found: {transaction_id}")
    previous_slot = _restore_transaction(runtime_home, backup, startup_dir)
    return {"rollback": "PASS", "transaction_id": transaction_id, "restored_slot_id": previous_slot}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transactional AOS Runtime deployment")
    parser.add_argument("--runtime-home", default=str(default_runtime_home()))
    parser.add_argument("--startup-dir", default=str(default_startup_dir()))
    sub = parser.add_subparsers(dest="command", required=True)
    stage_p = sub.add_parser("stage")
    stage_p.add_argument("--repo-root", required=True)
    stage_p.add_argument("--source-sha", required=True)
    stage_p.add_argument("--ci-run-id", required=True, type=int)
    stage_p.add_argument("--repo", default="MertSGI/AOS")
    validate_p = sub.add_parser("validate")
    validate_p.add_argument("--source-sha", required=True)
    smoke_p = sub.add_parser("smoke")
    smoke_p.add_argument("--source-sha", required=True)
    activate_p = sub.add_parser("activate")
    activate_p.add_argument("--source-sha", required=True)
    activate_p.add_argument("--no-start", action="store_true")
    activate_p.add_argument(
        "--without-startup",
        action="store_true",
        help="Activate/start candidate without installing a persistent Startup authority",
    )
    rollback_p = sub.add_parser("rollback")
    rollback_p.add_argument("--transaction-id", required=True)
    sub.add_parser("startup-status")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_home = Path(args.runtime_home)
    startup_dir = Path(args.startup_dir)
    try:
        if args.command == "stage":
            result = stage(runtime_home, Path(args.repo_root), args.source_sha, args.ci_run_id, args.repo)
        elif args.command == "validate":
            result = validate(runtime_home, args.source_sha)
        elif args.command == "smoke":
            result = smoke(runtime_home, args.source_sha)
        elif args.command == "activate":
            result = activate(
                runtime_home,
                args.source_sha,
                startup_dir,
                launch=not args.no_start,
                install_startup=not args.without_startup,
            )
        elif args.command == "rollback":
            result = rollback(runtime_home, args.transaction_id, startup_dir)
        else:
            expected = _startup_path(startup_dir)
            result = validate_startup_ownership(startup_dir, expected if expected.exists() else None)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"AOS_RUNTIME_DEPLOY_HOLD: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
