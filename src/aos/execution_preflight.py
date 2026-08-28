"""Canonical deterministic pre-engine execution gate and preflight controller wrapper for AOS."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from aos.validate import DuplicateJSONKeyError, load_json_strict, validate_document
from aos.workers.antigravity import (
    ADAPTER_CONTRACT_VERSION,
    RUNTIME_ENVIRONMENT_PROFILE_VERSION,
    get_local_capability_store_path,
    resolve_capability_status,
)

ALLOWED_ENGINE_DISPOSITIONS = {
    "VERIFIED_CANDIDATE",
    "VERIFICATION_FAILED",
    "WORKER_FAILED",
    "HOLD",
}


@dataclass
class PreEngineExecutionRequest:
    """Request contract for pre-engine execution validation."""

    local_target_repo_path: Path
    expected_control_branch: str
    authorization_artifact_path: Path
    canonical_state_path: Path
    attempt_number: int
    cli_command: Union[str, Path]
    raw_result_path: Union[str, Path]

    def __post_init__(self) -> None:
        self.local_target_repo_path = Path(self.local_target_repo_path).resolve()
        # Preserve absolute path identity without early symlink resolution for authorization and state
        self.authorization_artifact_path = Path(self.authorization_artifact_path).absolute()
        self.canonical_state_path = Path(self.canonical_state_path).absolute()
        self.raw_result_path = Path(self.raw_result_path).absolute()
        if isinstance(self.cli_command, Path):
            self.cli_command = str(self.cli_command)


class PreEngineCheck:
    """Individual pre-engine check status record."""

    def __init__(self, check_id: str, status: str = "NOT_RUN", message: Optional[str] = None):
        self.check_id = check_id
        self.status = status  # "PASS", "FAIL", "NOT_RUN"
        self.message = message

    def to_dict(self) -> Dict[str, Any]:
        res: Dict[str, Any] = {
            "check_id": self.check_id,
            "status": self.status,
        }
        if self.message is not None:
            res["message"] = self.message
        return res


class PreEngineExecutionResult:
    """Deterministic evaluation result from PreEngineExecutionGate."""

    def __init__(self, status: str, engine_may_execute: bool, checks: List[PreEngineCheck], errors: Optional[List[str]] = None):
        self.status = status  # "PASS" | "HOLD"
        self.engine_may_execute = engine_may_execute
        self.checks = checks
        self.errors = errors or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "engine_may_execute": self.engine_may_execute,
            "checks": [c.to_dict() for c in self.checks],
            "errors": self.errors,
        }


def _default_git_runner(cmd: List[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Deterministic read-only subprocess git runner."""
    return subprocess.run(
        ["git"] + cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )


def _default_cli_version_resolver(cli_path: Path) -> Optional[str]:
    """Default version resolver querying reported CLI version. Do not call without exact hash match."""
    try:
        res = subprocess.run([str(cli_path), "--version"], capture_output=True, text=True, timeout=10, shell=False)
        if res.returncode == 0:
            return (res.stdout or "").strip()
        return None
    except Exception:
        return None


def _default_capability_resolver(cli_path: Path, attestation: Dict[str, Any], identity: Dict[str, Any]) -> str:
    """Default capability resolver reusing precomputed executable identity."""
    store_path = Path(attestation.get("store_path", get_local_capability_store_path("antigravity")))
    return resolve_capability_status(cli_command=str(cli_path), store_path=store_path, identity=identity)


