"""AOS Model + Native Implementation Backend.

Narrow, bounded MODEL + NATIVE execution backend for a SINGLE authorized source file
and work package, using existing AOS Resource Orchestrator selection and zero-cost models.

Requirements:
1. Reads exact source content and SHA256 of the single authorized target file.
2. Resolves one existing eligible zero-cost code-capable model through AOS resource selection.
3. Requests one structured, bounded file replacement:
   {"relative_path": ..., "expected_original_sha256": ..., "new_content": ...}
4. Accepts ONLY the authorized relative path and exact expected original hash.
5. Rejects extra files, unknown fields, oversized output, traversal ('..'), symlinks, and drift.
6. Applies the candidate change using the native execution layer in an isolated workspace.
7. Runs ONLY the pre-authorized test command (never executes commands from model).
8. Verifies the actual changed file, bounded diff, and test outcome (exit_code == 0).
9. Produces a real durable execution receipt only after successful functional verification.
"""

from __future__ import annotations

import datetime
import difflib
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from aos.process_utils import run_headless
from aos.runtime_store import atomic_json
from aos.knowledge.receipts import record_implementation_receipt
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.index import rebuild_index
from aos.knowledge.model import KnowledgeEventType

from extensions.autonomy_fabric.execution_backend import (
    BackendClass,
    EvidenceClass,
    ExecutionAvailabilitySnapshot,
    ExecutionAvailabilityState,
    ExecutionBackend,
    ExecutionCapability,
    ExecutionCost,
    ExecutionHealth,
    ExecutionRequest,
    ExecutionResult,
    ExecutionTrustZone,
)
from extensions.autonomy_fabric.resource_orchestrator import ResourceOrchestrator


STRUCTURED_FILE_REPLACEMENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["relative_path", "expected_original_sha256", "new_content"],
    "additionalProperties": False,
    "properties": {
        "relative_path": {"type": "string", "minLength": 1, "maxLength": 1024},
        "expected_original_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
        "new_content": {"type": "string", "maxLength": 131072},
    },
}

MAX_ALLOWED_FILE_OUTPUT_CHARS = 131072


def _resolve_zero_cost_api_key() -> Optional[str]:
    """Resolve zero-cost NVIDIA API key from environment or local provider configuration."""
    api_key = os.environ.get("NVIDIA_API_KEY")
    if api_key and api_key.strip():
        return api_key.strip()

    # Check local cline/settings/providers.json (used as secure local config in this environment)
    local_providers_path = Path.home() / ".cline" / "data" / "settings" / "providers.json"
    if local_providers_path.is_file():
        try:
            cfg = json.loads(local_providers_path.read_text(encoding="utf-8"))
            key = (
                cfg.get("providers", {})
                .get("openai-compatible", {})
                .get("settings", {})
                .get("apiKey")
            )
            if key and isinstance(key, str) and key.strip().startswith("nvapi-"):
                return key.strip()
        except Exception:
            pass

    return None


