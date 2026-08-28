"""AOS-4 Generic Deterministic Independent Verification Foundation.

Provides deterministic verification-result contracts and verifier boundaries
that consume ControlledExecutionResult and evidence metadata without executing
or mutating the worker task.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from aos.validate import validate_document, load_json_strict, ValidationResult

EVIDENCE_LEVEL_RANK: Dict[str, int] = {
    "E0_CLAIM": 0,
    "E1_LOCAL_SOURCE": 1,
    "E1_REMOTE_SOURCE_PROVEN": 1,
    "E2_EXECUTABLE_EXACT_REVISION_PROVEN": 2,
    "E3_ISOLATED_RUNTIME_PROVEN": 3,
    "E4_SHARED_STAGING_PROVEN": 4,
    "E5_PRODUCTION_OBSERVATION": 5,
}

SHA_REGEX = re.compile(r"^[0-9a-f]{40}$")
SHA256_REGEX = re.compile(r"^[0-9a-f]{64}$")
CANDIDATE_ID_REGEX = re.compile(r"^cand_[0-9a-f]{16}$")


class VerificationCheck:
    """Individual deterministic verification check result."""

    def __init__(
        self,
        check_id: str,
        status: str,
        message: Optional[str] = None,
    ):
        if status not in ("PASS", "FAIL", "NOT_RUN"):
            raise ValueError(f"Invalid verification check status: '{status}'")
        self.check_id = check_id
        self.status = status
        self.message = message

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "check_id": self.check_id,
            "status": self.status,
        }
        if self.message is not None:
            d["message"] = self.message
        return d

    def __repr__(self) -> str:
        msg = f", message={self.message!r}" if self.message else ""
        return f"VerificationCheck({self.check_id!r}, {self.status!r}{msg})"


class VerificationResult:
    """Contract object for deterministic independent verification output."""

    def __init__(
        self,
        project_id: str,
        task_id: str,
        gate: str,
        disposition: str,  # "PASS" or "HOLD"
        verifier: str = "AOS-4 Independent Verifier",
        verified_at: Optional[str] = None,
        checks: Optional[List[VerificationCheck]] = None,
        errors: Optional[List[str]] = None,
        authorizes_canonical_closure: bool = False,
        execution_base_sha: Optional[str] = None,
        control_source_sha: Optional[str] = None,
        evidence_id: Optional[str] = None,
        extensions: Optional[Dict[str, Any]] = None,
        schema_version: str = "0.1.0",
    ):
        if disposition not in ("PASS", "HOLD"):
            raise ValueError(f"Invalid verification disposition: '{disposition}'")
        self.schema_version = schema_version
        self.project_id = project_id
        self.task_id = task_id
        self.gate = gate
        self.disposition = disposition
        self.verifier = verifier
        self.verified_at = verified_at or datetime.now(timezone.utc).isoformat()
        self.checks = checks or []
        self.errors = errors or []
        self.authorizes_canonical_closure = authorizes_canonical_closure
        self.execution_base_sha = execution_base_sha
        self.control_source_sha = control_source_sha
        self.evidence_id = evidence_id
        self.extensions = extensions or {}

    @property
    def is_pass(self) -> bool:
        return self.disposition == "PASS"

    @property
    def is_hold(self) -> bool:
        return self.disposition == "HOLD"

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "gate": self.gate,
            "disposition": self.disposition,
            "verifier": self.verifier,
            "verified_at": self.verified_at,
            "checks": [c.to_dict() for c in self.checks],
            "authorizes_canonical_closure": self.authorizes_canonical_closure,
        }
        if self.execution_base_sha is not None:
            d["execution_base_sha"] = self.execution_base_sha
        if self.control_source_sha is not None:
            d["control_source_sha"] = self.control_source_sha
        if self.evidence_id is not None:
            d["evidence_id"] = self.evidence_id
        if self.errors:
            d["errors"] = self.errors
        if self.extensions:
            d["extensions"] = self.extensions
        return d

    def __repr__(self) -> str:
        return f"VerificationResult(project={self.project_id!r}, task={self.task_id!r}, gate={self.gate!r}, disposition={self.disposition!r}, closure={self.authorizes_canonical_closure})"


def _compute_file_sha256(path: Path) -> str:
    """Compute sha256 hex digest of file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class IndependentVerifier:
    """Deterministic independent verifier boundary for AOS ControlledExecutionResult.

    Consumes existing execution result and evidence metadata without executing or mutating the worker task.
    Produces PASS or HOLD verification output, fails closed on malformed or contradictory evidence,
    and prevents executor results alone from authorizing canonical closure.
    """

    def __init__(self, verifier_identity: str = "AOS-4 Independent Verifier"):
        self.verifier_identity = verifier_identity

    def verify(
        self,
        execution_result: Any,
        task: Optional[Dict[str, Any]] = None,
        descriptor: Optional[Dict[str, Any]] = None,
        evidence: Optional[Dict[str, Any]] = None,
        snapshot: Optional[Dict[str, Any]] = None,
        candidate_store_dir: Optional[Union[str, Path]] = None,
        artifact_root: Optional[Union[str, Path]] = None,
        check_artifacts: bool = True,
    ) -> VerificationResult:
        """Deterministically verify execution result and evidence in two phases:
        Phase A: Structural & Canonical Input Validation (fail closed on malformed caller structures)
        Phase B: Semantic Verification (fail closed with boundary check on unexpected errors)
        """
        checks: List[VerificationCheck] = []
        errors: List[str] = []

        # =========================================================================
        # PHASE A — STRUCTURAL / CANONICAL INPUT VALIDATION
        # =========================================================================

        # 1. Type validation of execution_result
        if not isinstance(execution_result, dict):
            errors.append("Execution result must be a dictionary object")
            checks.append(VerificationCheck("execution_result_schema", "FAIL", "Execution result is not a dict"))
            return VerificationResult(
                project_id="unknown",
                task_id="unknown",
                gate="unknown",
                disposition="HOLD",
                verifier=self.verifier_identity,
                checks=checks,
                errors=errors,
                authorizes_canonical_closure=False,
            )

        # 2. Check Independent Context Completeness (Section 3)
        missing_context: List[str] = []
        if task is None or not isinstance(task, dict):
            missing_context.append("task")
        if descriptor is None or not isinstance(descriptor, dict):
            missing_context.append("descriptor")
        if evidence is None or not isinstance(evidence, dict):
            missing_context.append("evidence")
        if snapshot is None or not isinstance(snapshot, dict):
            missing_context.append("snapshot")

        if missing_context:
            msg = f"Missing mandatory independent context categories: {', '.join(missing_context)}"
            errors.append(msg)
            checks.append(VerificationCheck("independent_context_complete", "FAIL", msg))

        # 3. Check Artifact Verification Policy (Section 8)
        if not check_artifacts:
            msg = "Artifact verification policy disabled (check_artifacts=False); canonical closure forbidden"
            errors.append(msg)
            checks.append(VerificationCheck("artifact_verification_policy", "FAIL", msg))
        else:
            checks.append(VerificationCheck("artifact_verification_policy", "PASS"))

        # 4. Schema Validation: Controlled Execution Result
        exec_val = validate_document("controlled_execution_result", execution_result)
        if not exec_val.is_valid:
            err_msg = "; ".join(e.message for e in exec_val.errors)
            errors.append(f"ControlledExecutionResult schema validation failed: {err_msg}")
            checks.append(VerificationCheck("execution_result_schema", "FAIL", f"Schema invalid: {err_msg}"))
        else:
            checks.append(VerificationCheck("execution_result_schema", "PASS"))

        # 5. Schema Validation: Task (if provided)
        if task is not None:
            if not isinstance(task, dict):
                errors.append("Task input must be a dictionary object")
                checks.append(VerificationCheck("task_schema", "FAIL", "Task is not a dict"))
            else:
                task_val = validate_document("task", task)
                if not task_val.is_valid:
                    err_msg = "; ".join(e.message for e in task_val.errors)
                    errors.append(f"Task schema validation failed: {err_msg}")
                    checks.append(VerificationCheck("task_schema", "FAIL", f"Schema invalid: {err_msg}"))
                else:
                    checks.append(VerificationCheck("task_schema", "PASS"))

        # 6. Schema Validation: Project Descriptor (if provided)
        if descriptor is not None:
            if not isinstance(descriptor, dict):
                errors.append("Project descriptor input must be a dictionary object")
                checks.append(VerificationCheck("descriptor_schema", "FAIL", "Descriptor is not a dict"))
            else:
                desc_val = validate_document("project_descriptor", descriptor)
                if not desc_val.is_valid:
                    err_msg = "; ".join(e.message for e in desc_val.errors)
                    errors.append(f"Project descriptor schema validation failed: {err_msg}")
                    checks.append(VerificationCheck("descriptor_schema", "FAIL", f"Schema invalid: {err_msg}"))
                else:
                    checks.append(VerificationCheck("descriptor_schema", "PASS"))

        # 7. Schema Validation: Evidence (if provided)
        if evidence is not None:
            if not isinstance(evidence, dict):
                errors.append("Evidence input must be a dictionary object")
                checks.append(VerificationCheck("evidence_schema", "FAIL", "Evidence is not a dict"))
            else:
                ev_val = validate_document("evidence", evidence)
                if not ev_val.is_valid:
                    err_msg = "; ".join(e.message for e in ev_val.errors)
                    errors.append(f"Evidence schema validation failed: {err_msg}")
                    checks.append(VerificationCheck("evidence_schema", "FAIL", f"Schema invalid: {err_msg}"))
                else:
                    checks.append(VerificationCheck("evidence_schema", "PASS"))

        # 8. Schema Validation: Canonical Project Snapshot (if provided)
        if snapshot is not None:
            if not isinstance(snapshot, dict):
                errors.append("Canonical project snapshot input must be a dictionary object")
                checks.append(VerificationCheck("snapshot_schema", "FAIL", "Snapshot is not a dict"))
            else:
                snap_val = validate_document("canonical_project_snapshot", snapshot)
                if not snap_val.is_valid:
                    err_msg = "; ".join(e.message for e in snap_val.errors)
                    errors.append(f"Canonical project snapshot schema validation failed: {err_msg}")
                    checks.append(VerificationCheck("snapshot_schema", "FAIL", f"Schema invalid: {err_msg}"))
                else:
                    checks.append(VerificationCheck("snapshot_schema", "PASS"))

        project_id = str(execution_result.get("project_id") or (task.get("project_id") if isinstance(task, dict) else "unknown"))
        task_id = str(execution_result.get("task_id") or (task.get("task_id") if isinstance(task, dict) else "unknown"))
        gate = str(execution_result.get("gate") or (task.get("gate") if isinstance(task, dict) else "unknown"))
        exec_base_sha = execution_result.get("execution_base_sha")
        control_source_sha = execution_result.get("control_source_sha")
        evidence_id = evidence.get("evidence_id") if isinstance(evidence, dict) else None

        # If any Phase A check failed, return HOLD immediately without proceeding to unsafe dereferences
        if any(c.status == "FAIL" for c in checks) or len(errors) > 0:
            return VerificationResult(
                project_id=project_id,
                task_id=task_id,
                gate=gate,
                disposition="HOLD",
                verifier=self.verifier_identity,
                checks=checks,
                errors=errors,
                authorizes_canonical_closure=False,
                execution_base_sha=exec_base_sha if isinstance(exec_base_sha, str) else None,
                control_source_sha=control_source_sha if isinstance(control_source_sha, str) else None,
                evidence_id=evidence_id,
            )

        # =========================================================================
        # PHASE B — SEMANTIC VERIFICATION (Defensive Exception Wrapped)
        # =========================================================================
        try:
            # 1. Execution Disposition and Worker Status Checks
            exec_disposition = execution_result.get("disposition")
            timed_out = execution_result.get("worker_timed_out", False)
            exit_code = execution_result.get("worker_exit_code")
            result_errors = execution_result.get("errors", [])

            if exec_disposition != "VERIFIED_CANDIDATE":
                msg = f"Execution result disposition '{exec_disposition}' is not VERIFIED_CANDIDATE"
                errors.append(msg)
                checks.append(VerificationCheck("execution_disposition", "FAIL", msg))
            elif timed_out:
                msg = "Worker timed out during controlled execution"
                errors.append(msg)
                checks.append(VerificationCheck("execution_disposition", "FAIL", msg))
            elif exit_code not in (0, None):
                msg = f"Worker exited with non-zero exit code: {exit_code}"
                errors.append(msg)
                checks.append(VerificationCheck("execution_disposition", "FAIL", msg))
            elif result_errors:
                msg = f"Execution result contains unhandled errors: {'; '.join(str(e) for e in result_errors)}"
                errors.append(msg)
                checks.append(VerificationCheck("execution_disposition", "FAIL", msg))
            else:
                checks.append(VerificationCheck("execution_disposition", "PASS"))

            # 2. Scope Guard Verification
            scope_val = execution_result.get("scope_validation")
            scope_failed = False
            if not isinstance(scope_val, dict):
                scope_failed = True
                errors.append("Execution result scope_validation is not a dictionary")
            else:
                scope_is_valid = scope_val.get("is_valid", False)
                violations = scope_val.get("violations", [])
                if not isinstance(violations, list):
                    violations = [str(violations)]
                if not scope_is_valid:
                    scope_failed = True
                    errors.append("Scope validation reported is_valid=False")
                if violations:
                    scope_failed = True
                    errors.append(f"Scope violations reported: {'; '.join(str(v) for v in violations)}")

            changed_paths = execution_result.get("changed_paths", [])
            if not isinstance(changed_paths, list):
                scope_failed = True
                errors.append("Execution result changed_paths must be a list")

            # Check changed_paths against task allowed_scope
            if isinstance(task, dict) and isinstance(changed_paths, list):
                allowed_scope_def = task.get("allowed_scope")
                if not isinstance(allowed_scope_def, dict):
                    scope_failed = True
                    errors.append("Task allowed_scope is not a dictionary")
                else:
                    allowed_paths = allowed_scope_def.get("paths", [])
                    forbidden_paths = allowed_scope_def.get("forbidden_paths", [])
                    if not isinstance(allowed_paths, list):
                        allowed_paths = []
                    if not isinstance(forbidden_paths, list):
                        forbidden_paths = []

                    for p in changed_paths:
                        if not isinstance(p, str):
                            scope_failed = True
                            errors.append(f"Invalid changed path type: {p!r}")
                            continue

                        # Check forbidden
                        for fp in forbidden_paths:
                            if isinstance(fp, str):
                                if fp.endswith("/") and (p.startswith(fp) or p + "/" == fp):
                                    scope_failed = True
                                    errors.append(f"Changed path '{p}' matches forbidden scope '{fp}'")
                                elif p == fp:
                                    scope_failed = True
                                    errors.append(f"Changed path '{p}' is explicitly forbidden")

                        # Check allowed
                        matched = False
                        for ap in allowed_paths:
                            if isinstance(ap, str):
                                if ap.endswith("/") and (p.startswith(ap) or p + "/" == ap):
                                    matched = True
                                    break
                                elif p == ap:
                                    matched = True
                                    break
                        if not matched:
                            scope_failed = True
                            errors.append(f"Changed path '{p}' is outside permitted allowed_scope.paths")

            if scope_failed:
                checks.append(VerificationCheck("scope_guard_verification", "FAIL", "Scope validation failed"))
            else:
                checks.append(VerificationCheck("scope_guard_verification", "PASS"))

            # 3. Required Checks Accounting
            req_checks_failed = False
            check_list = execution_result.get("verification_checks", [])
            if not isinstance(check_list, list):
                req_checks_failed = True
                errors.append("Execution result verification_checks must be a list")
                check_list = []

            check_status_map: Dict[str, str] = {}
            for c in check_list:
                if isinstance(c, dict) and "check_id" in c and "status" in c:
                    cid = str(c["check_id"])
                    cst = str(c["status"])
                    check_status_map[cid] = cst
                    if cid.startswith("project:"):
                        check_status_map[cid[len("project:"):]] = cst

            # Extract required checks from task or descriptor
            required_check_ids: List[str] = []
            if isinstance(task, dict):
                ev_reqs = task.get("evidence_requirements")
                if isinstance(ev_reqs, dict):
                    rc = ev_reqs.get("required_checks", [])
                    if isinstance(rc, list):
                        required_check_ids.extend([str(x) for x in rc])
            if isinstance(descriptor, dict) and not required_check_ids:
                v_def = descriptor.get("verification")
                if isinstance(v_def, dict):
                    desc_checks = v_def.get("checks")
                    if isinstance(desc_checks, dict):
                        required_check_ids.extend([str(k) for k in desc_checks.keys()])

            if required_check_ids:
                for rcid in required_check_ids:
                    if rcid not in check_status_map:
                        req_checks_failed = True
                        errors.append(f"Required check '{rcid}' is missing from execution result verification_checks")
                    elif check_status_map[rcid] != "PASS":
                        req_checks_failed = True
                        errors.append(f"Required check '{rcid}' status is '{check_status_map[rcid]}' (expected PASS)")

            # Ensure all executed checks in execution_result passed
            for c in check_list:
                if isinstance(c, dict):
                    cid = c.get("check_id", "")
                    cst = c.get("status", "")
                    if cst == "FAIL":
                        req_checks_failed = True
                        errors.append(f"Verification check '{cid}' failed in execution result: {c.get('message', 'FAIL')}")

            if req_checks_failed:
                checks.append(VerificationCheck("required_checks_accounting", "FAIL", "Required checks accounting failed"))
            else:
                checks.append(VerificationCheck("required_checks_accounting", "PASS"))

            # 4. Revision & Exact SHA Consistency
            rev_failed = False
            if exec_base_sha is not None and (not isinstance(exec_base_sha, str) or not SHA_REGEX.match(exec_base_sha)):
                rev_failed = True
                errors.append(f"Execution base SHA is malformed: '{exec_base_sha}'")

            if control_source_sha is not None and (not isinstance(control_source_sha, str) or not SHA_REGEX.match(control_source_sha)):
                rev_failed = True
                errors.append(f"Control source SHA is malformed: '{control_source_sha}'")

            if isinstance(task, dict):
                t_base = task.get("base_sha")
                if t_base and exec_base_sha and t_base != exec_base_sha:
                    rev_failed = True
                    errors.append(f"Task base_sha '{t_base}' != execution_base_sha '{exec_base_sha}'")
                if task.get("project_id") and task.get("project_id") != project_id:
                    rev_failed = True
                    errors.append(f"Task project_id '{task.get('project_id')}' != execution result project_id '{project_id}'")
                if task.get("task_id") and task.get("task_id") != task_id:
                    rev_failed = True
                    errors.append(f"Task task_id '{task.get('task_id')}' != execution result task_id '{task_id}'")
                if task.get("gate") and task.get("gate") != gate:
                    rev_failed = True
                    errors.append(f"Task gate '{task.get('gate')}' != execution result gate '{gate}'")

            if isinstance(descriptor, dict):
                d_pid = descriptor.get("project_id")
                if d_pid and d_pid != project_id:
                    rev_failed = True
                    errors.append(f"Descriptor project_id '{d_pid}' != execution result project_id '{project_id}'")

            if isinstance(snapshot, dict):
                s_sha = snapshot.get("source_sha")
                if s_sha and control_source_sha and s_sha != control_source_sha:
                    rev_failed = True
                    errors.append(f"Snapshot source_sha '{s_sha}' != execution result control_source_sha '{control_source_sha}'")
                s_exec = snapshot.get("next_action_execution_base_sha")
                if s_exec and exec_base_sha and s_exec != exec_base_sha:
                    rev_failed = True
                    errors.append(f"Snapshot execution base '{s_exec}' != execution result execution_base_sha '{exec_base_sha}'")
                if snapshot.get("project_id") and snapshot.get("project_id") != project_id:
                    rev_failed = True
                    errors.append(f"Snapshot project_id '{snapshot.get('project_id')}' != execution result project_id '{project_id}'")
                if snapshot.get("current_milestone") and snapshot.get("current_milestone") != gate:
                    rev_failed = True
                    errors.append(f"Snapshot current_milestone '{snapshot.get('current_milestone')}' != execution result gate '{gate}'")
                
                amb_reasons = snapshot.get("ambiguity_reasons", [])
                if not isinstance(amb_reasons, list):
                    amb_reasons = [str(amb_reasons)]
                if snapshot.get("has_ambiguity") or amb_reasons:
                    rev_failed = True
                    errors.append(f"Canonical snapshot has ambiguity: {'; '.join(str(r) for r in amb_reasons)}")

            if rev_failed:
                checks.append(VerificationCheck("revision_integrity", "FAIL", "Revision integrity check failed"))
            else:
                checks.append(VerificationCheck("revision_integrity", "PASS"))

            # 5. Evidence Consistency & Contradiction Defense (Section 10)
            ev_failed = False
            if isinstance(evidence, dict):
                ev_result = evidence.get("result")
                # Require evidence.result MUST be EXACTLY "PASS" for closure
                if ev_result != "PASS":
                    ev_failed = True
                    errors.append(f"Evidence result is '{ev_result}' (expected PASS)")

                if ev_result in ("HOLD", "FAIL", "WORKER_FAILED", "VERIFICATION_FAILED") and exec_disposition == "VERIFIED_CANDIDATE":
                    ev_failed = True
                    errors.append(f"Contradictory evidence: evidence result is '{ev_result}' while execution disposition is VERIFIED_CANDIDATE")

                # Check evidence level against task requirement
                if isinstance(task, dict):
                    ev_reqs = task.get("evidence_requirements")
                    if isinstance(ev_reqs, dict):
                        min_level = ev_reqs.get("minimum_level")
                        ev_level = evidence.get("evidence_level")
                        if min_level and ev_level:
                            min_rank = EVIDENCE_LEVEL_RANK.get(min_level, 0)
                            ev_rank = EVIDENCE_LEVEL_RANK.get(ev_level, 0)
                            if ev_rank < min_rank:
                                ev_failed = True
                                errors.append(f"Evidence level '{ev_level}' ({ev_rank}) is lower than task required minimum_level '{min_level}' ({min_rank})")

                # Check evidence revisions
                ev_rev = evidence.get("revision") or evidence.get("revisions")
                if isinstance(ev_rev, dict):
                    ev_base = ev_rev.get("base_sha")
                    if ev_base and exec_base_sha and ev_base != exec_base_sha:
                        ev_failed = True
                        errors.append(f"Evidence base_sha '{ev_base}' != execution_base_sha '{exec_base_sha}'")

                ev_proj = str(evidence.get("project", "")).lower()
                if ev_proj and project_id and ev_proj != project_id.lower():
                    ev_failed = True
                    errors.append(f"Evidence project '{evidence.get('project')}' != execution result project_id '{project_id}'")

                ev_gate = evidence.get("gate")
                if ev_gate and gate and ev_gate != gate:
                    ev_failed = True
                    errors.append(f"Evidence gate '{ev_gate}' != execution result gate '{gate}'")

            if ev_failed:
                checks.append(VerificationCheck("evidence_consistency", "FAIL", "Evidence consistency check failed"))
            else:
                checks.append(VerificationCheck("evidence_consistency", "PASS"))

            # 6. Candidate Store & Physical Manifest Binding Integrity (Stage 10AE-R2 Hardened)
            art_failed = False
            if exec_disposition == "VERIFIED_CANDIDATE":
                ext = execution_result.get("extensions")
                if not isinstance(ext, dict):
                    art_failed = True
                    errors.append("Execution result extensions must be a dictionary object")
                    cand_ext = None
                else:
                    cand_ext = ext.get("candidate")
                    if not isinstance(cand_ext, dict):
                        art_failed = True
                        errors.append("Execution result extensions.candidate must be a dictionary object")

                if isinstance(cand_ext, dict):
                    cand_status = cand_ext.get("status")
                    if cand_status != "PERSISTED":
                        art_failed = True
                        errors.append(f"Candidate store status is '{cand_status}' (expected PERSISTED)")

                    if cand_ext.get("candidate_store_contract_version") != "0.1.0":
                        art_failed = True
                        errors.append(f"Candidate store contract version is '{cand_ext.get('candidate_store_contract_version')}' (expected 0.1.0)")

                    manifest_sha = cand_ext.get("manifest_sha256")
                    if not manifest_sha or not SHA256_REGEX.match(manifest_sha):
                        art_failed = True
                        errors.append(f"Candidate manifest SHA256 is invalid or malformed: '{manifest_sha}'")

                    cand_id = cand_ext.get("candidate_id")
                    if not cand_id or not isinstance(cand_id, str) or not CANDIDATE_ID_REGEX.match(cand_id):
                        art_failed = True
                        errors.append(f"Candidate ID '{cand_id}' is non-canonical or malformed")

                    # Candidate extension binding to execution result
                    cand_exec_base = cand_ext.get("execution_base_sha")
                    if cand_exec_base != exec_base_sha:
                        art_failed = True
                        errors.append(f"Candidate extension execution_base_sha '{cand_exec_base}' != execution_result '{exec_base_sha}'")

                    cand_ctrl_sha = cand_ext.get("control_source_sha")
                    if cand_ctrl_sha != control_source_sha:
                        art_failed = True
                        errors.append(f"Candidate extension control_source_sha '{cand_ctrl_sha}' != execution_result '{control_source_sha}'")

                    cand_paths = cand_ext.get("changed_paths")
                    if cand_paths != changed_paths:
                        art_failed = True
                        errors.append(f"Candidate extension changed_paths '{cand_paths}' != execution_result changed_paths '{changed_paths}'")

                    # Physical Candidate Store & Manifest Verification
                    if check_artifacts:
                        if not candidate_store_dir:
                            art_failed = True
                            errors.append("Candidate status is PERSISTED but candidate_store_dir is missing")
                        else:
                            store_root = Path(candidate_store_dir).resolve()
                            cand_path = (store_root / str(cand_id)).resolve()
                            
                            # Containment check
                            try:
                                if not cand_path.is_relative_to(store_root):
                                    art_failed = True
                                    errors.append(f"Candidate path '{cand_path}' escapes store_root '{store_root}'")
                            except AttributeError:
                                try:
                                    cand_path.relative_to(store_root)
                                except ValueError:
                                    art_failed = True
                                    errors.append(f"Candidate path '{cand_path}' escapes store_root '{store_root}'")

                            if not cand_path.exists() or not cand_path.is_dir():
                                art_failed = True
                                errors.append(f"Candidate directory missing or not a directory: '{cand_path}'")
                            else:
                                manifest_file = cand_path / "manifest.json"
                                if not manifest_file.exists() or not manifest_file.is_file() or manifest_file.is_symlink():
                                    art_failed = True
                                    errors.append(f"Candidate manifest.json missing, not regular file, or direct symlink at '{manifest_file}'")
                                else:
                                    actual_manifest_sha = _compute_file_sha256(manifest_file)
                                    if actual_manifest_sha != manifest_sha:
                                        art_failed = True
                                        errors.append(f"Candidate manifest SHA mismatch: on-disk '{actual_manifest_sha}' != claimed '{manifest_sha}'")
                                    else:
                                        # Strict JSON parse of physical manifest
                                        try:
                                            manifest_data = load_json_strict(manifest_file)
                                        except Exception as ex:
                                            art_failed = True
                                            errors.append(f"Failed to strictly parse physical manifest JSON: {ex}")
                                            manifest_data = None

                                        if isinstance(manifest_data, dict):
                                            if manifest_data.get("schema_version") != "0.1.0":
                                                art_failed = True
                                                errors.append(f"Manifest schema_version '{manifest_data.get('schema_version')}' != 0.1.0")
                                            if manifest_data.get("candidate_store_contract_version") != "0.1.0":
                                                art_failed = True
                                                errors.append(f"Manifest candidate_store_contract_version '{manifest_data.get('candidate_store_contract_version')}' != 0.1.0")
                                            if manifest_data.get("candidate_id") != cand_id:
                                                art_failed = True
                                                errors.append(f"Manifest candidate_id '{manifest_data.get('candidate_id')}' != '{cand_id}'")
                                            if manifest_data.get("project_id") != project_id:
                                                art_failed = True
                                                errors.append(f"Manifest project_id '{manifest_data.get('project_id')}' != '{project_id}'")
                                            if manifest_data.get("task_id") != task_id:
                                                art_failed = True
                                                errors.append(f"Manifest task_id '{manifest_data.get('task_id')}' != '{task_id}'")
                                            if manifest_data.get("gate") != gate:
                                                art_failed = True
                                                errors.append(f"Manifest gate '{manifest_data.get('gate')}' != '{gate}'")
                                            if manifest_data.get("control_source_sha") != control_source_sha:
                                                art_failed = True
                                                errors.append(f"Manifest control_source_sha '{manifest_data.get('control_source_sha')}' != '{control_source_sha}'")
                                            if manifest_data.get("execution_base_sha") != exec_base_sha:
                                                art_failed = True
                                                errors.append(f"Manifest execution_base_sha '{manifest_data.get('execution_base_sha')}' != '{exec_base_sha}'")
                                            if manifest_data.get("worker_branch") != execution_result.get("worker_branch"):
                                                art_failed = True
                                                errors.append("Manifest worker_branch mismatch")
                                            if manifest_data.get("initial_head_sha") != execution_result.get("initial_head_sha"):
                                                art_failed = True
                                                errors.append("Manifest initial_head_sha mismatch")
                                            if manifest_data.get("final_head_sha") != execution_result.get("final_head_sha"):
                                                art_failed = True
                                                errors.append("Manifest final_head_sha mismatch")

                                            m_changed = manifest_data.get("changed_paths")
                                            if not isinstance(m_changed, list):
                                                art_failed = True
                                                errors.append("Manifest changed_paths must be a list")
                                                m_changed = []

                                            m_path_names = []
                                            for cp in m_changed:
                                                if isinstance(cp, dict) and isinstance(cp.get("path"), str):
                                                    m_path_names.append(cp["path"])

                                            if m_path_names != changed_paths:
                                                art_failed = True
                                                errors.append(f"Manifest changed paths '{m_path_names}' != execution_result changed_paths '{changed_paths}'")

                                            # Physical Candidate Workspace File Byte & State Verification (Section 3 & 4)
                                            workspace_dir = cand_path / "workspace"
                                            for cp in m_changed:
                                                if not isinstance(cp, dict):
                                                    art_failed = True
                                                    errors.append(f"Manifest changed_paths record is not a dictionary: {cp!r}")
                                                    continue

                                                rel_p_str = cp.get("path")
                                                if not rel_p_str or not isinstance(rel_p_str, str) or not rel_p_str.strip():
                                                    art_failed = True
                                                    errors.append(f"Invalid or empty manifest changed path string: {rel_p_str!r}")
                                                    continue

                                                rel_path_obj = Path(rel_p_str)
                                                if rel_path_obj.is_absolute() or ".." in rel_p_str.replace("\\", "/").split("/"):
                                                    art_failed = True
                                                    errors.append(f"Manifest path '{rel_p_str}' is absolute or contains traversal")
                                                    continue

                                                target_ws_path = (workspace_dir / rel_path_obj).resolve()
                                                try:
                                                    if not target_ws_path.is_relative_to(workspace_dir.resolve()):
                                                        art_failed = True
                                                        errors.append(f"Manifest path '{rel_p_str}' escapes workspace")
                                                        continue
                                                except AttributeError:
                                                    try:
                                                        target_ws_path.relative_to(workspace_dir.resolve())
                                                    except ValueError:
                                                        art_failed = True
                                                        errors.append(f"Manifest path '{rel_p_str}' escapes workspace")
                                                        continue

                                                if target_ws_path.is_symlink():
                                                    art_failed = True
                                                    errors.append(f"Manifest path '{rel_p_str}' is a symlink")
                                                    continue

                                                m_st = cp.get("state")
                                                if m_st == "PRESENT":
                                                    sz = cp.get("size_bytes")
                                                    sh = cp.get("sha256")
                                                    if not isinstance(sz, int) or isinstance(sz, bool) or sz < 0:
                                                        art_failed = True
                                                        errors.append(f"Manifest PRESENT record size_bytes invalid for '{rel_p_str}': {sz!r}")
                                                    elif not isinstance(sh, str) or not SHA256_REGEX.match(sh):
                                                        art_failed = True
                                                        errors.append(f"Manifest PRESENT record sha256 invalid for '{rel_p_str}': {sh!r}")
                                                    else:
                                                        if not target_ws_path.exists() or not target_ws_path.is_file():
                                                            art_failed = True
                                                            errors.append(f"Candidate file missing or not a regular file: '{rel_p_str}'")
                                                        else:
                                                            act_sz = len(target_ws_path.read_bytes())
                                                            act_hash = _compute_file_sha256(target_ws_path)
                                                            if act_sz != sz:
                                                                art_failed = True
                                                                errors.append(f"Candidate file size mismatch for '{rel_p_str}': actual {act_sz} != manifest {sz}")
                                                            if act_hash != sh:
                                                                art_failed = True
                                                                errors.append(f"Candidate file sha256 mismatch for '{rel_p_str}': actual {act_hash} != manifest {sh}")
                                                elif m_st == "DELETED":
                                                    if target_ws_path.exists():
                                                        art_failed = True
                                                        errors.append(f"Candidate DELETED file actually exists: '{rel_p_str}'")
                                                else:
                                                    art_failed = True
                                                    errors.append(f"Unsupported manifest changed-path state '{m_st}' for '{rel_p_str}' (expected PRESENT or DELETED)")

            if art_failed:
                checks.append(VerificationCheck("candidate_and_artifact_integrity", "FAIL", "Candidate/artifact integrity check failed"))
            else:
                checks.append(VerificationCheck("candidate_and_artifact_integrity", "PASS"))

            # 7. Declared Evidence Artifact Integrity
            ev_art_failed = False
            if check_artifacts and isinstance(evidence, dict):
                declared_artifacts = evidence.get("artifacts", [])
                if not isinstance(declared_artifacts, list):
                    ev_art_failed = True
                    errors.append("Evidence artifacts must be a list")
                    declared_artifacts = []

                if declared_artifacts:
                    if artifact_root is None:
                        ev_art_failed = True
                        errors.append("Evidence declares artifacts but artifact_root is missing")
                    else:
                        root_path = Path(artifact_root).resolve()
                        for art_entry in declared_artifacts:
                            if not isinstance(art_entry, str) or not art_entry.strip():
                                ev_art_failed = True
                                errors.append(f"Invalid declared artifact entry: {art_entry!r}")
                                continue

                            art_str = art_entry.strip()
                            art_path_obj = Path(art_str)
                            if art_path_obj.is_absolute():
                                ev_art_failed = True
                                errors.append(f"Absolute artifact path forbidden: '{art_str}'")
                                continue

                            if ".." in art_str.replace("\\", "/").split("/"):
                                ev_art_failed = True
                                errors.append(f"Artifact path traversal ('..') forbidden: '{art_str}'")
                                continue

                            target_path = (root_path / art_path_obj).resolve()
                            try:
                                if not target_path.is_relative_to(root_path):
                                    ev_art_failed = True
                                    errors.append(f"Artifact path '{art_str}' escapes artifact_root '{root_path}'")
                                    continue
                            except AttributeError:
                                try:
                                    target_path.relative_to(root_path)
                                except ValueError:
                                    ev_art_failed = True
                                    errors.append(f"Artifact path '{art_str}' escapes artifact_root '{root_path}'")
                                    continue

                            if target_path.is_symlink():
                                ev_art_failed = True
                                errors.append(f"Artifact path '{art_str}' is a symlink")
                                continue

                            if not target_path.exists() or not target_path.is_file():
                                ev_art_failed = True
                                errors.append(f"Declared evidence artifact absent or not a file: '{art_str}' at '{target_path}'")
                                continue

            if ev_art_failed:
                checks.append(VerificationCheck("evidence_artifact_integrity", "FAIL", "Declared evidence artifact integrity failed"))
            else:
                checks.append(VerificationCheck("evidence_artifact_integrity", "PASS"))

        except Exception as ex:
            # Defense-in-depth exception fail-closed boundary
            err_msg = f"Unexpected Exception during semantic verification: {ex.__class__.__name__}"
            errors.append(err_msg)
            checks.append(VerificationCheck("verifier_fail_closed_boundary", "FAIL", err_msg))

        # 8. Closure Authority Evaluation
        all_checks_passed = all(c.status == "PASS" for c in checks) and len(errors) == 0

        if all_checks_passed:
            disposition = "PASS"
            authorizes_closure = True
            checks.append(VerificationCheck("closure_authority_rule", "PASS", "Independent verification passed; canonical closure authorized"))
        else:
            disposition = "HOLD"
            authorizes_closure = False
            checks.append(VerificationCheck("closure_authority_rule", "FAIL", "Verification checks failed or held; canonical closure forbidden"))

        return VerificationResult(
            project_id=project_id,
            task_id=task_id,
            gate=gate,
            disposition=disposition,
            verifier=self.verifier_identity,
            checks=checks,
            errors=errors,
            authorizes_canonical_closure=authorizes_closure,
            execution_base_sha=exec_base_sha if isinstance(exec_base_sha, str) else None,
            control_source_sha=control_source_sha if isinstance(control_source_sha, str) else None,
            evidence_id=evidence_id,
        )


def verify_controlled_execution(
    execution_result: Dict[str, Any],
    task: Optional[Dict[str, Any]] = None,
    descriptor: Optional[Dict[str, Any]] = None,
    evidence: Optional[Dict[str, Any]] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    candidate_store_dir: Optional[Union[str, Path]] = None,
    artifact_root: Optional[Union[str, Path]] = None,
    check_artifacts: bool = True,
    verifier_identity: str = "AOS-4 Independent Verifier",
) -> VerificationResult:
    """Convenience helper to run deterministic independent verification."""
    verifier = IndependentVerifier(verifier_identity=verifier_identity)
    return verifier.verify(
        execution_result=execution_result,
        task=task,
        descriptor=descriptor,
        evidence=evidence,
        snapshot=snapshot,
        candidate_store_dir=candidate_store_dir,
        artifact_root=artifact_root,
        check_artifacts=check_artifacts,
    )


# Functional aliases
verify_execution = verify_controlled_execution
verify_execution_result = verify_controlled_execution


def validate_verification_result(data: Dict[str, Any]) -> ValidationResult:
    """Validate a VerificationResult dictionary against verification_result schema."""
    return validate_document("verification_result", data)
