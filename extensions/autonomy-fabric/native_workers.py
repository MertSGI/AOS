"""AOS Native Workers (V2).

Implements bounded native workers:
- NativeFileWorker: hash, read, write, patch, atomic replacement, allowlist confinement, rollback.
- NativeProcessWorker: explicit argv execution, cwd confinement, timeout, secret redaction, tree termination.
- NativeGitWorker: safe git operations (fetch, status, diff, rev-parse, branch, worktree, add, commit, fast-forward push). Prohibits force push, destructive reset.
- GitHubCIWorker: read canonical refs, workflow runs, check suites, text logs, classify status.
- BrowserExecutionBackend: connects to Playwright/browser screenshot adapter for DOM and visual validation.
- ModelReasoningBackend: integrates ProviderRegistry / Model Fabric to reason and propose bounded patches.
- AntigravityExecutionBackend: wraps Antigravity CLI as an optional specialist backend with quota/degradation detection.
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import shutil
import hashlib
import tempfile
import difflib
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from aos.process_utils import run_headless

from extensions.autonomy_fabric.execution_backend import (
    ExecutionBackend,
    ExecutionRequest,
    ExecutionResult,
    ExecutionCapability,
    ExecutionTrustZone,
    ExecutionHealth,
    ExecutionCost,
    EvidenceClass,
)


SECRET_PATTERNS = [
    re.compile(r'(?i)(?:api_key|token|secret|password|bearer|authorization)\s*[:=]\s*["\']?([a-zA-Z0-9_\-\.]{8,})["\']?'),
    re.compile(r'(?i)key=([a-zA-Z0-9_\-\.]{8,})'),
    re.compile(r'(ghp_[a-zA-Z0-9]{30,40})'),
    re.compile(r'(github_pat_[a-zA-Z0-9_]{60,100})'),
    re.compile(r'(AIza[0-9A-Za-z-_]{30,45})'),
    re.compile(r'(gsk_[a-zA-Z0-9]{40,60})'),
]


def redact_secrets(text: str) -> str:
    """Redacts known sensitive credential tokens from output strings."""
    if not text:
        return ""
    redacted = text
    for pat in SECRET_PATTERNS:
        redacted = pat.sub("[REDACTED_SECRET]", redacted)
    return redacted


def compute_file_sha256(file_path: str) -> str:
    """Computes full 64-char SHA256 of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def is_path_confined(target_path: str, workspace_root: str) -> bool:
    """Ensures target_path resolves strictly within workspace_root."""
    try:
        resolved_ws = Path(workspace_root).resolve()
        resolved_target = Path(target_path).resolve()
        return resolved_target == resolved_ws or resolved_ws in resolved_target.parents
    except Exception:
        return False