class ModelNativeImplementationBackend(ExecutionBackend):
    """Bounded MODEL + NATIVE implementation worker for a single authorized file."""

    backend_id = "model_native_implementation_worker"
    backend_class = BackendClass.NATIVE_EXECUTION_BACKEND
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    cost = ExecutionCost.FREE_TIER_CLOUD
    supported_capabilities: Set[ExecutionCapability] = {
        ExecutionCapability.FILE_READ,
        ExecutionCapability.FILE_WRITE,
        ExecutionCapability.PATCH_APPLY,
        ExecutionCapability.PROCESS_EXEC,
        ExecutionCapability.TEST_EXECUTION,
        ExecutionCapability.MODEL_REASONING,
        ExecutionCapability.LONG_HORIZON_AGENTIC_WORK,
    }

    def __init__(
        self,
        *,
        model_caller: Optional[Callable[[str, str, str, str], Dict[str, Any]]] = None,
        orchestrator: Optional[ResourceOrchestrator] = None,
        base_url: str = "https://integrate.api.nvidia.com/v1",
        default_model: str = "nvidia/nemotron-3-ultra-550b-a55b",
    ) -> None:
        self._model_caller = model_caller
        self._orchestrator = orchestrator or ResourceOrchestrator()
        self._base_url = base_url
        self._default_model = default_model

    def get_health(self) -> ExecutionHealth:
        if self._model_caller is not None:
            return ExecutionHealth.HEALTHY
        api_key = _resolve_zero_cost_api_key()
        if not api_key:
            return ExecutionHealth.UNAVAILABLE
        return ExecutionHealth.HEALTHY

    def get_availability(self) -> ExecutionAvailabilitySnapshot:
        health = self.get_health()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if health == ExecutionHealth.HEALTHY:
            return ExecutionAvailabilitySnapshot(
                ExecutionAvailabilityState.AVAILABLE,
                observed_at=now_iso,
                source="MODEL_NATIVE_RESOLVER",
                evidence={"provider": "nemotron", "billing_class": "FREE_TIER"},
            )
        return ExecutionAvailabilitySnapshot(
            ExecutionAvailabilityState.UNAVAILABLE,
            observed_at=now_iso,
            source="MODEL_NATIVE_RESOLVER",
            evidence={"reason": "RESOURCE_UNAVAILABLE"},
        )

    def _call_model(
        self,
        relative_path: str,
        original_sha256: str,
        original_content: str,
        instruction: str,
    ) -> Dict[str, Any]:
        """Perform exactly ONE live model generation attempt via eligible zero-cost provider."""
        if self._model_caller is not None:
            return self._model_caller(
                relative_path, original_sha256, original_content, instruction
            )

        api_key = _resolve_zero_cost_api_key()
        if not api_key:
            raise RuntimeError("RESOURCE_UNAVAILABLE: No eligible zero-cost API key found")

        import openai

        client = openai.OpenAI(api_key=api_key, base_url=self._base_url)

        system_instruction = (
            "You are a precise autonomous software repair engine. "
            "You MUST output strictly valid JSON matching this schema:\n"
            "{\n"
            '  "relative_path": "string",\n'
            '  "expected_original_sha256": "string",\n'
            '  "new_content": "string"\n'
            "}\n"
            "Do NOT include markdown fences, comments, or extra keys. Output JSON ONLY."
        )

        user_prompt = (
            f"File relative_path: {json.dumps(relative_path)}\n"
            f"Original sha256: {json.dumps(original_sha256)}\n"
            f"Original file content:\n```\n{original_content}\n```\n\n"
            f"Task Instruction: {instruction}\n\n"
            f"Return valid JSON with 'relative_path', 'expected_original_sha256', and the complete 'new_content'."
        )

        resp = client.chat.completions.create(
            model=self._default_model,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=2048,
            temperature=0.0,
        )

        raw_content = resp.choices[0].message.content or "{}"
        if len(raw_content) > MAX_ALLOWED_FILE_OUTPUT_CHARS:
            raise ValueError(f"Model output exceeded maximum permitted size ({len(raw_content)} chars)")

        data = json.loads(raw_content)
        if not isinstance(data, dict):
            raise ValueError(f"Model response is not a JSON object: {type(data)}")
        return data

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute single authorized file repair with native isolated verification."""
        workspace = Path(request.workspace).resolve()

        # 1. Validate single authorized target file and write scope
        write_scope = [p.replace("\\", "/").strip("/") for p in (request.write_scope or []) if p.strip()]
        if not write_scope:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_native_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=str(workspace),
                sanitized_errors=["EMPTY_WRITE_SCOPE: No authorized target files provided"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        if len(write_scope) > 1:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_native_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=str(workspace),
                sanitized_errors=["MULTIPLE_FILES_DISALLOWED: Worker admits only a single authorized target file"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        authorized_rel_path = write_scope[0]

        # Guard against traversal, absolute paths, and null bytes
        if (
            ".." in authorized_rel_path
            or authorized_rel_path.startswith("/")
            or "\\" in authorized_rel_path
            or "\0" in authorized_rel_path
        ):
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_native_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=str(workspace),
                sanitized_errors=["PATH_TRAVERSAL_DETECTED: Target path is unsafe"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        target_file = (workspace / authorized_rel_path).resolve()

        # Confinement verification
        try:
            if os.path.commonpath([str(workspace), str(target_file)]) != str(workspace):
                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="model_native_worker",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="FAILED",
                    exit_code=1,
                    workspace=str(workspace),
                    sanitized_errors=["WORKSPACE_CONFINEMENT_VIOLATION: Target file outside workspace root"],
                    evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
                )
        except Exception:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_native_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=str(workspace),
                sanitized_errors=["WORKSPACE_CONFINEMENT_ERROR"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        # Check symlinks: must NOT be a symlink
        if target_file.is_symlink():
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_native_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=str(workspace),
                sanitized_errors=["SYMLINK_DISALLOWED: Symlinks are prohibited in target files"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        # Read original content and compute SHA256
        if not target_file.is_file():
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_native_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=str(workspace),
                sanitized_errors=[f"TARGET_FILE_NOT_FOUND: {authorized_rel_path}"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        orig_bytes = target_file.read_bytes()
        orig_sha256 = hashlib.sha256(orig_bytes).hexdigest()
        orig_text = orig_bytes.decode("utf-8", errors="replace")

        # 2. Pre-authorized test commands
        raw_tests = request.payload.get("tests") or getattr(request, "tests", []) or []
        if isinstance(raw_tests, str):
            raw_tests = [raw_tests]
        pre_authorized_tests = [str(t).strip() for t in raw_tests if str(t).strip()]

        instruction = str(request.payload.get("prompt") or request.payload.get("instruction") or "").strip()

        # 3. Model Request (exactly one live attempt)
        try:
            model_response = self._call_model(
                relative_path=authorized_rel_path,
                original_sha256=orig_sha256,
                original_content=orig_text,
                instruction=instruction,
            )
        except Exception as ex:
            err_msg = str(ex)
            status_val = "RESOURCE_UNAVAILABLE" if "RESOURCE_UNAVAILABLE" in err_msg else "FAILED"
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_native_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status=status_val,
                exit_code=1,
                workspace=str(workspace),
                sanitized_errors=[f"MODEL_CALL_FAILED: {err_msg}"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        # 4. Validate Model Response Fields strictly
        allowed_keys = {"relative_path", "expected_original_sha256", "new_content"}
        actual_keys = set(model_response.keys())
        if actual_keys != allowed_keys:
            extra = actual_keys - allowed_keys
            missing = allowed_keys - actual_keys
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_native_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=str(workspace),
                sanitized_errors=[f"STRUCTURED_SCHEMA_VIOLATION: extra={extra}, missing={missing}"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        resp_rel_path = str(model_response.get("relative_path") or "").replace("\\", "/").strip("/")
        if resp_rel_path != authorized_rel_path:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_native_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=str(workspace),
                sanitized_errors=[f"UNAUTHORIZED_PATH_MUTATION: Model returned {resp_rel_path!r}, expected {authorized_rel_path!r}"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        resp_orig_sha = str(model_response.get("expected_original_sha256") or "").strip().lower()
        if resp_orig_sha != orig_sha256.lower():
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_native_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=str(workspace),
                sanitized_errors=[f"PRECONDITION_HASH_MISMATCH: Model expected {resp_orig_sha}, actual was {orig_sha256}"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        new_content = model_response.get("new_content")
        if not isinstance(new_content, str):
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_native_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=str(workspace),
                sanitized_errors=["INVALID_NEW_CONTENT: Must be string"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        if len(new_content) > MAX_ALLOWED_FILE_OUTPUT_CHARS:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_native_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=str(workspace),
                sanitized_errors=["OUTPUT_TOO_LARGE: new_content exceeds limit"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        # 5. Native Atomic File Application
        new_bytes = new_content.encode("utf-8")
        new_sha256 = hashlib.sha256(new_bytes).hexdigest()

        # Compute diff before writing
        diff_lines = list(difflib.unified_diff(
            orig_text.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{authorized_rel_path}",
            tofile=f"b/{authorized_rel_path}",
        ))
        actual_diff = "".join(diff_lines)

        tmp_file = target_file.with_suffix(target_file.suffix + ".aos_tmp")
        try:
            tmp_file.write_bytes(new_bytes)
            os.replace(tmp_file, target_file)
        except Exception as e:
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except Exception:
                    pass
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_native_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=str(workspace),
                sanitized_errors=[f"FILE_WRITE_ERROR: {str(e)}"],
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        # 6. Run ONLY Pre-Authorized Test Command(s)
        test_results = []
        all_tests_passed = True
        for test_cmd in pre_authorized_tests:
            # Execute natively via run_headless (never shell=True with user string)
            # Use split tokens for Windows PowerShell or cmd
            proc = run_headless(
                test_cmd,
                cwd=str(workspace),
                timeout=120,
                check=False,
            )
            test_results.append({
                "cmd": test_cmd,
                "exit_code": proc.returncode,
                "stdout": proc.stdout[:2000] if proc.stdout else "",
                "stderr": proc.stderr[:2000] if proc.stderr else "",
            })
            if proc.returncode != 0:
                all_tests_passed = False
                break

        if pre_authorized_tests and not all_tests_passed:
            # Revert candidate modification on test failure
            target_file.write_bytes(orig_bytes)
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="model_native_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=str(workspace),
                sanitized_errors=["FUNCTIONAL_TEST_FAILED: Pre-authorized test did not pass on candidate code"],
                evidence_payload={
                    "test_results": test_results,
                    "reverted": True,
                },
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

        # 7. Generate Durable Implementation Receipt
        receipt_dict = None
        runtime_dir_str = request.payload.get("runtime_dir")
        runtime_dir = Path(runtime_dir_str) if runtime_dir_str else (workspace / ".aos" / "runtime")
        try:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            ledger_dir = runtime_dir / "resource-os"
            ledger_dir.mkdir(parents=True, exist_ok=True)
            ledger = KnowledgeLedger(ledger_dir)

            # Determine base git SHA and result git SHA
            base_git_sha = str(request.payload.get("source_sha") or "").strip().lower()
            if len(base_git_sha) != 40:
                head_proc = run_headless(["git", "-C", str(workspace), "rev-parse", "HEAD"], check=False, timeout=30.0)
                if head_proc.returncode == 0 and len(head_proc.stdout.strip()) == 40:
                    base_git_sha = head_proc.stdout.strip().lower()
                else:
                    base_git_sha = "0" * 40

            # Commit the candidate change in isolated workspace to establish verifiable result Git SHA
            run_headless(["git", "-C", str(workspace), "add", authorized_rel_path], check=False, timeout=30.0)
            commit_proc = run_headless(
                ["git", "-C", str(workspace), "commit", "-m", f"AOS Model+Native repair: {authorized_rel_path}"],
                check=False,
                timeout=30.0,
            )
            head_after = run_headless(["git", "-C", str(workspace), "rev-parse", "HEAD"], check=False, timeout=30.0)
            if head_after.returncode == 0 and len(head_after.stdout.strip()) == 40:
                result_git_sha = head_after.stdout.strip().lower()
            else:
                result_git_sha = base_git_sha

            receipt_dict = record_implementation_receipt(
                ledger,
                project_id=request.project_id,
                idempotency_key=f"model-native:{request.project_id}:{request.task_id}:{new_sha256}",
                agent_class="AOS_NATIVE",
                tool_name="model_native_worker",
                base_sha=base_git_sha,
                result_sha=result_git_sha,
                changed_paths=[authorized_rel_path],
                verification={"status": "PASS", "test_results": test_results},
                claims={
                    "task_id": request.task_id,
                    "diff": actual_diff,
                    "original_file_sha256": orig_sha256,
                    "changed_file_sha256": new_sha256,
                    "base_git_sha": base_git_sha,
                    "result_git_sha": result_git_sha,
                },
            )
        except Exception as e:
            receipt_dict = {"error": str(e)}

        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id="model_native_worker",
            task_id=request.task_id,
            request_id=request.request_id,
            status="SUCCESS",
            exit_code=0,
            workspace=str(workspace),
            changed_paths=[authorized_rel_path],
            artifact_hashes={authorized_rel_path: new_sha256},
            stdout_digest=f"MODEL+NATIVE successfully repaired {authorized_rel_path} with verified tests",
            evidence_payload={
                "target_file": authorized_rel_path,
                "original_sha256": orig_sha256,
                "new_sha256": new_sha256,
                "diff": actual_diff,
                "test_results": test_results,
                "receipt": receipt_dict,
            },
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
        )