def _is_path_indirection(path: Path) -> bool:
    """Check if path is a symlink, Windows junction, or Windows reparse point. Fail closed on error."""
    try:
        if path.is_symlink():
            return True
        if hasattr(path, "is_junction") and callable(path.is_junction) and path.is_junction():
            return True

        lstat_res = os.lstat(path)
        attrs = getattr(lstat_res, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if reparse_flag and (attrs & reparse_flag):
            return True
        return False
    except Exception:
        return True


def _is_canonical_file_safe(file_path: Path, repo_path: Path) -> Tuple[bool, str]:
    """Verify file exists, is not an indirection, contains no indirection components up to repo_path, and is contained in repo_path."""
    try:
        if not file_path.is_file():
            return False, f"File not found or not regular file: {file_path}"
        if _is_path_indirection(file_path):
            return False, f"File is a symlink, junction, or reparse point: {file_path}"

        curr = file_path.parent
        repo_resolved = repo_path.resolve()
        while True:
            if _is_path_indirection(curr):
                return False, f"Path component '{curr}' is a symlink, junction, or reparse point"
            if curr.resolve() == repo_resolved:
                break
            if curr.parent == curr:
                return False, f"Path '{file_path}' is outside repository '{repo_path}'"
            curr = curr.parent

        if not file_path.resolve().is_relative_to(repo_resolved):
            return False, f"Resolved path '{file_path.resolve()}' outside repo '{repo_resolved}'"

        return True, ""
    except Exception as e:
        return False, f"Path safety inspection error: {e}"


class PreEngineExecutionGate:
    """Canonical deterministic pre-engine execution gate."""

    ORDERED_CHECK_IDS = [
        "request_contract",
        "authorization_schema",
        "authorization_semantics",
        "canonical_state_freshness",
        "source_branch",
        "source_head_parent",
        "source_worktree_clean",
        "source_remote_freshness",
        "authorization_carrier_presence",
        "canonical_state_carrier_presence",
        "raw_result_collision",
        "capability_attestation_schema",
        "cli_path",
        "cli_hash",
        "cli_version",
        "worker_capability",
    ]

    def __init__(
        self,
        cli_version_resolver: Optional[Callable[[Path], Optional[str]]] = None,
        capability_resolver: Optional[Callable[[Path, Dict[str, Any], Dict[str, Any]], str]] = None,
        git_runner: Optional[Callable[[List[str], Path], subprocess.CompletedProcess[str]]] = None,
        capability_store_path_resolver: Optional[Callable[[], Path]] = None,
    ):
        self.cli_version_resolver = cli_version_resolver or _default_cli_version_resolver
        self.capability_resolver = capability_resolver or _default_capability_resolver
        self.git_runner = git_runner or _default_git_runner
        self.capability_store_path_resolver = capability_store_path_resolver or (
            lambda: get_local_capability_store_path("antigravity")
        )

    def evaluate(self, request: PreEngineExecutionRequest) -> PreEngineExecutionResult:
        checks_map: Dict[str, PreEngineCheck] = {
            cid: PreEngineCheck(check_id=cid, status="NOT_RUN") for cid in self.ORDERED_CHECK_IDS
        }
        errors: List[str] = []

        parsed_auth: Dict[str, Any] = {}
        parsed_state: Dict[str, Any] = {}
        parsed_attestation: Dict[str, Any] = {}
        resolved_cli_file: Optional[Path] = None

        def _fail(check_id: str, message: str) -> PreEngineExecutionResult:
            checks_map[check_id].status = "FAIL"
            checks_map[check_id].message = message
            errors.append(f"Check '{check_id}' failed: {message}")
            ordered_checks = [checks_map[cid] for cid in self.ORDERED_CHECK_IDS]
            return PreEngineExecutionResult(status="HOLD", engine_may_execute=False, checks=ordered_checks, errors=errors)

        def _pass(check_id: str, message: Optional[str] = None) -> None:
            checks_map[check_id].status = "PASS"
            if message:
                checks_map[check_id].message = message

        # 1. request_contract (Indirection & path containment before resolve)
        try:
            if not request.local_target_repo_path.is_dir():
                return _fail("request_contract", f"Local target repo path not found: {request.local_target_repo_path}")

            auth_safe, auth_msg = _is_canonical_file_safe(request.authorization_artifact_path, request.local_target_repo_path)
            if not auth_safe:
                return _fail("request_contract", f"Authorization artifact unsafe: {auth_msg}")

            state_safe, state_msg = _is_canonical_file_safe(request.canonical_state_path, request.local_target_repo_path)
            if not state_safe:
                return _fail("request_contract", f"Canonical state file unsafe: {state_msg}")

            if request.attempt_number <= 0:
                return _fail("request_contract", f"Invalid attempt number: {request.attempt_number}")
            if not request.expected_control_branch:
                return _fail("request_contract", "Missing expected control branch")
            if not request.cli_command:
                return _fail("request_contract", "Missing CLI command")
            _pass("request_contract")
        except Exception as e:
            return _fail("request_contract", f"Unexpected request contract evaluation error: {e}")

        # 2. authorization_schema
        try:
            parsed_auth = load_json_strict(request.authorization_artifact_path)
            val_auth = validate_document("execution_authorization", parsed_auth)
            if not val_auth.is_valid:
                err_msg = "; ".join(e.message for e in val_auth.errors)
                return _fail("authorization_schema", f"Authorization artifact schema invalid: {err_msg}")
            _pass("authorization_schema")
        except DuplicateJSONKeyError as e:
            return _fail("authorization_schema", f"Authorization artifact duplicate JSON key error: {e}")
        except json.JSONDecodeError as jde:
            return _fail("authorization_schema", f"Authorization artifact JSON decode error: {jde}")
        except Exception as e:
            return _fail("authorization_schema", f"Error reading authorization artifact: {e}")

        # 3. authorization_semantics
        try:
            decision = parsed_auth.get("decision")
            authority_source = parsed_auth.get("authority_source")
            control_src_sha = parsed_auth.get("control_source_sha")
            exec_base_sha = parsed_auth.get("execution_base_sha")

            if decision != "AUTO_EXECUTE":
                return _fail("authorization_semantics", f"Authorization decision is '{decision}', required 'AUTO_EXECUTE'")
            if authority_source not in ("POLICY_AUTONOMOUS", "HUMAN_EXPLICIT"):
                return _fail("authorization_semantics", f"Invalid authority_source '{authority_source}'")
            if not control_src_sha or not isinstance(control_src_sha, str) or len(control_src_sha) != 40:
                return _fail("authorization_semantics", f"Invalid control_source_sha: {control_src_sha}")
            if not exec_base_sha or not isinstance(exec_base_sha, str) or len(exec_base_sha) != 40:
                return _fail("authorization_semantics", f"Invalid execution_base_sha: {exec_base_sha}")
            _pass("authorization_semantics")
        except Exception as e:
            return _fail("authorization_semantics", f"Authorization semantics evaluation error: {e}")

        # 4. canonical_state_freshness (State schema validation + freshness semantics)
        try:
            parsed_state = load_json_strict(request.canonical_state_path)

            val_state = validate_document("state", parsed_state)
            if not val_state.is_valid:
                err_msg = "; ".join(e.message for e in val_state.errors)
                return _fail("canonical_state_freshness", f"Canonical state schema invalid: {err_msg}")

            ext = parsed_state.get("extensions", {}).get("aos4_independent_verification", {})

            next_att = ext.get("next_execution_attempt_number")
            next_auth_stat = ext.get("next_execution_authorization_status")

            if next_att != request.attempt_number:
                return _fail(
                    "canonical_state_freshness",
                    f"State next attempt number ({next_att}) != requested ({request.attempt_number})",
                )
            if next_auth_stat not in ("POLICY_AUTHORIZED", "EXPLICIT_HUMAN_REAUTHORIZATION"):
                return _fail(
                    "canonical_state_freshness",
                    f"State next authorization status '{next_auth_stat}' is not an explicitly authorized live state",
                )

            N = request.attempt_number
            st_auth_id = ext.get(f"attempt_{N}_authorization_id")
            st_auth_status = ext.get(f"attempt_{N}_authorization_status")
            st_auth_consumed = ext.get(f"attempt_{N}_authorization_consumed")
            st_ctrl_sha = ext.get(f"attempt_{N}_authorization_control_source_sha")
            st_exec_base = ext.get(f"attempt_{N}_execution_base_sha")
            st_exec_actual = ext.get(f"attempt_{N}_execution_actual", 0)
            st_worker_actual = ext.get(f"attempt_{N}_worker_execution_actual", 0)
            st_retry_actual = ext.get(f"attempt_{N}_retry_actual", 0)
            st_terminal = ext.get(f"attempt_{N}_terminal", False)

            if st_auth_id != parsed_auth.get("authorization_id"):
                return _fail("canonical_state_freshness", f"State authorization ID '{st_auth_id}' != artifact ID '{parsed_auth.get('authorization_id')}'")
            if st_auth_status != "ISSUED_NOT_CONSUMED":
                return _fail("canonical_state_freshness", f"State authorization status is '{st_auth_status}', expected 'ISSUED_NOT_CONSUMED'")
            if st_auth_consumed is not False:
                return _fail("canonical_state_freshness", f"State authorization consumed is {st_auth_consumed}, expected False")
            if st_ctrl_sha != parsed_auth.get("control_source_sha"):
                return _fail("canonical_state_freshness", f"State control source SHA '{st_ctrl_sha}' != artifact '{parsed_auth.get('control_source_sha')}'")
            if st_exec_base != parsed_auth.get("execution_base_sha"):
                return _fail("canonical_state_freshness", f"State execution base SHA '{st_exec_base}' != artifact '{parsed_auth.get('execution_base_sha')}'")
            if st_exec_actual != 0:
                return _fail("canonical_state_freshness", f"State attempt_{N}_execution_actual ({st_exec_actual}) != 0")
            if st_worker_actual != 0:
                return _fail("canonical_state_freshness", f"State attempt_{N}_worker_execution_actual ({st_worker_actual}) != 0")
            if st_retry_actual != 0:
                return _fail("canonical_state_freshness", f"State attempt_{N}_retry_actual ({st_retry_actual}) != 0")
            if st_terminal is not False:
                return _fail("canonical_state_freshness", f"State attempt_{N}_terminal is {st_terminal}, expected False")

            _pass("canonical_state_freshness")
        except Exception as e:
            return _fail("canonical_state_freshness", f"Canonical state freshness check error: {e}")

        # 5. source_branch
        try:
            res_branch = self.git_runner(["branch", "--show-current"], cwd=request.local_target_repo_path)
            if res_branch.returncode != 0:
                return _fail("source_branch", f"Git branch check failed: {res_branch.stderr}")
            curr_branch = (res_branch.stdout or "").strip()
            if curr_branch != request.expected_control_branch:
                return _fail("source_branch", f"Current branch '{curr_branch}' != expected '{request.expected_control_branch}'")
            _pass("source_branch")
        except Exception as e:
            return _fail("source_branch", f"Git branch inspection error: {e}")

        # 6. source_head_parent
        head_sha = ""
        head_parent_sha = ""
        try:
            res_head = self.git_runner(["rev-parse", "HEAD"], cwd=request.local_target_repo_path)
            if res_head.returncode != 0:
                return _fail("source_head_parent", f"Git rev-parse HEAD failed: {res_head.stderr}")
            head_sha = (res_head.stdout or "").strip()

            res_parent = self.git_runner(["rev-parse", "HEAD^"], cwd=request.local_target_repo_path)
            if res_parent.returncode != 0:
                return _fail("source_head_parent", f"Git rev-parse HEAD^ failed: {res_parent.stderr}")
            head_parent_sha = (res_parent.stdout or "").strip()

            if head_parent_sha != parsed_auth.get("control_source_sha"):
                return _fail(
                    "source_head_parent",
                    f"HEAD parent ({head_parent_sha}) != authorization control_source_sha ({parsed_auth.get('control_source_sha')})",
                )
            _pass("source_head_parent")
        except Exception as e:
            return _fail("source_head_parent", f"Git HEAD/parent inspection error: {e}")

        # 7. source_worktree_clean
        try:
            res_status = self.git_runner(["status", "--porcelain"], cwd=request.local_target_repo_path)
            if res_status.returncode != 0:
                return _fail("source_worktree_clean", f"Git status check failed: {res_status.stderr}")
            if (res_status.stdout or "").strip():
                return _fail("source_worktree_clean", "Working tree is dirty")
            _pass("source_worktree_clean")
        except Exception as e:
            return _fail("source_worktree_clean", f"Git worktree status error: {e}")

        # 8. source_remote_freshness
        try:
            res_remote = self.git_runner(
                ["ls-remote", "origin", f"refs/heads/{request.expected_control_branch}"],
                cwd=request.local_target_repo_path,
            )
            if res_remote.returncode != 0 or not res_remote.stdout.strip():
                return _fail("source_remote_freshness", f"Git ls-remote failed or empty: {res_remote.stderr}")
            remote_sha = res_remote.stdout.strip().split()[0]
            if remote_sha != head_sha:
                return _fail("source_remote_freshness", f"Remote branch SHA ({remote_sha}) != local HEAD ({head_sha})")
            _pass("source_remote_freshness")
        except Exception as e:
            return _fail("source_remote_freshness", f"Git remote freshness error: {e}")

        # 9. authorization_carrier_presence
        try:
            rel_auth_path = os.path.relpath(request.authorization_artifact_path, request.local_target_repo_path).replace("\\", "/")

            res_carrier_tree = self.git_runner(["ls-tree", "--name-only", "HEAD", "--", rel_auth_path], cwd=request.local_target_repo_path)
            if res_carrier_tree.returncode != 0:
                return _fail("authorization_carrier_presence", f"Git ls-tree HEAD command failed for auth artifact: {res_carrier_tree.stderr}")
            carrier_files = [line.strip() for line in (res_carrier_tree.stdout or "").splitlines() if line.strip()]
            if rel_auth_path not in carrier_files:
                return _fail("authorization_carrier_presence", f"Authorization artifact '{rel_auth_path}' absent from carrier HEAD")

            res_parent_tree = self.git_runner(["ls-tree", "--name-only", head_parent_sha, "--", rel_auth_path], cwd=request.local_target_repo_path)
            if res_parent_tree.returncode != 0:
                return _fail("authorization_carrier_presence", f"Git ls-tree parent command failed for auth artifact: {res_parent_tree.stderr}")
            parent_files = [line.strip() for line in (res_parent_tree.stdout or "").splitlines() if line.strip()]
            if rel_auth_path in parent_files:
                return _fail("authorization_carrier_presence", f"Authorization artifact '{rel_auth_path}' already exists in parent {head_parent_sha}")

            _pass("authorization_carrier_presence")
        except Exception as e:
            return _fail("authorization_carrier_presence", f"Authorization carrier presence check error: {e}")

        # 10. canonical_state_carrier_presence
        try:
            rel_state_path = os.path.relpath(request.canonical_state_path, request.local_target_repo_path).replace("\\", "/")
            res_state_tree = self.git_runner(["ls-tree", "--name-only", "HEAD", "--", rel_state_path], cwd=request.local_target_repo_path)
            if res_state_tree.returncode != 0:
                return _fail("canonical_state_carrier_presence", f"Git ls-tree HEAD command failed for state file: {res_state_tree.stderr}")
            state_files = [line.strip() for line in (res_state_tree.stdout or "").splitlines() if line.strip()]
            if rel_state_path not in state_files:
                return _fail("canonical_state_carrier_presence", f"Canonical state file '{rel_state_path}' absent from HEAD membership")
            _pass("canonical_state_carrier_presence")
        except Exception as e:
            return _fail("canonical_state_carrier_presence", f"Canonical state carrier presence check error: {e}")

        # 11. raw_result_collision
        try:
            if request.raw_result_path.exists():
                return _fail("raw_result_collision", f"Raw result target path already exists: {request.raw_result_path}")
            _pass("raw_result_collision")
        except Exception as e:
            return _fail("raw_result_collision", f"Raw result collision check error: {e}")

        # 12. capability_attestation_schema (Trusted capability store path & indirection safety)
        try:
            store_path = Path(self.capability_store_path_resolver())
            if not store_path.is_absolute():
                return _fail("capability_attestation_schema", f"Capability store path is not absolute: {store_path}")
            if not store_path.is_file():
                return _fail("capability_attestation_schema", f"Capability store file not found or not regular file: {store_path}")
            if _is_path_indirection(store_path):
                return _fail("capability_attestation_schema", f"Capability store path is a symlink, junction, or reparse point: {store_path}")

            parsed_attestation = load_json_strict(store_path)

            val_att = validate_document("worker_capability_attestation", parsed_attestation)
            if not val_att.is_valid:
                err_msg = "; ".join(e.message for e in val_att.errors)
                return _fail("capability_attestation_schema", f"Capability attestation schema invalid: {err_msg}")

            if parsed_attestation.get("worker_adapter") != "antigravity":
                return _fail("capability_attestation_schema", f"Invalid worker_adapter '{parsed_attestation.get('worker_adapter')}'")
            if parsed_attestation.get("capability_status") != "PROVEN":
                return _fail("capability_attestation_schema", f"Attestation capability_status is '{parsed_attestation.get('capability_status')}', required 'PROVEN'")
            if parsed_attestation.get("adapter_contract_version") != ADAPTER_CONTRACT_VERSION:
                return _fail("capability_attestation_schema", f"Adapter contract version '{parsed_attestation.get('adapter_contract_version')}' != '{ADAPTER_CONTRACT_VERSION}'")
            if parsed_attestation.get("runtime_environment_profile_version") != RUNTIME_ENVIRONMENT_PROFILE_VERSION:
                return _fail("capability_attestation_schema", f"Profile version '{parsed_attestation.get('runtime_environment_profile_version')}' != '{RUNTIME_ENVIRONMENT_PROFILE_VERSION}'")

            parsed_attestation["store_path"] = store_path
            _pass("capability_attestation_schema")
        except DuplicateJSONKeyError as e:
            return _fail("capability_attestation_schema", f"Capability attestation duplicate JSON key error: {e}")
        except json.JSONDecodeError as jde:
            return _fail("capability_attestation_schema", f"Capability attestation JSON decode error: {jde}")
        except Exception as e:
            return _fail("capability_attestation_schema", f"Capability attestation check error: {e}")

        # 13. cli_path (Absolute pinned CLI path, no indirection)
        try:
            cli_path_obj = Path(request.cli_command)
            if not cli_path_obj.is_absolute():
                return _fail("cli_path", f"CLI path is not absolute: {request.cli_command}")
            if not cli_path_obj.is_file():
                return _fail("cli_path", f"CLI path is not a regular file: {request.cli_command}")
            if _is_path_indirection(cli_path_obj):
                return _fail("cli_path", f"CLI path is a symlink, junction, or reparse point: {request.cli_command}")

            resolved_cli_file = cli_path_obj.resolve()
            expected_filename = parsed_attestation.get("executable_filename")
            if expected_filename and resolved_cli_file.name != expected_filename:
                return _fail("cli_path", f"CLI basename '{resolved_cli_file.name}' != expected '{expected_filename}'")
            _pass("cli_path")
        except Exception as e:
            return _fail("cli_path", f"CLI path validation error: {e}")

        # 14. cli_hash (CRITICAL ORDERING: HASH BEFORE VERSION!)
        actual_sha256 = ""
        try:
            h = hashlib.sha256()
            with open(resolved_cli_file, "rb") as f:
                while chunk := f.read(65536):
                    h.update(chunk)
            actual_sha256 = h.hexdigest()

            expected_sha256 = parsed_attestation.get("executable_sha256")
            if actual_sha256 != expected_sha256:
                # CRITICAL: FAIL IMMEDIATELY. DO NOT CALL cli_version_resolver!
                return _fail("cli_hash", f"CLI hash ({actual_sha256}) != expected attestation hash ({expected_sha256})")
            _pass("cli_hash")
        except Exception as e:
            return _fail("cli_hash", f"CLI hash calculation error: {e}")

        # 15. cli_version (ONLY AFTER HASH MATCH!)
        reported_version = None
        try:
            reported_version = self.cli_version_resolver(resolved_cli_file)
            expected_version = parsed_attestation.get("reported_cli_version")
            if not reported_version or reported_version != expected_version:
                return _fail("cli_version", f"CLI reported version ({reported_version}) != expected ({expected_version})")
            _pass("cli_version")
        except Exception as e:
            return _fail("cli_version", f"CLI version inspection error: {e}")

        # 16. worker_capability (ONLY AFTER VERSION MATCH! Reuse precomputed identity)
        try:
            precomputed_identity = {
                "path": str(resolved_cli_file),
                "filename": resolved_cli_file.name,
                "sha256": actual_sha256,
                "version": reported_version,
            }
            cap_status = self.capability_resolver(resolved_cli_file, parsed_attestation, precomputed_identity)
            if cap_status != "PROVEN":
                return _fail("worker_capability", f"Resolved capability status is '{cap_status}', required 'PROVEN'")
            _pass("worker_capability")
        except Exception as e:
            return _fail("worker_capability", f"Worker capability resolution error: {e}")

        ordered_checks = [checks_map[cid] for cid in self.ORDERED_CHECK_IDS]
        return PreEngineExecutionResult(status="PASS", engine_may_execute=True, checks=ordered_checks, errors=[])


class PreflightControlledExecutionController:
    """Canonical wrapper combining PreEngineExecutionGate with ControlledExecutionEngine."""

    def __init__(
        self,
        gate: PreEngineExecutionGate,
        engine: Any,
    ):
        self.gate = gate
        self.engine = engine
        self._attempted = False

    def execute(self, request: PreEngineExecutionRequest) -> Dict[str, Any]:
        # TRUE ONE-SHOT CONTROLLER: set attempted BEFORE preflight on first call
        if self._attempted:
            return {
                "status": "HOLD",
                "preflight_status": "HOLD",
                "engine_invoked": False,
                "errors": ["One-shot controller already attempted"],
                "disposition": "HOLD",
            }

        self._attempted = True

        preflight_res = self.gate.evaluate(request)
        if preflight_res.status != "PASS" or not preflight_res.engine_may_execute:
            return {
                "status": "HOLD",
                "preflight_status": "HOLD",
                "engine_invoked": False,
                "preflight_result": preflight_res.to_dict(),
                "errors": preflight_res.errors,
                "disposition": "HOLD",
            }

        try:
            engine_res = self.engine.execute(
                local_target_repo_path=str(request.local_target_repo_path)
            )
        except Exception as e:
            return {
                "status": "HOLD",
                "preflight_status": "PASS",
                "engine_invoked": True,
                "disposition": "HOLD",
                "preflight_result": preflight_res.to_dict(),
                "errors": [f"Engine raised exception: {e}"],
            }

        # MALFORMED ENGINE RESULT MUST HOLD (No PASS fallback)
        if not isinstance(engine_res, dict):
            return {
                "status": "HOLD",
                "preflight_status": "PASS",
                "engine_invoked": True,
                "disposition": "HOLD",
                "preflight_result": preflight_res.to_dict(),
                "engine_result": str(engine_res),
                "errors": ["INVALID_ENGINE_TERMINAL_RESULT: Engine result is not a dictionary"],
            }

        disp = engine_res.get("disposition")
        if not disp or disp not in ALLOWED_ENGINE_DISPOSITIONS:
            return {
                "status": "HOLD",
                "preflight_status": "PASS",
                "engine_invoked": True,
                "disposition": "HOLD",
                "preflight_result": preflight_res.to_dict(),
                "engine_result": engine_res,
                "errors": [f"INVALID_ENGINE_TERMINAL_RESULT: Missing or invalid terminal disposition '{disp}'"],
            }

        # REAL VALID ENGINE RESULT PRESERVATION
        return {
            "status": disp,
            "preflight_status": "PASS",
            "engine_invoked": True,
            "disposition": disp,
            "preflight_result": preflight_res.to_dict(),
            "engine_result": engine_res,
        }


class CanonicalLiveExecutionController:
    """Canonical production-facing live execution boundary for AOS-4."""

    def __init__(self, engine: Any):
        gate = PreEngineExecutionGate()  # Production defaults enforced!
        self._controller = PreflightControlledExecutionController(gate=gate, engine=engine)

    def execute(self, request: PreEngineExecutionRequest) -> Dict[str, Any]:
        return self._controller.execute(request)
