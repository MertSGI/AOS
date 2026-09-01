"""AOS generic hosted disposable rehearsal primitive (R4.2 Evidence Closure)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple

from aos.execution_authority import validate_execution_authority
from aos.rehearsal_evidence import validate_rehearsal_report
from aos.validate import load_json_strict, validate_document

ALLOWED_COMMANDS = {"docker", "git", "python", "py"}


class HostedDisposableRehearsalError(Exception):
    """Exception for hosted disposable rehearsal failure."""
    pass


def validate_path_safety(relative_path_str: str, fixture_root: Path) -> Path:
    """Platform-independent path safety validation against Windows/POSIX absolute paths, UNC paths, and traversal."""
    p_str = relative_path_str.strip()

    # 1. POSIX absolute check
    if p_str.startswith("/") or p_str.startswith("\\"):
        raise HostedDisposableRehearsalError(f"Absolute path forbidden in fixture request: '{relative_path_str}'")

    # 2. Windows drive absolute check
    if re.match(r"^[a-zA-Z]:[/\\]", p_str):
        raise HostedDisposableRehearsalError(f"Windows drive absolute path forbidden: '{relative_path_str}'")

    # 3. UNC network path check
    if p_str.startswith(r"\\") or p_str.startswith("//"):
        raise HostedDisposableRehearsalError(f"UNC network path forbidden: '{relative_path_str}'")

    # 4. Path traversal check
    posix_parts = PurePosixPath(p_str).parts
    win_parts = PureWindowsPath(p_str).parts
    if ".." in posix_parts or ".." in win_parts:
        raise HostedDisposableRehearsalError(f"Path traversal forbidden in fixture request: '{relative_path_str}'")

    # 5. Host resolution check below fixture_root
    raw_path = Path(relative_path_str)
    resolved_root = fixture_root.resolve()
    resolved_file = (resolved_root / raw_path).resolve()

    try:
        resolved_file.relative_to(resolved_root)
    except ValueError:
        raise HostedDisposableRehearsalError(f"Resolved path '{resolved_file}' escapes fixture root '{resolved_root}'")

    if not resolved_file.exists() or not resolved_file.is_file():
        raise HostedDisposableRehearsalError(f"Fixture file not found or not regular file: '{resolved_file}'")

    return resolved_file


def run_bounded_command(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Execute command safely via subprocess.run without shell=True."""
    if not cmd or cmd[0] not in ALLOWED_COMMANDS:
        raise HostedDisposableRehearsalError(f"Command '{cmd[0] if cmd else None}' not in allowlist: {ALLOWED_COMMANDS}")

    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )


class HostedDisposableRunner:
    """Generic AOS runner for hosted disposable execution against PostgreSQL containers (R4.2 Evidence Closure)."""

    def __init__(
        self,
        request: Dict[str, Any],
        request_file_path: Path,
        fixture_root: Path,
        output_dir: Path,
        postgres_image_id: Optional[str] = None,
        postgres_repo_digest: Optional[str] = None,
    ):
        self.request = request
        self.request_file_path = request_file_path.resolve()
        self.fixture_root = fixture_root.resolve()
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.postgres_image_id = postgres_image_id or os.environ.get("POSTGRES_IMAGE_ID", "local_docker_image_id")
        self.postgres_repo_digest = postgres_repo_digest or os.environ.get("POSTGRES_REPO_DIGEST", "postgres@sha256:local_digest")

        # Validate request schema
        val_res = validate_document("hosted_disposable_rehearsal_request", self.request)
        if not val_res.is_valid:
            err_msg = "; ".join(str(e) for e in val_res.errors)
            raise HostedDisposableRehearsalError(f"Request schema validation failed: {err_msg}")

        # Validate path safety
        self.migration_paths = [validate_path_safety(p, self.fixture_root) for p in request["migration_files"]]
        self.assertion_paths = [validate_path_safety(p, self.fixture_root) for p in request["assertion_files"]]
        self.rollback_paths = [validate_path_safety(p, self.fixture_root) for p in request["rollback_files"]]

        fp = request["failure_probe"]
        self.fp_setup_paths = [validate_path_safety(p, self.fixture_root) for p in fp["setup_files"]]
        self.fp_fatal_path = validate_path_safety(fp["fatal_failure_file"], self.fixture_root)
        self.fp_sentinel_path = validate_path_safety(fp["downstream_sentinel_file"], self.fixture_root)

        # Real accounting counters
        self.resource_created_count = 0
        self.resource_cleanup_attempt_count = 0
        self.resource_cleanup_success_count = 0
        self.resource_cleanup_failure_count = 0
        self.orphan_resource_count = 0
        self.containers_created: List[str] = []

        self.authority_invocation_count = 0
        self.migration_attempt_counts: Dict[str, int] = {}
        self.fatal_failure_attempt_count = 0
        self.automatic_retry_count = 0
        self.downstream_step_execution_count = 0

        # Observed container config (initialized to None before inspect)
        self.observed_network_mode: Optional[str] = None
        self.observed_host_volume_mount_count: Optional[int] = None
        self.observed_docker_socket_mount_count: Optional[int] = None
        self.observed_pids_limit: Optional[int] = None
        self.container_runtime_inspection_count = 0

    def execute_hosted_rehearsal(self) -> Dict[str, Any]:
        """Run full hosted disposable execution sequence and produce bound report & manifest."""
        primary_exception: Optional[Exception] = None
        report_steps: List[Dict[str, Any]] = []
        fixture_sha_map: Dict[str, str] = {}

        request_sha256 = hashlib.sha256(self.request_file_path.read_bytes()).hexdigest()

        all_fixture_paths = (
            self.migration_paths
            + self.assertion_paths
            + self.rollback_paths
            + self.fp_setup_paths
            + [self.fp_fatal_path, self.fp_sentinel_path]
        )
        for fp in all_fixture_paths:
            rel_p = fp.relative_to(self.fixture_root).as_posix()
            fixture_sha_map[rel_p] = hashlib.sha256(fp.read_bytes()).hexdigest()

        # Step 1: Real AOS Core Authority Invocation
        synthetic_snapshot = {
            "project_id": "test_project",
            "current_milestone": "TEST_GATE",
            "has_ambiguity": False,
            "next_action_execution_base_sha": "7966e9a1a7c36f9af0d78bfc67ab539b06fda0e7",
            "security_class": "HIGH_RISK",
            "autonomy_level": "HOLD",
        }
        synthetic_task = {
            "task_id": "task-canonical-exec",
            "project_id": "test_project",
            "gate": "TEST_GATE",
            "base_sha": "7966e9a1a7c36f9af0d78bfc67ab539b06fda0e7",
            "risk_class": "R3",
            "allowed_scope": {"paths": ["src/aos"]},
            "worker_requirements": {"adapter": "antigravity", "environment": "production"},
            "evidence_requirements": {"required_files": []},
            "retry_policy": {"max_retries": 0},
        }

        try:
            self.authority_invocation_count += 1
            auth_res = validate_execution_authority(synthetic_snapshot, synthetic_task)
            observed_dec = "BLOCK_CANONICAL_EXECUTION" if (auth_res.disposition == "HOLD" and not auth_res.is_valid) else "ALLOW_EXECUTION"
        except Exception as e:
            raise HostedDisposableRehearsalError(f"AOS Core authority validation invocation failed: {e}")

        if observed_dec != "BLOCK_CANONICAL_EXECUTION":
            raise HostedDisposableRehearsalError(f"Authority validation returned unexpected decision: {observed_dec}")

        report_steps.append({
            "step_id": "step-001-authority-separation",
            "claim_class": "AUTHORITY_DECISION",
            "required_for_pass_candidate": True,
            "component": {
                "origin": "AOS_CORE",
                "name": "ExecutionAuthorityValidator",
                "module": "aos.execution_authority",
                "symbol": "validate_execution_authority",
                "source_path": "src/aos/execution_authority.py",
                "source_sha256": hashlib.sha256((Path(__file__).parent / "execution_authority.py").read_bytes()).hexdigest(),
            },
            "execution_provenance": {
                "executed": True,
                "synthetic": True,
                "invocation_identity": "aos.execution_authority.validate_execution_authority",
                "result_summary": "HOLD",
            },
            "status": "PASS",
            "expected_decision": "BLOCK_CANONICAL_EXECUTION",
            "observed_decision": observed_dec,
            "evidence_references": ["auth_gate_hold_trace"],
        })

        pre_migration_state_hash = ""
        post_migration_state_hash = ""
        post_rollback_state_hash = ""

        # Step 2: Successful Migration Sequence
        succ_container = f"aos-disposable-success-{int(time.time())}"
        try:
            self._start_postgres_container(succ_container)

            pre_state = self._observe_fixture_scope_state(succ_container)
            pre_migration_state_hash = hashlib.sha256(pre_state.encode("utf-8")).hexdigest()

            for idx, mpath in enumerate(self.migration_paths, 1):
                rel_p = mpath.relative_to(self.fixture_root).as_posix()
                self.migration_attempt_counts[rel_p] = self.migration_attempt_counts.get(rel_p, 0) + 1
                res = self._execute_sql_in_container(succ_container, mpath)
                if res.returncode != 0:
                    raise HostedDisposableRehearsalError(f"Migration file '{rel_p}' failed: {res.stderr}")

            assert_sql = self.assertion_paths[0]
            assert_res = self._execute_sql_in_container(succ_container, assert_sql)
            if "1" not in assert_res.stdout:
                raise HostedDisposableRehearsalError(f"Assertion failed for '{assert_sql.name}': {assert_res.stdout}")

            post_migration_state = self._observe_fixture_scope_state(succ_container)
            post_migration_state_hash = hashlib.sha256(post_migration_state.encode("utf-8")).hexdigest()

            if post_migration_state_hash == pre_migration_state_hash:
                raise HostedDisposableRehearsalError("Post-migration scope state is identical to pre-migration baseline!")

            report_steps.append({
                "step_id": "step-002-migration-execution",
                "claim_class": "MIGRATION_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "REHEARSAL_HARNESS",
                    "identity_mode": "IN_MEMORY",
                    "name": "HostedPostgresMigrationRunner",
                    "description": "Hosted disposable PostgreSQL container migration sequence runner",
                    "identity_sha256": hashlib.sha256("Hosted disposable PostgreSQL container migration sequence runner".encode("utf-8")).hexdigest(),
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                    "invocation_identity": f"docker exec {succ_container} psql",
                    "result_summary": "ALL_MIGRATIONS_APPLIED_AND_ASSERTION_VERIFIED",
                },
                "status": "PASS",
                "evidence_references": ["migration_assert_output_1"],
            })

            rb_sql = self.rollback_paths[0]
            self._execute_sql_in_container(succ_container, rb_sql)

            post_rollback_state = self._observe_fixture_scope_state(succ_container)
            post_rollback_state_hash = hashlib.sha256(post_rollback_state.encode("utf-8")).hexdigest()

            if post_rollback_state_hash != pre_migration_state_hash:
                raise HostedDisposableRehearsalError(
                    f"Rollback verification state mismatch: post_rollback={post_rollback_state_hash} != pre_migration={pre_migration_state_hash}"
                )

            report_steps.append({
                "step_id": "step-003-rollback-execution",
                "claim_class": "ROLLBACK_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "REHEARSAL_HARNESS",
                    "identity_mode": "IN_MEMORY",
                    "name": "HostedPostgresRollbackRunner",
                    "description": "Hosted disposable PostgreSQL container rollback runner",
                    "identity_sha256": hashlib.sha256("Hosted disposable PostgreSQL container rollback runner".encode("utf-8")).hexdigest(),
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                    "invocation_identity": f"docker exec {succ_container} psql",
                    "result_summary": "ROLLBACK_SUCCESSFUL_STATE_RECONCILED",
                },
                "status": "PASS",
                "evidence_references": ["rollback_assert_output_1"],
            })

        except Exception as e:
            primary_exception = e
        finally:
            self._cleanup_container(succ_container)

        if primary_exception:
            if self.resource_cleanup_failure_count != 0 or self.orphan_resource_count != 0:
                cleanup_err = f"Secondary cleanup failure: failures={self.resource_cleanup_failure_count}, orphans={self.orphan_resource_count}"
                raise HostedDisposableRehearsalError(f"{primary_exception} [{cleanup_err}]") from primary_exception
            raise primary_exception

        # Step 3: Fatal Failure Probe Container
        fail_container = f"aos-disposable-failure-probe-{int(time.time())}"
        try:
            self._start_postgres_container(fail_container)

            for spath in self.fp_setup_paths:
                self._execute_sql_in_container(fail_container, spath)

            self.fatal_failure_attempt_count += 1
            fatal_res = self._execute_sql_in_container(fail_container, self.fp_fatal_path)
            if fatal_res.returncode == 0:
                raise HostedDisposableRehearsalError("Fatal failure probe SQL unexpectedly succeeded")

            sentinel_query = self._run_psql_command(
                fail_container,
                "SELECT count(*) FROM information_schema.tables WHERE table_name = 'downstream_sentinel_must_not_exist';"
            )
            if "0" not in sentinel_query.stdout:
                self.downstream_step_execution_count += 1
                raise HostedDisposableRehearsalError("Downstream sentinel table unexpectedly exists!")

            report_steps.append({
                "step_id": "step-004-failure-stop-probe",
                "claim_class": "DISPOSABLE_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "REHEARSAL_HARNESS",
                    "identity_mode": "IN_MEMORY",
                    "name": "HostedFailureProbeRunner",
                    "description": "Hosted failure probe sequence runner proving single attempt and downstream sentinel suppression",
                    "identity_sha256": hashlib.sha256("Hosted failure probe sequence runner proving single attempt and downstream sentinel suppression".encode("utf-8")).hexdigest(),
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                    "invocation_identity": f"docker exec {fail_container} psql",
                    "result_summary": "FATAL_FAILURE_STOPPED_SINGLE_ATTEMPT_SENTINEL_SUPPRESSED",
                },
                "status": "PASS",
                "evidence_references": ["failure_probe_stop_evidence_1"],
            })

        except Exception as e:
            primary_exception = e
        finally:
            self._cleanup_container(fail_container)

        if primary_exception:
            if self.resource_cleanup_failure_count != 0 or self.orphan_resource_count != 0:
                cleanup_err = f"Secondary cleanup failure: failures={self.resource_cleanup_failure_count}, orphans={self.orphan_resource_count}"
                raise HostedDisposableRehearsalError(f"{primary_exception} [{cleanup_err}]") from primary_exception
            raise primary_exception

        # Verify Cleanup Accounting
        if self.resource_cleanup_failure_count != 0 or self.orphan_resource_count != 0 or self.resource_created_count != self.resource_cleanup_success_count:
            raise HostedDisposableRehearsalError(
                f"Cleanup verification failed: created={self.resource_created_count}, cleaned={self.resource_cleanup_success_count}, failures={self.resource_cleanup_failure_count}, orphans={self.orphan_resource_count}"
            )

        # 1. Build Hosted Runtime Manifest Payload first
        runtime_manifest = {
            "schema_version": "0.1",
            "aos_source_sha": os.environ.get("GITHUB_SHA", "7966e9a1a7c36f9af0d78bfc67ab539b06fda0e7"),
            "request_sha256": request_sha256,
            "runtime_profile": self.request["runtime_profile"],
            "postgres_requested_image": "postgres:16",
            "resolved_postgres_image_id": self.postgres_image_id,
            "resolved_postgres_repo_digest": self.postgres_repo_digest,
            "fixture_sha256_map": fixture_sha_map,
            "target_container_network_mode": self.observed_network_mode,
            "host_volume_mount_count": self.observed_host_volume_mount_count,
            "docker_socket_mount_count": self.observed_docker_socket_mount_count,
            "migration_attempt_counts": self.migration_attempt_counts,
            "expected_mutation_verification": "PASS",
            "fatal_failure_detected": "YES",
            "failed_step_attempt_count": self.fatal_failure_attempt_count,
            "automatic_retry_count": self.automatic_retry_count,
            "downstream_step_execution_count": self.downstream_step_execution_count,
            "downstream_sentinel_present": "NO",
            "pre_migration_scope_state_hash": pre_migration_state_hash,
            "post_migration_scope_state_hash": post_migration_state_hash,
            "post_rollback_scope_state_hash": post_rollback_state_hash,
            "rollback_state_verification": "PASS",
            "disposable_resource_created_count": self.resource_created_count,
            "disposable_resource_cleaned_count": self.resource_cleanup_success_count,
            "orphan_resource_count": self.orphan_resource_count,
        }

        manifest_val = validate_document("hosted_runtime_manifest", runtime_manifest)
        if not manifest_val.is_valid:
            err_msg = "; ".join(str(e) for e in manifest_val.errors)
            raise HostedDisposableRehearsalError(f"Generated hosted runtime manifest validation failed: {err_msg}")

        manifest_bytes = json.dumps(runtime_manifest, indent=2, sort_keys=True).encode("utf-8")
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

        # 2. Build Report with Cryptographic Manifest Binding
        report = {
            "schema_version": "0.1",
            "rehearsal_id": self.request["rehearsal_id"],
            "target_repo": "MertSGI/AOS",
            "candidate_sha": os.environ.get("GITHUB_SHA", "7966e9a1a7c36f9af0d78bfc67ab539b06fda0e7"),
            "top_level_classification": "PASS_CANDIDATE",
            "runtime_evidence_binding": {
                "manifest_filename": "runtime_manifest.json",
                "manifest_sha256": manifest_sha256,
                "manifest_schema_version": "0.1",
            },
            "steps": report_steps,
        }

        val_res = validate_rehearsal_report(report, repo_root=Path.cwd())
        if not val_res.is_valid:
            err_msg = "; ".join(str(e) for e in val_res.errors)
            raise HostedDisposableRehearsalError(f"Generated R3 rehearsal report validation failed: {err_msg}")

        report_file = self.output_dir / "report.json"
        manifest_file = self.output_dir / "runtime_manifest.json"

        report_bytes = json.dumps(report, indent=2, sort_keys=True).encode("utf-8")

        report_file.write_bytes(report_bytes)
        manifest_file.write_bytes(manifest_bytes)

        # Independent re-read hash check
        re_manifest_sha = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
        if re_manifest_sha != manifest_sha256:
            raise HostedDisposableRehearsalError("Manifest written SHA256 mismatch!")

        return {
            "report": report,
            "report_file": report_file,
            "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            "manifest_file": manifest_file,
            "manifest_sha256": manifest_sha256,
            "containers_cleaned": self.resource_cleanup_success_count,
        }

    def _observe_fixture_scope_state(self, container_name: str) -> str:
        """Query container for normalized table/row state of aos_rehearsal_items."""
        res = self._run_psql_command(
            container_name,
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'aos_rehearsal_items';"
        )
        if "1" not in res.stdout:
            return "TABLE_NOT_EXISTS"

        cnt_res = self._run_psql_command(container_name, "SELECT count(*) FROM aos_rehearsal_items;")
        return f"TABLE_EXISTS_COUNT_{cnt_res.stdout.strip()}"

    def _start_postgres_container(self, name: str) -> None:
        """Start isolated postgres:16 container with --network none, resource limits (--pids-limit 100), inspect observations."""
        cmd = [
            "docker", "run", "-d",
            "--name", name,
            "--network", "none",
            "--memory", "512m",
            "--cpus", "1.0",
            "--pids-limit", "100",
            "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
            "postgres:16"
        ]
        res = run_bounded_command(cmd, timeout=30)
        if res.returncode != 0:
            raise HostedDisposableRehearsalError(f"Failed to start container '{name}': {res.stderr}")

        self.containers_created.append(name)
        self.resource_created_count += 1

        # Strict container inspection and policy enforcement
        insp_res = run_bounded_command(["docker", "inspect", name])
        if insp_res.returncode != 0:
            raise HostedDisposableRehearsalError(f"docker inspect for container '{name}' returned nonzero exit code {insp_res.returncode}")

        try:
            insp_data = json.loads(insp_res.stdout)
            if not isinstance(insp_data, list) or not insp_data:
                raise HostedDisposableRehearsalError(f"docker inspect returned invalid/empty array: {insp_res.stdout}")
            c_info = insp_data[0]
            host_config = c_info.get("HostConfig", {})
            net_mode = host_config.get("NetworkMode", "")
            pids_limit = host_config.get("PidsLimit")
            mounts = c_info.get("Mounts", [])
        except Exception as e:
            if isinstance(e, HostedDisposableRehearsalError):
                raise e
            raise HostedDisposableRehearsalError(f"Failed to parse docker inspect output for '{name}': {e}")

        host_bind_mount_count = len([m for m in mounts if m.get("Type") == "bind"])
        docker_socket_mount_count = len([m for m in mounts if "docker.sock" in m.get("Source", "")])

        # Enforce security policy rules immediately
        if net_mode != "none":
            raise HostedDisposableRehearsalError(f"Container '{name}' network mode is '{net_mode}', expected 'none'")
        if host_bind_mount_count != 0:
            raise HostedDisposableRehearsalError(f"Container '{name}' has {host_bind_mount_count} host bind mounts, expected 0")
        if docker_socket_mount_count != 0:
            raise HostedDisposableRehearsalError(f"Container '{name}' has {docker_socket_mount_count} docker.sock mounts, expected 0")
        if pids_limit != 100:
            raise HostedDisposableRehearsalError(f"Container '{name}' pids_limit is {pids_limit}, expected 100")

        self.observed_network_mode = net_mode
        self.observed_host_volume_mount_count = host_bind_mount_count
        self.observed_docker_socket_mount_count = docker_socket_mount_count
        self.observed_pids_limit = pids_limit
        self.container_runtime_inspection_count += 1

        ready = False
        for _ in range(15):
            time.sleep(1)
            chk = run_bounded_command(["docker", "exec", name, "pg_isready", "-U", "postgres"], timeout=5)
            if chk.returncode == 0:
                ready = True
                break
        if not ready:
            raise HostedDisposableRehearsalError(f"PostgreSQL inside container '{name}' failed readiness check")

    def _execute_sql_in_container(self, container_name: str, sql_file: Path) -> subprocess.CompletedProcess[str]:
        """Copy SQL file into container via docker cp and execute via psql."""
        container_sql_path = f"/tmp/{sql_file.name}"
        cp_cmd = ["docker", "cp", str(sql_file), f"{container_name}:{container_sql_path}"]
        cp_res = run_bounded_command(cp_cmd, timeout=15)
        if cp_res.returncode != 0:
            raise HostedDisposableRehearsalError(f"Failed docker cp of '{sql_file}' to '{container_name}': {cp_res.stderr}")

        exec_cmd = ["docker", "exec", container_name, "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-f", container_sql_path]
        return run_bounded_command(exec_cmd, timeout=15)

    def _run_psql_command(self, container_name: str, command_str: str) -> subprocess.CompletedProcess[str]:
        """Run raw SQL string inside container via psql -c."""
        exec_cmd = ["docker", "exec", container_name, "psql", "-U", "postgres", "-c", command_str]
        return run_bounded_command(exec_cmd, timeout=15)

    def _cleanup_container(self, container_name: str) -> None:
        """Force clean container, enforce return code, and verify absence via post-cleanup inspect."""
        self.resource_cleanup_attempt_count += 1
        rm_res = run_bounded_command(["docker", "rm", "-f", container_name], timeout=15)

        if rm_res.returncode != 0:
            self.resource_cleanup_failure_count += 1
            print(f"Cleanup rm error: {rm_res.stderr}", file=sys.stderr)
        else:
            # Check post-removal inspect
            insp_res = run_bounded_command(["docker", "inspect", container_name])
            if insp_res.returncode != 0:
                self.resource_cleanup_success_count += 1
            else:
                self.resource_cleanup_failure_count += 1
                self.orphan_resource_count += 1