class NativeFileWorker(ExecutionBackend):
    """Native file worker for deterministic, bounded filesystem modifications."""

    backend_id = "native_file_worker"
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    supported_capabilities = {
        ExecutionCapability.FILE_READ,
        ExecutionCapability.FILE_WRITE,
        ExecutionCapability.PATCH_APPLY,
    }
    cost = ExecutionCost.FREE_LOCAL

    def get_health(self) -> ExecutionHealth:
        return ExecutionHealth.HEALTHY

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        start_time = time.time()
        errors: List[str] = []
        changed_paths: List[str] = []
        artifact_hashes: Dict[str, str] = {}
        backups: Dict[str, Optional[bytes]] = {}

        action = request.payload.get("action", "apply_patch")
        workspace = request.workspace

        if not os.path.isdir(workspace):
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="local_file_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=workspace,
                sanitized_errors=[f"Workspace directory {workspace} does not exist"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        try:
            if action == "write_file":
                rel_path = request.payload.get("path")
                content = request.payload.get("content", "")
                precondition_sha = request.payload.get("precondition_sha")

                if not rel_path:
                    raise ValueError("Missing 'path' in write_file payload")

                full_path = os.path.normpath(os.path.join(workspace, rel_path))
                if not is_path_confined(full_path, workspace):
                    raise PermissionError(f"Path {rel_path} escapes workspace boundary")

                # Scope check
                if request.write_scope and not any(rel_path.startswith(prefix) for prefix in request.write_scope):
                    raise PermissionError(f"Path {rel_path} not in authorized write_scope: {request.write_scope}")

                # Backup for rollback
                if os.path.exists(full_path):
                    with open(full_path, "rb") as f:
                        backups[full_path] = f.read()
                    actual_sha = compute_file_sha256(full_path)
                    if precondition_sha and actual_sha != precondition_sha:
                        raise ValueError(f"Precondition SHA mismatch on {rel_path}: expected {precondition_sha}, got {actual_sha}")
                else:
                    backups[full_path] = None

                # Match the bytes produced by the text-mode write below. On
                # Windows, newline=None translates each ``\n`` to ``\r\n``.
                encoded_content = content.replace("\n", os.linesep).encode("utf-8")
                if backups[full_path] == encoded_content:
                    raise ValueError(f"write_file produced no content change for {rel_path}")

                # Atomic write
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(full_path))
                with open(temp_fd, "w", encoding="utf-8") as f:
                    f.write(content)
                shutil.move(temp_path, full_path)

                changed_paths.append(rel_path)
                artifact_hashes[rel_path] = compute_file_sha256(full_path)

            elif action == "read_file":
                rel_path = request.payload.get("path")
                if not rel_path:
                    raise ValueError("Missing 'path' in read_file payload")
                full_path = os.path.normpath(os.path.join(workspace, rel_path))
                if not is_path_confined(full_path, workspace):
                    raise PermissionError(f"Path {rel_path} escapes workspace boundary")
                if not os.path.exists(full_path):
                    raise FileNotFoundError(f"File {rel_path} not found")

                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    data = f.read()
                artifact_hashes[rel_path] = compute_file_sha256(full_path)
                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="local_file_worker",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="SUCCESS",
                    exit_code=0,
                    workspace=workspace,
                    changed_paths=[],
                    artifact_hashes=artifact_hashes,
                    stdout_digest=f"Read {len(data)} chars",
                    evidence_payload={"content": data},
                    evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
                )

            elif action == "apply_patch":
                from extensions.autonomy_fabric.patch_engine import (
                    parse_unified_diff,
                    apply_patch_to_lines,
                    PatchPreconditionError,
                )
                patch_text = request.payload.get("patch", "")
                patches = parse_unified_diff(patch_text)
                if not patches:
                    raise ValueError("No valid unified diff hunks found in patch")

                for file_patch in patches:
                    if file_patch.is_deleted_file:
                        if not request.payload.get("allow_deletion", False):
                            raise PermissionError(f"File deletion not authorized for {file_patch.orig_path}")

                    target_rel = file_patch.new_path or file_patch.orig_path
                    if not target_rel:
                        continue

                    full_path = os.path.normpath(os.path.join(workspace, target_rel))
                    if not is_path_confined(full_path, workspace):
                        raise PermissionError(f"Path {target_rel} escapes workspace boundary")
                    if request.write_scope and not any(target_rel.startswith(prefix) for prefix in request.write_scope):
                        raise PermissionError(f"Path {target_rel} not in authorized write_scope: {request.write_scope}")

                    if os.path.exists(full_path):
                        with open(full_path, "rb") as f:
                            backups[full_path] = f.read()
                        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                            current_content = f.read().splitlines(keepends=True)
                    else:
                        backups[full_path] = None
                        current_content = []

                    precondition_shas = request.payload.get("precondition_shas", {})
                    expected_sha = file_patch.precondition_sha or precondition_shas.get(target_rel)
                    if expected_sha:
                        actual_sha = compute_file_sha256(full_path) if os.path.exists(full_path) else None
                        if not actual_sha or (not actual_sha.startswith(expected_sha) and not expected_sha.startswith(actual_sha)):
                            raise PatchPreconditionError(
                                f"Precondition SHA mismatch for {target_rel}: expected {expected_sha}, actual {actual_sha}"
                            )

                    # Apply via robust patch engine with context verification and offset tracking
                    patched_lines = apply_patch_to_lines(current_content, file_patch)
                    patched_content = "".join(patched_lines)
                    if patched_content == "".join(current_content):
                        raise PatchPreconditionError(
                            f"Patch produced no content change for {target_rel}"
                        )

                    os.makedirs(os.path.dirname(full_path), exist_ok=True)
                    temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(full_path))
                    with open(temp_fd, "w", encoding="utf-8") as f:
                        f.write(patched_content)
                    shutil.move(temp_path, full_path)

                    changed_paths.append(target_rel)
                    artifact_hashes[target_rel] = compute_file_sha256(full_path)

            else:
                raise ValueError(f"Unknown action {action}")

            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="local_file_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="SUCCESS",
                exit_code=0,
                workspace=workspace,
                changed_paths=changed_paths,
                artifact_hashes=artifact_hashes,
                stdout_digest=f"Modified {len(changed_paths)} file(s)",
                evidence_class=EvidenceClass.SOURCE_PROOF,
            )

        except Exception as e:
            # Perform atomic rollback
            for path, old_bytes in backups.items():
                try:
                    if old_bytes is None:
                        if os.path.exists(path):
                            os.remove(path)
                    else:
                        with open(path, "wb") as f:
                            f.write(old_bytes)
                except Exception as rb_err:
                    errors.append(f"Rollback failure for {path}: {rb_err}")

            errors.append(str(e))
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="local_file_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=workspace,
                changed_paths=[],
                sanitized_errors=errors,
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

    def _parse_unified_diff(self, patch_text: str) -> List[Tuple[str, List[str], List[str]]]:
        """Parses a simple unified diff into (target_rel_path, orig_lines, new_lines)."""
        results = []
        current_file = None
        new_lines: List[str] = []
        orig_lines: List[str] = []

        for line in patch_text.splitlines(keepends=True):
            if line.startswith("+++ b/"):
                if current_file:
                    results.append((current_file, orig_lines, new_lines))
                    new_lines = []
                    orig_lines = []
                current_file = line[6:].strip()
            elif line.startswith("+++ "):
                if current_file:
                    results.append((current_file, orig_lines, new_lines))
                    new_lines = []
                    orig_lines = []
                current_file = line[4:].strip()
            elif line.startswith("@@"):
                continue
            elif line.startswith("+") and not line.startswith("+++"):
                new_lines.append(line[1:])
            elif line.startswith("-") and not line.startswith("---"):
                orig_lines.append(line[1:])
            elif line.startswith(" "):
                new_lines.append(line[1:])
                orig_lines.append(line[1:])
            elif not line.startswith("---"):
                # Handle raw file content replacement if no diff header
                pass

        if current_file:
            results.append((current_file, orig_lines, new_lines))
        return results


