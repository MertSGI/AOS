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

from aos.validate import validate_document, ValidationResult

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
        """Deterministically verify execution result and evidence."""
        checks: List[VerificationCheck] = []
        errors: List[str] = []

        # 1. Structural / Type Validation of execution_result
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

        project_id = str(execution_result.get("project_id") or (task.get("project_id") if isinstance(task, dict) else "unknown"))
        task_id = str(execution_result.get("task_id") or (task.get("task_id") if isinstance(task, dict) else "unknown"))
        gate = str(execution_result.get("gate") or (task.get("gate") if isinstance(task, dict) else "unknown"))
        exec_base_sha = execution_result.get("execution_base_sha")
        control_source_sha = execution_result.get("control_source_sha")
        evidence_id = evidence.get("evidence_id") if isinstance(evidence, dict) else None

        # Check Independent Context Completeness (Section 7)
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
        else:
            checks.append(VerificationCheck("independent_context_complete", "PASS"))

        # Check Artifact Verification Policy (Section 8)
        if not check_artifacts:
            msg = "Artifact verification policy disabled (check_artifacts=False); canonical closure forbidden"
            errors.append(msg)
            checks.append(VerificationCheck("artifact_verification_policy", "FAIL", msg))
        else:
            checks.append(VerificationCheck("artifact_verification_policy", "PASS"))

        # Schema Validation: Controlled Execution Result
        exec_val = validate_document("controlled_execution_result", execution_result)
        if not exec_val.is_valid:
            err_msg = "; ".join(e.message for e in exec_val.errors)
            errors.append(f"ControlledExecutionResult schema validation failed: {err_msg}")
            checks.append(VerificationCheck("execution_result_schema", "FAIL", f"Schema invalid: {err_msg}"))
        else:
            checks.append(VerificationCheck("execution_result_schema", "PASS"))

        # Schema Validation: Task (if provided)
        if task is not None and isinstance(task, dict):
            task_val = validate_document("task", task)
            if not task_val.is_valid:
                err_msg = "; ".join(e.message for e in task_val.errors)
                errors.append(f"Task schema validation failed: {err_msg}")
                checks.append(VerificationCheck("task_schema", "FAIL", f"Schema invalid: {err_msg}"))
            else:
                checks.append(VerificationCheck("task_schema", "PASS"))

        # Schema Validation: Project Descriptor (if provided)
        if descriptor is not None and isinstance(descriptor, dict):
            desc_val = validate_document("project_descriptor", descriptor)
            if not desc_val.is_valid:
                err_msg = "; ".join(e.message for e in desc_val.errors)
                errors.append(f"Project descriptor schema validation failed: {err_msg}")
                checks.append(VerificationCheck("descriptor_schema", "FAIL", f"Schema invalid: {err_msg}"))
            else:
                checks.append(VerificationCheck("descriptor_schema", "PASS"))

        # Schema Validation: Evidence (if provided)
        if evidence is not None and isinstance(evidence, dict):
            ev_val = validate_document("evidence", evidence)
            if not ev_val.is_valid:
                err_msg = "; ".join(e.message for e in ev_val.errors)
                errors.append(f"Evidence schema validation failed: {err_msg}")
                checks.append(VerificationCheck("evidence_schema", "FAIL", f"Schema invalid: {err_msg}"))
            else:
                checks.append(VerificationCheck("evidence_schema", "PASS"))

        # Schema Validation: Canonical Project Snapshot (if provided)
        if snapshot is not None and isinstance(snapshot, dict):
            snap_val = validate_document("canonical_project_snapshot", snapshot)
            if not snap_val.is_valid:
                err_msg = "; ".join(e.message for e in snap_val.errors)
                errors.append(f"Canonical project snapshot schema validation failed: {err_msg}")
                checks.append(VerificationCheck("snapshot_schema", "FAIL", f"Schema invalid: {err_msg}"))
            else:
                checks.append(VerificationCheck("snapshot_schema", "PASS"))

        # 2. Execution Disposition and Worker Status Checks
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

        # 3. Scope Guard Verification
        scope_val = execution_result.get("scope_validation", {})
        scope_is_valid = scope_val.get("is_valid", False) if isinstance(scope_val, dict) else False
        violations = scope_val.get("violations", []) if isinstance(scope_val, dict) else []
        changed_paths = execution_result.get("changed_paths", [])

        scope_failed = False
        if not scope_is_valid:
            scope_failed = True
            msg = "Scope validation reported is_valid=False"
            errors.append(msg)
        if violations:
            scope_failed = True
            msg = f"Scope violations reported: {'; '.join(str(v) for v in violations)}"
            errors.append(msg)

        # Check changed_paths against task allowed_scope if task is provided
        if task is not None and isinstance(task, dict):
            allowed_scope_def = task.get("allowed_scope", {})
            allowed_paths = allowed_scope_def.get("paths", [])
            forbidden_paths = allowed_scope_def.get("forbidden_paths", [])

            for p in changed_paths:
                # Check forbidden
                for fp in forbidden_paths:
                    if fp.endswith("/") and (p.startswith(fp) or p + "/" == fp):
                        scope_failed = True
                        msg = f"Changed path '{p}' matches forbidden scope '{fp}'"
                        errors.append(msg)
                    elif p == fp:
                        scope_failed = True
                        msg = f"Changed path '{p}' is explicitly forbidden"
                        errors.append(msg)

                # Check allowed
                matched = False
                for ap in allowed_paths:
                    if ap.endswith("/") and (p.startswith(ap) or p + "/" == ap):
                        matched = True
                        break
                    elif p == ap:
                        matched = True
                        break
                if not matched:
                    scope_failed = True
                    msg = f"Changed path '{p}' is outside permitted allowed_scope.paths"
                    errors.append(msg)

        if scope_failed:
            checks.append(VerificationCheck("scope_guard_verification", "FAIL", "Scope validation failed"))
        else:
            checks.append(VerificationCheck("scope_guard_verification", "PASS"))

        # 4. Required Checks Accounting
        req_checks_failed = False
        check_list = execution_result.get("verification_checks", [])
        check_status_map: Dict[str, str] = {}
        for c in check_list:
            if isinstance(c, dict) and "check_id" in c and "status" in c:
                cid = c["check_id"]
                cst = c["status"]
                check_status_map[cid] = cst
                if cid.startswith("project:"):
                    check_status_map[cid[len("project:"):]] = cst

        # Extract required checks from task or descriptor
        required_check_ids: List[str] = []
        if task is not None and isinstance(task, dict):
            required_check_ids.extend(task.get("evidence_requirements", {}).get("required_checks", []))
        if descriptor is not None and isinstance(descriptor, dict) and not required_check_ids:
            desc_checks = descriptor.get("verification", {}).get("checks", {})
            required_check_ids.extend(list(desc_checks.keys()))

        if required_check_ids:
            for rcid in required_check_ids:
                if rcid not in check_status_map:
                    req_checks_failed = True
                    msg = f"Required check '{rcid}' is missing from execution result verification_checks"
                    errors.append(msg)
                elif check_status_map[rcid] != "PASS":
                    req_checks_failed = True
                    msg = f"Required check '{rcid}' status is '{check_status_map[rcid]}' (expected PASS)"
                    errors.append(msg)

        # Ensure all executed checks in execution_result passed
        for c in check_list:
            if isinstance(c, dict):
                cid = c.get("check_id", "")
                cst = c.get("status", "")
                if cst == "FAIL":
                    req_checks_failed = True
                    msg = f"Verification check '{cid}' failed in execution result: {c.get('message', 'FAIL')}"
                    errors.append(msg)

        if req_checks_failed:
            checks.append(VerificationCheck("required_checks_accounting", "FAIL", "Required checks accounting failed"))
        else:
            checks.append(VerificationCheck("required_checks_accounting", "PASS"))

        # 5. Revision & Exact SHA Consistency
        rev_failed = False
        if exec_base_sha is not None and (not isinstance(exec_base_sha, str) or not SHA_REGEX.match(exec_base_sha)):
            rev_failed = True
            msg = f"Execution base SHA is malformed: '{exec_base_sha}'"
            errors.append(msg)

        if control_source_sha is not None and (not isinstance(control_source_sha, str) or not SHA_REGEX.match(control_source_sha)):
            rev_failed = True
            msg = f"Control source SHA is malformed: '{control_source_sha}'"
            errors.append(msg)

        if task is not None and isinstance(task, dict):
            t_base = task.get("base_sha")
            if t_base and exec_base_sha and t_base != exec_base_sha:
                rev_failed = True
                msg = f"Task base_sha '{t_base}' != execution_base_sha '{exec_base_sha}'"
                errors.append(msg)
            if task.get("project_id") and task.get("project_id") != project_id:
                rev_failed = True
                msg = f"Task project_id '{task.get('project_id')}' != execution result project_id '{project_id}'"
                errors.append(msg)
            if task.get("task_id") and task.get("task_id") != task_id:
                rev_failed = True
                msg = f"Task task_id '{task.get('task_id')}' != execution result task_id '{task_id}'"
                errors.append(msg)
            if task.get("gate") and task.get("gate") != gate:
                rev_failed = True
                msg = f"Task gate '{task.get('gate')}' != execution result gate '{gate}'"
                errors.append(msg)

        if descriptor is not None and isinstance(descriptor, dict):
            d_pid = descriptor.get("project_id")
            if d_pid and d_pid != project_id:
                rev_failed = True
                msg = f"Descriptor project_id '{d_pid}' != execution result project_id '{project_id}'"
                errors.append(msg)

        if snapshot is not None and isinstance(snapshot, dict):
            s_sha = snapshot.get("source_sha")
            if s_sha and control_source_sha and s_sha != control_source_sha:
                rev_failed = True
                msg = f"Snapshot source_sha '{s_sha}' != execution result control_source_sha '{control_source_sha}'"
                errors.append(msg)
            s_exec = snapshot.get("next_action_execution_base_sha")
            if s_exec and exec_base_sha and s_exec != exec_base_sha:
                rev_failed = True
                msg = f"Snapshot execution base '{s_exec}' != execution result execution_base_sha '{exec_base_sha}'"
                errors.append(msg)
            if snapshot.get("project_id") and snapshot.get("project_id") != project_id:
                rev_failed = True
                msg = f"Snapshot project_id '{snapshot.get('project_id')}' != execution result project_id '{project_id}'"
                errors.append(msg)
            if snapshot.get("current_milestone") and snapshot.get("current_milestone") != gate:
                rev_failed = True
                msg = f"Snapshot current_milestone '{snapshot.get('current_milestone')}' != execution result gate '{gate}'"
                errors.append(msg)
            if snapshot.get("has_ambiguity") or snapshot.get("ambiguity_reasons"):
                rev_failed = True
                msg = f"Canonical snapshot has ambiguity: {'; '.join(snapshot.get('ambiguity_reasons', []))}"
                errors.append(msg)

        if rev_failed:
            checks.append(VerificationCheck("revision_integrity", "FAIL", "Revision integrity check failed"))
        else:
            checks.append(VerificationCheck("revision_integrity", "PASS"))

        # 6. Evidence Consistency & Contradiction Defense
        ev_failed = False
        if evidence is not None and isinstance(evidence, dict):
            ev_result = evidence.get("result")
            if ev_result in ("HOLD", "FAIL", "WORKER_FAILED", "VERIFICATION_FAILED") and exec_disposition == "VERIFIED_CANDIDATE":
                ev_failed = True
                msg = f"Contradictory evidence: evidence result is '{ev_result}' while execution disposition is VERIFIED_CANDIDATE"
                errors.append(msg)
            elif ev_result == "PASS" and exec_disposition != "VERIFIED_CANDIDATE":
                ev_failed = True
                msg = f"Contradictory evidence: evidence result is PASS while execution disposition is '{exec_disposition}'"
                errors.append(msg)

            # Check evidence level against task requirement
            if task is not None and isinstance(task, dict):
                min_level = task.get("evidence_requirements", {}).get("minimum_level")
                ev_level = evidence.get("evidence_level")
                if min_level and ev_level:
                    min_rank = EVIDENCE_LEVEL_RANK.get(min_level, 0)
                    ev_rank = EVIDENCE_LEVEL_RANK.get(ev_level, 0)
                    if ev_rank < min_rank:
                        ev_failed = True
                        msg = f"Evidence level '{ev_level}' ({ev_rank}) is lower than task required minimum_level '{min_level}' ({min_rank})"
                        errors.append(msg)

            # Check evidence revisions
            ev_rev = evidence.get("revision") or evidence.get("revisions") or {}
            ev_base = ev_rev.get("base_sha")
            if ev_base and exec_base_sha and ev_base != exec_base_sha:
                ev_failed = True
                msg = f"Evidence base_sha '{ev_base}' != execution_base_sha '{exec_base_sha}'"
                errors.append(msg)

            ev_proj = str(evidence.get("project", "")).lower()
            if ev_proj and project_id and ev_proj != project_id.lower():
                ev_failed = True
                msg = f"Evidence project '{evidence.get('project')}' != execution result project_id '{project_id}'"
                errors.append(msg)

            ev_gate = evidence.get("gate")
            if ev_gate and gate and ev_gate != gate:
                ev_failed = True
                msg = f"Evidence gate '{ev_gate}' != execution result gate '{gate}'"
                errors.append(msg)

        if ev_failed:
            checks.append(VerificationCheck("evidence_consistency", "FAIL", "Evidence consistency check failed"))
        else:
            checks.append(VerificationCheck("evidence_consistency", "PASS"))

        # 7. Candidate Store & Physical Manifest Integrity (Section 9)
        art_failed = False
        cand_ext = execution_result.get("extensions", {}).get("candidate")
        if cand_ext is not None and isinstance(cand_ext, dict):
            cand_status = cand_ext.get("status")
            if cand_status != "PERSISTED":
                art_failed = True
                msg = f"Candidate store status is '{cand_status}' (expected PERSISTED)"
                errors.append(msg)

            manifest_sha = cand_ext.get("manifest_sha256")
            if not manifest_sha or not SHA256_REGEX.match(manifest_sha):
                art_failed = True
                msg = f"Candidate manifest SHA256 is invalid or malformed: '{manifest_sha}'"
                errors.append(msg)

            cand_id = cand_ext.get("candidate_id")
            if check_artifacts:
                if cand_status == "PERSISTED" and not candidate_store_dir:
                    art_failed = True
                    msg = "Candidate status is PERSISTED but candidate_store_dir is missing"
                    errors.append(msg)
                elif candidate_store_dir and cand_id:
                    cand_path = Path(candidate_store_dir) / cand_id
                    if not cand_path.exists() or not cand_path.is_dir():
                        art_failed = True
                        msg = f"Candidate directory missing or not a directory: '{cand_path}'"
                        errors.append(msg)
                    else:
                        manifest_file = cand_path / "manifest.json"
                        if not manifest_file.is_file():
                            art_failed = True
                            msg = f"Candidate manifest.json missing at '{manifest_file}'"
                            errors.append(msg)
                        else:
                            actual_manifest_sha = _compute_file_sha256(manifest_file)
                            if actual_manifest_sha != manifest_sha:
                                art_failed = True
                                msg = f"Candidate manifest SHA mismatch: on-disk '{actual_manifest_sha}' != claimed '{manifest_sha}'"
                                errors.append(msg)

        if art_failed:
            checks.append(VerificationCheck("candidate_and_artifact_integrity", "FAIL", "Candidate/artifact integrity check failed"))
        else:
            checks.append(VerificationCheck("candidate_and_artifact_integrity", "PASS"))

        # 8. Declared Evidence Artifact Integrity (Section 10 & 11)
        ev_art_failed = False
        if check_artifacts and evidence is not None and isinstance(evidence, dict):
            declared_artifacts = evidence.get("artifacts", [])
            if declared_artifacts:
                if artifact_root is None:
                    ev_art_failed = True
                    msg = "Evidence declares artifacts but artifact_root is missing"
                    errors.append(msg)
                else:
                    root_path = Path(artifact_root).resolve()
                    for art_entry in declared_artifacts:
                        if not isinstance(art_entry, str) or not art_entry.strip():
                            ev_art_failed = True
                            msg = f"Invalid declared artifact entry: {art_entry!r}"
                            errors.append(msg)
                            continue
                        
                        art_str = art_entry.strip()
                        art_path_obj = Path(art_str)
                        if art_path_obj.is_absolute():
                            ev_art_failed = True
                            msg = f"Absolute artifact path forbidden: '{art_str}'"
                            errors.append(msg)
                            continue
                        
                        if ".." in art_str.replace("\\", "/").split("/"):
                            ev_art_failed = True
                            msg = f"Artifact path traversal ('..') forbidden: '{art_str}'"
                            errors.append(msg)
                            continue

                        target_path = (root_path / art_path_obj).resolve()
                        try:
                            if not target_path.is_relative_to(root_path):
                                ev_art_failed = True
                                msg = f"Artifact path '{art_str}' escapes artifact_root '{root_path}'"
                                errors.append(msg)
                                continue
                        except AttributeError:
                            # Python < 3.9 fallback for is_relative_to
                            try:
                                target_path.relative_to(root_path)
                            except ValueError:
                                ev_art_failed = True
                                msg = f"Artifact path '{art_str}' escapes artifact_root '{root_path}'"
                                errors.append(msg)
                                continue

                        if not target_path.exists() or not target_path.is_file():
                            ev_art_failed = True
                            msg = f"Declared evidence artifact absent or not a file: '{art_str}' at '{target_path}'"
                            errors.append(msg)
                            continue

        if ev_art_failed:
            checks.append(VerificationCheck("evidence_artifact_integrity", "FAIL", "Declared evidence artifact integrity failed"))
        else:
            checks.append(VerificationCheck("evidence_artifact_integrity", "PASS"))

        # 9. Closure Authority Evaluation
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
            execution_base_sha=exec_base_sha,
            control_source_sha=control_source_sha,
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