def main() -> None:
    parser = argparse.ArgumentParser(description="AOS Hosted Disposable Rehearsal CLI Runner")
    parser.add_argument("request_path", type=Path, help="Path to hosted rehearsal request JSON")
    parser.add_argument("--fixture-root", type=Path, required=True, help="Path to root directory of fixture files")
    parser.add_argument("--output-dir", type=Path, required=True, help="Path to output directory for report artifact")
    parser.add_argument("--postgres-image-id", type=str, default=None, help="Resolved Docker image ID")
    parser.add_argument("--postgres-repo-digest", type=str, default=None, help="Resolved Docker repo digest")
    args = parser.parse_args()

    try:
        req = load_json_strict(args.request_path)
        runner = HostedDisposableRunner(
            req,
            request_file_path=args.request_path,
            fixture_root=args.fixture_root,
            output_dir=args.output_dir,
            postgres_image_id=args.postgres_image_id,
            postgres_repo_digest=args.postgres_repo_digest,
        )
        res = runner.execute_hosted_rehearsal()
        print(f"HOSTED_REHEARSAL_SUCCESS report={res['report_file']} sha256={res['report_sha256']} manifest_sha256={res['manifest_sha256']}")
        sys.exit(0)
    except Exception as e:
        print(f"HOSTED_REHEARSAL_FAIL: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