class NativeProcessWorker(ExecutionBackend):
    """Native process runner for deterministic commands (pytest, node, python, etc.)."""

    backend_id = "native_process_worker"
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    supported_capabilities = {ExecutionCapability.PROCESS_EXEC}
    cost = ExecutionCost.FREE_LOCAL

    ALLOWED_BINARIES = {
        "python", "py", "pytest", "node", "npm", "npx", "git", "ffmpeg", "playwright"
    }

    def get_health(self) -> ExecutionHealth:
        return ExecutionHealth.HEALTHY

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        workspace = request.workspace
        cmd = request.payload.get("cmd", [])
        env_vars = request.payload.get("env", {})

        if not cmd or not isinstance(cmd, list):
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="local_process_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=workspace,
                sanitized_errors=["'cmd' must be a non-empty list of command arguments"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        binary_name = os.path.basename(cmd[0]).lower()
        if binary_name.endswith(".exe"):
            binary_name = binary_name[:-4]

        # Check allowed commands
        is_allowed = binary_name in self.ALLOWED_BINARIES
        if request.allowed_commands:
            is_allowed = is_allowed and any(binary_name == ac or cmd[0] == ac for ac in request.allowed_commands)

        if not is_allowed:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="local_process_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="DENIED",
                exit_code=126,
                workspace=workspace,
                sanitized_errors=[f"Binary '{cmd[0]}' is not in allowed command policy"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        # Build clean environment with only allowlisted variables + explicit additions
        safe_env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "TMP": os.environ.get("TMP", ""),
            "PYTHONPATH": os.environ.get("PYTHONPATH", workspace),
        }
        for k, v in env_vars.items():
            safe_env[k] = str(v)

        timeout = request.timeout_seconds or 180
        try:
            proc = run_headless(
                cmd,
                cwd=workspace,
                env=safe_env,
                timeout=timeout,
            )
            stdout_clean = redact_secrets(proc.stdout)
            stderr_clean = redact_secrets(proc.stderr)

            status = "SUCCESS" if proc.returncode == 0 else "FAILED"
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="local_process_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status=status,
                exit_code=proc.returncode,
                workspace=workspace,
                stdout_digest=stdout_clean[:2000],
                stderr_digest=stderr_clean[:2000],
                sanitized_errors=[stderr_clean] if proc.returncode != 0 else [],
                evidence_payload={"stdout": stdout_clean, "stderr": stderr_clean},
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )
        except subprocess.TimeoutExpired:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="local_process_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="TIMED_OUT",
                exit_code=124,
                workspace=workspace,
                sanitized_errors=[f"Process timed out after {timeout} seconds"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )
        except Exception as e:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="local_process_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=workspace,
                sanitized_errors=[redact_secrets(str(e))],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )


class NativeGitWorker(ExecutionBackend):
    """Native git worker for bounded version-control operations."""

    backend_id = "native_git_worker"
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    supported_capabilities = {ExecutionCapability.GIT_READ, ExecutionCapability.GIT_WRITE}
    cost = ExecutionCost.FREE_LOCAL

    PROHIBITED_SUBCOMMANDS = {"reset", "clean", "rebase", "filter-branch"}

    def get_health(self) -> ExecutionHealth:
        return ExecutionHealth.HEALTHY

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        workspace = request.workspace
        action = request.payload.get("action")
        args = request.payload.get("args", [])

        if not action:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="local_git_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=workspace,
                sanitized_errors=["Missing 'action' in git payload"],
                evidence_class=EvidenceClass.SOURCE_PROOF,
            )

        if action in self.PROHIBITED_SUBCOMMANDS:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="local_git_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="DENIED",
                exit_code=1,
                workspace=workspace,
                sanitized_errors=[f"Git action '{action}' is prohibited by safety policy"],
                evidence_class=EvidenceClass.SOURCE_PROOF,
            )

        if action == "push" and any(
            arg in ("--force", "-f", "--force-with-lease", "--force-if-includes")
            or str(arg).startswith("--force=")
            or str(arg).startswith("--force-with-lease=")
            for arg in args
        ):
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="local_git_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="DENIED",
                exit_code=1,
                workspace=workspace,
                sanitized_errors=["Force push is strictly prohibited"],
                evidence_class=EvidenceClass.SOURCE_PROOF,
            )

        cmd = ["git", action] + args
        try:
            proc = run_headless(
                cmd,
                cwd=workspace,
                timeout=request.timeout_seconds or 60,
            )
            stdout_clean = redact_secrets(proc.stdout)
            stderr_clean = redact_secrets(proc.stderr)
            status = "SUCCESS" if proc.returncode == 0 else "FAILED"

            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="local_git_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status=status,
                exit_code=proc.returncode,
                workspace=workspace,
                stdout_digest=stdout_clean[:1000],
                stderr_digest=stderr_clean[:1000],
                sanitized_errors=[stderr_clean] if proc.returncode != 0 else [],
                evidence_payload={"stdout": stdout_clean, "stderr": stderr_clean},
                evidence_class=EvidenceClass.SOURCE_PROOF,
            )
        except Exception as e:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="local_git_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=workspace,
                sanitized_errors=[redact_secrets(str(e))],
                evidence_class=EvidenceClass.SOURCE_PROOF,
            )


class GitHubCIWorker(ExecutionBackend):
    """Worker for observing canonical GitHub CI runs, commit statuses, and job logs."""

    backend_id = "github_ci_worker"
    trust_zone = ExecutionTrustZone.REMOTE_CI
    supported_capabilities = {ExecutionCapability.GITHUB_READ, ExecutionCapability.CI_OBSERVE}
    cost = ExecutionCost.FREE_LOCAL

    def __init__(self, mock_client: Optional[Any] = None):
        self.mock_client = mock_client

    def get_health(self) -> ExecutionHealth:
        return ExecutionHealth.HEALTHY

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        action = request.payload.get("action", "observe_run")
        sha = request.payload.get("sha")
        repo = request.payload.get("repo", "MertSGI/AOS")

        # Explicit target SHA is required for CI acceptance; HEAD cannot be treated as authoritative evidence
        if not sha or sha == "HEAD":
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="ci_observer",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=request.workspace,
                stdout_digest="Missing explicit target SHA for CI acceptance",
                sanitized_errors=["MISSING_EXPLICIT_CI_SHA: explicit commit SHA is required for authoritative CI acceptance"],
                evidence_payload={"sha": sha},
                evidence_class=EvidenceClass.CI_RUNTIME_PROOF,
            )

        if self.mock_client:
            data = self.mock_client.get_run_status(repo, sha)
            conclusion = data.get("conclusion")
            status_val = data.get("status")
            mock_sha = data.get("sha")
            # Fail closed on missing or mismatched SHA
            if not mock_sha or mock_sha != sha:
                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="ci_observer",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="FAILED",
                    exit_code=1,
                    workspace=request.workspace,
                    stdout_digest=f"CI SHA mismatch: requested {sha}, mock has {mock_sha}",
                    sanitized_errors=[f"MOCK_CI_SHA_MISMATCH: requested {sha}, got {mock_sha}"],
                    evidence_payload=data,
                    evidence_class=EvidenceClass.CI_RUNTIME_PROOF,
                )

            exec_status = "SUCCESS" if conclusion == "success" else ("FAILED" if conclusion in ("failure", "cancelled", "timed_out") else ("DEGRADED" if status_val in ("in_progress", "queued") else "FAILED"))
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="ci_observer",
                task_id=request.task_id,
                request_id=request.request_id,
                status=exec_status,
                exit_code=0 if exec_status == "SUCCESS" else 1,
                workspace=request.workspace,
                stdout_digest=f"CI status for {sha}: status={status_val}, conclusion={conclusion}",
                evidence_payload=data,
                evidence_class=EvidenceClass.CI_RUNTIME_PROOF,
            )

        # 1. Try gh CLI if installed
        gh_path = shutil.which("gh")
        if gh_path:
            try:
                cmd = ["gh", "run", "list", "--repo", repo, "--commit", sha or "HEAD", "--json", "status,conclusion,databaseId,headSha"]
                proc = run_headless(cmd, timeout=30)
                if proc.returncode == 0 and proc.stdout.strip():
                    runs = json.loads(proc.stdout)
                    if sha:
                        runs = [r for r in runs if r.get("headSha") == sha or r.get("head_sha") == sha]
                    if runs:
                        target = runs[0]
                        conclusion = target.get("conclusion")
                        status_val = target.get("status")
                        exec_status = "SUCCESS" if conclusion == "success" else ("FAILED" if conclusion in ("failure", "cancelled", "timed_out") else ("DEGRADED" if status_val in ("in_progress", "queued") else "FAILED"))
                        return ExecutionResult(
                            backend_id=self.backend_id,
                            worker_id="ci_observer",
                            task_id=request.task_id,
                            request_id=request.request_id,
                            status=exec_status,
                            exit_code=0 if exec_status == "SUCCESS" else 1,
                            workspace=request.workspace,
                            stdout_digest=f"CI run status via gh: status={status_val}, conclusion={conclusion}",
                            evidence_payload={"runs": runs, "conclusion": conclusion, "status": status_val},
                            evidence_class=EvidenceClass.CI_RUNTIME_PROOF,
                        )
            except Exception:
                pass

        # 2. Query GitHub REST API via strictly verified TLS
        try:
            api_url = f"https://api.github.com/repos/{repo}/actions/runs?head_sha={sha or 'HEAD'}"
            data = None

            # Attempt verified Python HTTPS connection with native system truststore or default SSL context
            try:
                import truststore
                truststore.inject_into_ssl()
            except Exception:
                pass

            import urllib.request
            import ssl
            ctx = ssl.create_default_context()
            req_api = urllib.request.Request(api_url, headers={"User-Agent": "AOS-CI-Worker"})
            with urllib.request.urlopen(req_api, context=ctx, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            runs = data.get("workflow_runs", [])
            # Filter exact head_sha if specified
            if sha:
                runs = [r for r in runs if r.get("head_sha") == sha]

            if not runs:
                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="ci_observer",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="FAILED",
                    exit_code=1,
                    workspace=request.workspace,
                    stdout_digest=f"No CI workflow runs found bound to SHA {sha}",
                    sanitized_errors=[f"NO_BOUND_CI_RUN_FOR_SHA: {sha}"],
                    evidence_payload={"sha": sha, "conclusion": "NO_RUNS"},
                    evidence_class=EvidenceClass.CI_RUNTIME_PROOF,
                )

            target_run = runs[0]
            conclusion = target_run.get("conclusion")
            status_val = target_run.get("status")

            exec_status = "SUCCESS" if conclusion == "success" else ("FAILED" if conclusion in ("failure", "cancelled", "timed_out") else ("DEGRADED" if status_val in ("in_progress", "queued") else "FAILED"))

            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="ci_observer",
                task_id=request.task_id,
                request_id=request.request_id,
                status=exec_status,
                exit_code=0 if exec_status == "SUCCESS" else 1,
                workspace=request.workspace,
                stdout_digest=f"CI run {target_run.get('id')}: status={status_val}, conclusion={conclusion}",
                evidence_payload={
                    "run_id": target_run.get("id"),
                    "sha": sha,
                    "status": status_val,
                    "conclusion": conclusion,
                    "html_url": target_run.get("html_url"),
                    "runs": runs,
                },
                evidence_class=EvidenceClass.CI_RUNTIME_PROOF,
            )
        except Exception as e:
            # FAIL CLOSED: Network / TLS / API failure must report FAILED, never masquerade as CI PASS
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="ci_observer",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=request.workspace,
                stdout_digest="CI observer failed to reach canonical GitHub API",
                sanitized_errors=[f"CI_API_FAILURE: {str(e)}"],
                evidence_payload={"sha": sha, "error": str(e)},
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )


class BrowserExecutionBackend(ExecutionBackend):
    """Bridges existing Design Intelligence Playwright/browser capabilities into ExecutionBackend."""

    backend_id = "browser_execution_backend"
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    supported_capabilities = {ExecutionCapability.BROWSER}
    cost = ExecutionCost.FREE_LOCAL

    def __init__(self, capture_adapter: Optional[Any] = None):
        self.capture_adapter = capture_adapter

    def get_health(self) -> ExecutionHealth:
        return ExecutionHealth.HEALTHY

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        url = request.payload.get("url", "")
        run_id = request.payload.get("run_id", request.task_id)

        adapter = self.capture_adapter
        if not adapter:
            from extensions.design_intelligence.browser_capture import RealBrowserCaptureAdapter
            adapter = RealBrowserCaptureAdapter()

        try:
            manifest = adapter.capture_manifest(url, run_id)
            # Section 2: BrowserExecutionBackend must not synthesize evidence defaults.
            # Require manifest to explicitly contain:
            # viewports_captured, file_hashes, screenshot_paths, horizontal_overflow_detected,
            # cta_visible, console_errors, page_errors, dom_inspection, dom_metrics,
            # capture_adapter, capture_mode.
            # Do NOT use getattr(..., []), getattr(..., "PASS"), etc. for acceptance-critical fields.
            required_attrs = [
                "viewports_captured",
                "file_hashes",
                "screenshot_paths",
                "horizontal_overflow_detected",
                "cta_visible",
                "console_errors",
                "page_errors",
                "dom_inspection",
                "dom_metrics",
                "capture_adapter",
                "capture_mode",
            ]
            missing_fields = [attr for attr in required_attrs if getattr(manifest, attr, None) is None]
            if missing_fields:
                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="browser_adapter",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="FAILED",
                    exit_code=1,
                    workspace=request.workspace,
                    stdout_digest=f"Browser capture failed: missing required measured fields {missing_fields}",
                    sanitized_errors=[f"BROWSER_MEASURED_EVIDENCE_ABSENT: missing required measured fields: {', '.join(missing_fields)}"],
                    evidence_payload={
                        "manifest_id": getattr(manifest, "manifest_id", f"man-{run_id}"),
                        "missing_fields": missing_fields,
                    },
                    evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
                )

            viewports = manifest.viewports_captured
            hashes = manifest.file_hashes
            screenshot_paths = manifest.screenshot_paths
            overflow_map = manifest.horizontal_overflow_detected
            cta_map = manifest.cta_visible
            console_errors = manifest.console_errors
            page_errors = manifest.page_errors
            dom_inspection = manifest.dom_inspection
            dom_metrics = manifest.dom_metrics
            cap_adapter = manifest.capture_adapter
            cap_mode = manifest.capture_mode

            # Require RealBrowserCaptureAdapter and REAL_LOCAL_BROWSER_SCREENSHOT for real-browser proof
            if cap_adapter != "RealBrowserCaptureAdapter" or cap_mode != "REAL_LOCAL_BROWSER_SCREENSHOT":
                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="browser_adapter",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="FAILED",
                    exit_code=1,
                    workspace=request.workspace,
                    stdout_digest=f"Browser capture failed: invalid adapter/mode ({cap_adapter}, {cap_mode})",
                    sanitized_errors=[f"BROWSER_MEASURED_EVIDENCE_ABSENT: capture_adapter must be 'RealBrowserCaptureAdapter' and capture_mode must be 'REAL_LOCAL_BROWSER_SCREENSHOT', got adapter={cap_adapter}, mode={cap_mode}"],
                    evidence_payload={
                        "manifest_id": getattr(manifest, "manifest_id", f"man-{run_id}"),
                        "capture_adapter": cap_adapter,
                        "capture_mode": cap_mode,
                    },
                    evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
                )

            if not viewports or not hashes or not screenshot_paths:
                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="browser_adapter",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="FAILED",
                    exit_code=1,
                    workspace=request.workspace,
                    stdout_digest="Browser capture failed: empty viewports or screenshots",
                    sanitized_errors=["BROWSER_MEASURED_EVIDENCE_ABSENT: empty viewports, hashes, or screenshot paths in manifest"],
                    evidence_payload={
                        "manifest_id": getattr(manifest, "manifest_id", f"man-{run_id}"),
                    },
                    evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
                )

            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="browser_adapter",
                task_id=request.task_id,
                request_id=request.request_id,
                status="SUCCESS",
                exit_code=0,
                workspace=request.workspace,
                artifact_hashes=hashes,
                stdout_digest=f"Captured {len(viewports)} viewports (DOM & errors verified)",
                evidence_payload={
                    "manifest_id": getattr(manifest, "manifest_id", f"man-{run_id}"),
                    "viewports": viewports,
                    "screenshot_paths": screenshot_paths,
                    "horizontal_overflow_detected": overflow_map,
                    "cta_visible": cta_map,
                    "console_errors": console_errors,
                    "page_errors": page_errors,
                    "dom_inspection": dom_inspection,
                    "dom_metrics": dom_metrics,
                    "capture_adapter": cap_adapter,
                    "capture_mode": cap_mode,
                },
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )
        except Exception as e:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="browser_adapter",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=request.workspace,
                sanitized_errors=[str(e)],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )


class ModelReasoningBackend(ExecutionBackend):
    """Model reasoning backend: reasons and proposes bounded patches without direct file mutation."""

    backend_id = "model_reasoning_backend"
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    supported_capabilities = {ExecutionCapability.MODEL_REASONING}
    cost = ExecutionCost.FREE_TIER_CLOUD

    def __init__(
        self,
        provider_router: Optional[Any] = None,
        mock_reasoner: Optional[Any] = None,
        provider_executor_factory: Optional[Any] = None,
        execution_mode: str = "RUNTIME_PROVIDER_REQUIRED",
    ):
        self.provider_router = provider_router
        self.mock_reasoner = mock_reasoner
        self.provider_executor_factory = provider_executor_factory
        self.execution_mode = execution_mode  # RUNTIME_PROVIDER_REQUIRED or OFFLINE_TEST_PROVIDER

    def get_health(self) -> ExecutionHealth:
        return ExecutionHealth.HEALTHY

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        prompt = request.payload.get("prompt", "")
        context = request.payload.get("context", {})
        risk_class = request.payload.get("risk_class", "R0")
        schema = request.payload.get("schema", {})
        exec_mode = request.payload.get("execution_mode", self.execution_mode)

        # 1. Direct mock reasoner injection (if explicitly provided for unit tests)
        if self.mock_reasoner:
            plan_response = self.mock_reasoner(prompt, context)
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_reasoner",
                task_id=request.task_id,
                request_id=request.request_id,
                status="SUCCESS",
                exit_code=0,
                workspace=request.workspace,
                stdout_digest=f"Generated model proposal for {request.task_id}",
                evidence_payload={"proposal": plan_response, "provider_route": "INJECTED_MOCK"},
                evidence_class=EvidenceClass.SOURCE_PROOF,
            )

        # 2. Resolve ProviderRouter from request payload, project descriptor, or injected router
        router = self.provider_router
        if not router:
            policy_data = request.payload.get("routing_policy")
            if not policy_data:
                # Check for explicit policy file in payload or workspace
                policy_file = request.payload.get("policy_file")
                if policy_file and os.path.isabs(policy_file) and os.path.exists(policy_file):
                    with open(policy_file, "r", encoding="utf-8") as f:
                        policy_data = json.load(f)
                elif policy_file and os.path.exists(os.path.join(request.workspace, policy_file)):
                    with open(os.path.join(request.workspace, policy_file), "r", encoding="utf-8") as f:
                        policy_data = json.load(f)

            if policy_data:
                try:
                    from aos.provider_registry import ProviderRegistry, ProviderRouter
                    reg = ProviderRegistry(policy_data)
                    router = ProviderRouter(reg)
                except Exception:
                    router = None

        routed_provider = "offline_deterministic_provider"
        model_id = "aos-reasoning-v2"
        selection_reason = "Default offline provider routing"
        provider_instance = None

        if router:
            # Runtime cloud routing requires valid credentials (ignore_credentials=False)
            ignore_creds = request.payload.get("ignore_credentials", False)
            route_res = router.select(risk_class=risk_class, ignore_credentials=ignore_creds)
            if not route_res:
                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="model_reasoner",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="FAILED",
                    exit_code=1,
                    workspace=request.workspace,
                    stdout_digest="No eligible provider found by ProviderRouter",
                    sanitized_errors=["PROVIDER_POLICY_HOLD: no eligible provider found"],
                    evidence_class=EvidenceClass.SOURCE_PROOF,
                )
            routed_provider = route_res.selected_provider_id
            model_id = route_res.selected_model_id
            selection_reason = route_res.selection_reason

        # 3. Obtain provider executor instance and execute reasoning contract
        if self.provider_executor_factory:
            provider_instance = self.provider_executor_factory(routed_provider, model_id)
        elif request.payload.get("provider_executor"):
            provider_instance = request.payload["provider_executor"]

        if provider_instance and hasattr(provider_instance, "generate_plan"):
            try:
                plan_data, resp_id, usage = provider_instance.generate_plan(prompt, schema)
                # Structured response validation
                if not isinstance(plan_data, dict):
                    raise ValueError(f"Provider response must be structured dictionary, got {type(plan_data)}")

                suggested_patch = plan_data.get("suggested_patch")
                if suggested_patch:
                    # Validate patch syntax and target paths
                    from extensions.autonomy_fabric.patch_engine import parse_unified_diff
                    parsed = parse_unified_diff(suggested_patch)
                    if not parsed:
                        raise ValueError("Model suggested_patch contains invalid unified diff syntax")
                    for p in parsed:
                        t_path = p.new_path or p.orig_path
                        if t_path and request.write_scope and not any(t_path.startswith(prefix) for prefix in request.write_scope):
                            raise PermissionError(f"Model proposed patch targeting unauthorized path {t_path}")

                # Section 1: Remove provider-name heuristics entirely.
                # Evidence class must derive only from explicit executor provenance.
                # Expected contract: provider executor exposes execution_provenance ("LOCAL_OFFLINE" or "LIVE_EXTERNAL")
                # Hosted provider: LIVE_EXTERNAL_PROOF only when executor explicitly reports a successful external invocation.
                # Fake/offline provider: LOCAL_RUNTIME_PROOF
                # Unknown/missing provenance: LOCAL_RUNTIME_PROOF; never promote to LIVE_EXTERNAL_PROOF.
                prov = getattr(provider_instance, "execution_provenance", None)
                if prov is None:
                    # Backward compatibility for existing test mocks, but NEVER promote to LIVE_EXTERNAL based on name
                    prov = "LOCAL_OFFLINE"

                if prov == "LIVE_EXTERNAL":
                    ev_class = EvidenceClass.LIVE_EXTERNAL_PROOF
                else:
                    ev_class = EvidenceClass.LOCAL_RUNTIME_PROOF

                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="model_reasoner",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="SUCCESS",
                    exit_code=0,
                    workspace=request.workspace,
                    stdout_digest=f"Generated structured proposal via {routed_provider} ({model_id})",
                    evidence_payload={
                        "proposal": plan_data,
                        "provider_route": routed_provider,
                        "model_id": model_id,
                        "response_id": resp_id,
                        "usage": usage,
                        "selection_reason": selection_reason,
                        "execution_mode": exec_mode,
                    },
                    evidence_class=ev_class,
                )
            except Exception as ex:
                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="model_reasoner",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="FAILED",
                    exit_code=1,
                    workspace=request.workspace,
                    stdout_digest=f"Provider {routed_provider} execution failed: {ex}",
                    sanitized_errors=[str(ex)],
                    evidence_class=EvidenceClass.SOURCE_PROOF,
                )

        # 4. Fail closed in RUNTIME_PROVIDER_REQUIRED mode if no executable provider is available
        if exec_mode == "RUNTIME_PROVIDER_REQUIRED":
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_reasoner",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=request.workspace,
                stdout_digest=f"No executable provider instance available for {routed_provider} in runtime mode",
                sanitized_errors=[f"PROVIDER_EXECUTOR_UNAVAILABLE: provider '{routed_provider}' has no executable contract instance in RUNTIME_PROVIDER_REQUIRED mode"],
                evidence_payload={"provider_route": routed_provider, "execution_mode": exec_mode},
                evidence_class=EvidenceClass.SOURCE_PROOF,
            )

        # 5. Explicit OFFLINE_TEST_PROVIDER mode only
        target_file = context.get("target_file", "output.txt")
        proposal = {
            "reasoning": f"Analyzed task {request.task_id} via provider {routed_provider} ({model_id}) on workspace {request.workspace}",
            "suggested_patch": f"--- a/{target_file}\n+++ b/{target_file}\n@@ -1,1 +1,1 @@\n-old\n+new\n",
            "confidence": 1.0,
            "provider_route": routed_provider,
            "model_id": model_id,
            "selection_reason": selection_reason,
        }
        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id="model_reasoner",
            task_id=request.task_id,
            request_id=request.request_id,
            status="SUCCESS",
            exit_code=0,
            workspace=request.workspace,
            stdout_digest=f"Autonomous offline reasoning completed via {routed_provider}",
            evidence_payload={"proposal": proposal, "provider_route": routed_provider, "model_id": model_id, "execution_mode": exec_mode},
            evidence_class=EvidenceClass.SOURCE_PROOF,
        )


class AntigravityExecutionBackend(ExecutionBackend):
    """Optional specialist fallback wrapping Antigravity CLI."""

    backend_id = "antigravity_backend"
    trust_zone = ExecutionTrustZone.HOST_USER
    supported_capabilities = {ExecutionCapability.ANTIGRAVITY}
    cost = ExecutionCost.QUOTA_LIMITED

    def __init__(self, adapter: Optional[Any] = None, simulate_exhausted: bool = False, simulate_unavailable: bool = False):
        self.adapter = adapter
        self.simulate_exhausted = simulate_exhausted
        self.simulate_unavailable = simulate_unavailable

    def get_health(self) -> ExecutionHealth:
        if self.simulate_unavailable:
            return ExecutionHealth.UNAVAILABLE
        if self.simulate_exhausted:
            return ExecutionHealth.QUOTA_EXHAUSTED
        return ExecutionHealth.HEALTHY

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if self.simulate_unavailable:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="antigravity_cli",
                task_id=request.task_id,
                request_id=request.request_id,
                status="DEGRADED",
                exit_code=1,
                workspace=request.workspace,
                sanitized_errors=["Antigravity binary unavailable or missing"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        if self.simulate_exhausted:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="antigravity_cli",
                task_id=request.task_id,
                request_id=request.request_id,
                status="DEGRADED",
                exit_code=1,
                workspace=request.workspace,
                sanitized_errors=["Antigravity quota exhausted (429 / ResourceExhausted)"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        if not self.adapter:
            from extensions.autonomy_fabric.antigravity_adapter import FakeAntigravityAdapter
            self.adapter = FakeAntigravityAdapter()

        prompt = request.payload.get("prompt", f"Execute task {request.task_id}")
        resp = self.adapter.execute_prompt(prompt, workspace_path=request.workspace)

        status = "SUCCESS" if resp.mapped_aos_status.value == "COMPLETED" else "FAILED"
        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id="antigravity_cli",
            task_id=request.task_id,
            request_id=request.request_id,
            status=status,
            exit_code=0 if status == "SUCCESS" else 1,
            workspace=request.workspace,
            stdout_digest=resp.raw_response[:500],
            evidence_payload={"conversation_id": resp.conversation_id, "turns": resp.turn_count},
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
        )
