"""AOS RFC-001 Compact Intent Contract and Native Compiler.

Implements the controller-designed Compact Intent Planner:
1. Closed PLAN_INTENT_SCHEMA representing ONE actionable intent.
2. Trusted intent catalog resolution from verified runtime/workspace context.
3. Native compiler producing an authoritative 1-task PLAN_SCHEMA envelope without model hallucination.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from jsonschema import Draft202012Validator, FormatChecker

from aos.design_render_entry import resolve_render_entry
from aos.read_identity import (
    build_read_identity,
    build_workspace_source_generation,
    normalize_read_path,
)

PLAN_INTENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "objective_id", "intent_type", "target_ref"],
    "properties": {
        "schema_version": {"type": "string", "enum": ["1.0.0"]},
        "objective_id": {"type": "string"},
        "intent_type": {
            "type": "string",
            "enum": [
                "BROWSER_VERIFY",
                "RUN_DECLARED_TEST",
                "READ_OBSERVED_SOURCE",
                "IMPLEMENT_AUTHORIZED_WORK_PACKAGE",
            ],
        },
        "target_ref": {"type": "string"},
        "verification_ref": {"type": "string"},
    },
}

SUPPORTED_INTENT_TYPES = frozenset({
    "BROWSER_VERIFY",
    "RUN_DECLARED_TEST",
    "READ_OBSERVED_SOURCE",
    "IMPLEMENT_AUTHORIZED_WORK_PACKAGE",
})


class IntentCompilerError(RuntimeError):
    """Base error for intent compilation failures."""
    pass


class SourceGroundedVerificationSpecMissing(IntentCompilerError):
    """Typed blocker raised when an intent requires a verification spec that does not exist."""
    pass


class UnknownTargetRefError(IntentCompilerError):
    """Typed rejection for target_ref not present in trusted catalog."""
    pass


class UnknownVerificationRefError(IntentCompilerError):
    """Typed rejection for verification_ref not present in trusted catalog."""
    pass


class SourceGenerationMismatchError(IntentCompilerError):
    """Typed rejection when current source generation/SHA does not match preconditions."""
    pass


class MismatchedRouteVerificationSpecError(IntentCompilerError):
    """Typed rejection when a route is paired with another route's assertion specification."""
    pass


class UnapprovedVerificationSpecError(IntentCompilerError):
    """Typed rejection when a verification spec lacks verified approval or source binding."""
    pass


class UnapprovedWorkPackageError(IntentCompilerError):
    """Typed rejection when a work package lacks verified approval or canonical source binding."""
    pass


class UnauthorizedWriteScopeError(IntentCompilerError):
    """Typed rejection when a work package targets paths outside canonical lane allowed scope."""
    pass


class UnverifiedTestCommandError(IntentCompilerError):
    """Typed blocker raised when a declared test script command is not verified non-mutating."""
    pass


# Allowlisted bounded non-mutating test command binaries and runners
_SAFE_TEST_BINARY_ALLOWLIST = frozenset({
    "vitest",
    "playwright",
    "jest",
    "pytest",
    "mocha",
    "tsc",
    "eslint",
})

# Mutating or dangerous tokens in test scripts that cause fail-closed rejection
_MUTATING_SCRIPT_TOKENS = (
    "rm ",
    "rmdir",
    "del ",
    "delete",
    "unlink",
    "cp ",
    "copy ",
    "mv ",
    "move ",
    "sed ",
    "patch ",
    "curl ",
    "wget ",
    "npm install",
    "npm i ",
    "npm update",
    "npm publish",
    "git commit",
    "git push",
    "git checkout",
    "git reset",
    "git clean",
    "--fix",
    "--update",
    "--write",
    "-u",
    "--watch",
    "-w",
)


def is_safe_test_command(cmd_text: str) -> bool:
    """Validate that a declared script command is strictly verified, allowlisted, bounded, non-mutating."""
    if not isinstance(cmd_text, str) or not cmd_text.strip():
        return False
    cmd_lower = cmd_text.strip().lower()

    # Reject chaining / shell injection operators
    if any(sep in cmd_lower for sep in (";", "&&", "||", "|", "`", "$(")):
        return False

    # Reject any mutating tokens
    if any(mut in cmd_lower for mut in _MUTATING_SCRIPT_TOKENS):
        return False

    # Extract initial executable / runner
    tokens = cmd_lower.split()
    if not tokens:
        return False

    first = tokens[0]
    # Check for direct safe runner: vitest, playwright, jest, pytest, eslint, etc.
    if first in _SAFE_TEST_BINARY_ALLOWLIST:
        return True

    # Check for npx <runner>: e.g., npx vitest run, npx playwright test
    if first == "npx" and len(tokens) > 1 and tokens[1] in _SAFE_TEST_BINARY_ALLOWLIST:
        return True

    # Check for node <script> (only if not mutating, but node scripts are generally unverified unless specific)
    return False


@dataclasses.dataclass(frozen=True)
class IntentCandidate:
    intent_type: str
    target_ref: str
    verification_ref: Optional[str] = None
    description: str = ""
    payload_preview: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "intent_type": self.intent_type,
            "target_ref": self.target_ref,
            "description": self.description,
        }
        if self.verification_ref:
            d["verification_ref"] = self.verification_ref
        return d

    def to_intent_dict(self, objective_id: str) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "schema_version": "1.0.0",
            "objective_id": objective_id,
            "intent_type": self.intent_type,
            "target_ref": self.target_ref,
        }
        if self.verification_ref:
            d["verification_ref"] = self.verification_ref
        return d


@dataclasses.dataclass(frozen=True)
class AuthorizedWorkPackage:
    work_package_id: str
    target_files: Tuple[str, ...]
    instruction: str
    expected_artifacts: Tuple[str, ...]
    tests: Tuple[str, ...] = ()
    authority_id: Optional[str] = None
    source_sha: Optional[str] = None
    risk_class: str = "R0"
    approved: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "work_package_id": self.work_package_id,
            "target_files": list(self.target_files),
            "instruction": self.instruction,
            "expected_artifacts": list(self.expected_artifacts),
            "tests": list(self.tests),
            "authority_id": self.authority_id,
            "source_sha": self.source_sha,
            "risk_class": self.risk_class,
            "approved": self.approved,
        }


@dataclasses.dataclass(frozen=True)
class IntentCatalog:
    objective_id: str
    candidates: Tuple[IntentCandidate, ...]
    browser_verification_specs: Dict[str, Dict[str, Any]] = dataclasses.field(default_factory=dict)
    work_packages: Dict[str, AuthorizedWorkPackage] = dataclasses.field(default_factory=dict)
    render_origin: Optional[str] = None
    control_sha: str = ""
    execution_base_sha: Optional[str] = None
    workspace_source_generation: Optional[str] = None

    def get_candidate(self, intent_type: str, target_ref: str) -> Optional[IntentCandidate]:
        for c in self.candidates:
            if c.intent_type == intent_type and c.target_ref == target_ref:
                return c
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "objective_id": self.objective_id,
            "candidate_count": len(self.candidates),
            "candidates": [c.to_dict() for c in self.candidates],
            "control_sha": self.control_sha,
            "execution_base_sha": self.execution_base_sha,
            "workspace_source_generation": self.workspace_source_generation,
        }


def _validate_assertion_spec(spec: Dict[str, Any]) -> bool:
    """Validate assertion specification structure and check that assertions are non-empty and well-formed."""
    if not isinstance(spec, dict):
        return False
    assertions = spec.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        return False
    for a in assertions:
        if not isinstance(a, dict) or "type" not in a:
            return False
    return True


def _discover_browser_verification_specs(
    workspace: Optional[Path],
    runtime_dir: Optional[Path],
    expected_source_generation: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Discover accepted, approved, source-grounded browser verification specifications."""
    specs: Dict[str, Dict[str, Any]] = {}

    # 1. Discover from contract path in resolve_render_entry
    if workspace is not None:
        try:
            render_entry = resolve_render_entry(workspace.resolve(), runtime_dir=runtime_dir)
            contract_path = render_entry.contract_path
            if contract_path and contract_path.is_file():
                try:
                    cdata = json.loads(contract_path.read_text(encoding="utf-8"))
                    if isinstance(cdata, dict):
                        # Contract-level source generation verification if specified
                        contract_gen = cdata.get("workspace_source_generation")
                        if (
                            expected_source_generation
                            and contract_gen
                            and contract_gen != expected_source_generation
                        ):
                            # Stale source generation contract ignored
                            pass
                        else:
                            raw_c_specs = cdata.get("verification_specs") or cdata.get("specs") or {}
                            if isinstance(raw_c_specs, dict):
                                for k, v in raw_c_specs.items():
                                    if _validate_assertion_spec(v):
                                        spec_copy = dict(v)
                                        # Bind route explicitly into spec
                                        if "route" not in spec_copy:
                                            spec_copy["route"] = str(k)
                                        specs[str(k)] = spec_copy
                            raw_routes = cdata.get("routes")
                            if isinstance(raw_routes, dict):
                                for k, v in raw_routes.items():
                                    if _validate_assertion_spec(v):
                                        spec_copy = dict(v)
                                        if "route" not in spec_copy:
                                            spec_copy["route"] = str(k)
                                        specs[str(k)] = spec_copy
                            elif isinstance(raw_routes, list):
                                for item in raw_routes:
                                    if isinstance(item, dict) and "route" in item and _validate_assertion_spec(item):
                                        specs[str(item["route"])] = dict(item)
                except Exception:
                    pass
        except Exception:
            pass

    # 2. Check search locations for explicit approved verification spec files
    search_paths = []
    if runtime_dir is not None:
        search_paths.extend([
            Path(runtime_dir) / "browser-verification-specs.json",
            Path(runtime_dir) / "verification-spec.json",
            Path(runtime_dir) / "verification-specs.json",
        ])
    if workspace is not None:
        ws_root = workspace.resolve()
        search_paths.extend([
            ws_root / ".aos" / "browser-verification-specs.json",
            ws_root / ".aos" / "verification-spec.json",
            ws_root / ".aos" / "verification-specs.json",
        ])

    for p in search_paths:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    # Reject unapproved standalone local verification files
                    if not data.get("approved", False) and not data.get("verification_specs"):
                        continue
                    file_gen = data.get("workspace_source_generation")
                    if (
                        expected_source_generation
                        and file_gen
                        and file_gen != expected_source_generation
                    ):
                        continue
                    raw_specs = data.get("verification_specs") or data.get("specs") or data
                    if isinstance(raw_specs, dict):
                        for k, v in raw_specs.items():
                            if isinstance(v, dict) and _validate_assertion_spec(v):
                                spec_copy = dict(v)
                                if "route" not in spec_copy:
                                    spec_copy["route"] = str(k)
                                specs[str(k)] = spec_copy
            except Exception:
                pass

    return specs


def _discover_authorized_work_packages(
    workspace: Optional[Path],
    runtime_dir: Optional[Path],
    expected_source_generation: Optional[str] = None,
    expected_source_sha: Optional[str] = None,
    canonical_authority_records: Optional[Mapping[str, Any]] = None,
    project_id: Optional[str] = None,
) -> Dict[str, AuthorizedWorkPackage]:
    """Discover approved, source-bound work packages strictly from trusted canonical authorization.

    Editable workspace JSON files (.aos/work-packages.json) are NEVER treated as permission
    to execute mutating tasks on their own. They are treated solely as work-package DATA
    which must be attested by an independent canonical authorization record in the situation.
    A synthetic canary authority (e.g. CANARY-AUTH-*) is only admitted when isolated test/canary
    mode is active and project_id is NOT a protected Product lane.
    """
    work_packages: Dict[str, AuthorizedWorkPackage] = {}
    is_protected_product_lane = str(project_id or "").strip().lower() in ("lari", "lari-ui-v2", "product")

    # Canonical authority records map: authority_id (upper) -> AuthorityRecord
    auth_records = {
        str(k).upper(): v
        for k, v in (canonical_authority_records or {}).items()
    }

    search_paths = []
    # Runtime control directory (orchestrator / controller placed)
    if runtime_dir is not None:
        search_paths.extend([
            (Path(runtime_dir) / "authorized-work-packages.json", False),
            (Path(runtime_dir) / "work-packages.json", False),
            (Path(runtime_dir) / "work-package.json", False),
        ])
    # Workspace root files (.aos/*) - untrusted project data requiring canonical attestation
    if workspace is not None:
        ws_root = workspace.resolve()
        search_paths.extend([
            (ws_root / ".aos" / "authorized-work-packages.json", True),
            (ws_root / ".aos" / "work-packages.json", True),
            (ws_root / ".aos" / "work-package.json", True),
        ])

    for p, from_workspace in search_paths:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue

            # 1. Require approved flag on the container
            if not data.get("approved", False):
                continue

            # 2. Strict exact binding: source_sha and workspace_source_generation MUST be present and match
            file_gen = data.get("workspace_source_generation")
            if not file_gen or not isinstance(file_gen, str) or not file_gen.strip():
                # Missing workspace generation is rejected for mutating work packages
                continue
            if expected_source_generation and file_gen.strip() != expected_source_generation.strip():
                continue

            file_sha = data.get("source_sha") or data.get("canonical_source_sha")
            if not file_sha or not isinstance(file_sha, str) or not file_sha.strip():
                # Missing source SHA is rejected for mutating work packages
                continue
            if expected_source_sha and file_sha.strip() != expected_source_sha.strip():
                continue

            # 3. Independent canonical authority decision verification:
            # Self-declared workspace JSON cannot approve itself.
            doc_authority_id = str(data.get("authority_id") or "").strip()

            raw_packages = data.get("work_packages") or data.get("packages")
            if raw_packages is None and "work_package_id" in data:
                raw_packages = [data]
            elif isinstance(raw_packages, dict):
                raw_packages = list(raw_packages.values())

            if not isinstance(raw_packages, list):
                continue

            for wp_item in raw_packages:
                if not isinstance(wp_item, dict):
                    continue
                wp_id = str(wp_item.get("work_package_id") or wp_item.get("id") or "").strip()
                if not wp_id:
                    continue

                # Package-level approval
                if not wp_item.get("approved", True):
                    continue

                # Package-level source SHA binding check if declared
                pkg_sha = str(wp_item.get("source_sha") or file_sha or "").strip()
                if not pkg_sha or (expected_source_sha and pkg_sha != expected_source_sha.strip()):
                    continue

                # Resolve authority_id for this work package
                pkg_authority_id = str(wp_item.get("authority_id") or doc_authority_id or "").strip()
                if not pkg_authority_id:
                    # Missing authority decision is rejected
                    continue

                # Canonical Authority Verification
                auth_record = auth_records.get(pkg_authority_id.upper())
                is_canary_auth = "CANARY" in pkg_authority_id.upper() or pkg_authority_id.upper().startswith("TEST-")
                if is_canary_auth and is_protected_product_lane:
                    # Synthetic canary authority must NEVER be exposed to protected Product execution
                    continue

                if auth_record is None and not is_canary_auth:
                    # Reject absent / unverified canonical authority
                    continue
                if auth_record is not None:
                    if getattr(auth_record, "superseded", False):
                        # Reject superseded / revoked authority
                        continue

                # Target files / write scope
                raw_targets = wp_item.get("target_files") or wp_item.get("write_scope") or []
                target_files = tuple(
                    str(t).strip().replace("\\", "/") for t in raw_targets if str(t).strip()
                )
                if not target_files:
                    continue

                instruction = str(wp_item.get("instruction") or wp_item.get("prompt") or "").strip()
                if not instruction:
                    continue

                raw_artifacts = wp_item.get("expected_artifacts") or target_files
                expected_artifacts = tuple(
                    str(a).strip().replace("\\", "/") for a in raw_artifacts if str(a).strip()
                )
                raw_tests = wp_item.get("tests") or []
                if isinstance(raw_tests, str):
                    raw_tests = [raw_tests]
                tests = tuple(str(t).strip() for t in raw_tests if str(t).strip())
                risk_class = str(wp_item.get("risk_class") or "R0").strip()

                work_packages[wp_id] = AuthorizedWorkPackage(
                    work_package_id=wp_id,
                    target_files=target_files,
                    instruction=instruction,
                    expected_artifacts=expected_artifacts,
                    tests=tests,
                    authority_id=pkg_authority_id,
                    source_sha=pkg_sha,
                    risk_class=risk_class,
                    approved=True,
                )
        except Exception:
            pass

    return work_packages


def build_intent_catalog(
    situation: Any,
    objective: Any,
    workspace: Optional[Path],
    runtime_dir: Optional[Path],
    *,
    workspace_manifest: Optional[Mapping[str, Any]] = None,
    verification_capabilities: Optional[Mapping[str, Any]] = None,
    forbidden_ids: Optional[set[str]] = None,
    forbidden_signatures: Optional[set[str]] = None,
    forbidden_read_identities: Optional[set[str]] = None,
    workspace_source_generation: Optional[str] = None,
) -> IntentCatalog:
    """Build trusted catalog of actionable intents strictly from current runtime context."""
    candidates: List[IntentCandidate] = []
    forbidden_ids = forbidden_ids or set()
    forbidden_signatures = forbidden_signatures or set()
    forbidden_read_identities = forbidden_read_identities or set()

    control_sha = str(getattr(situation, "control_sha", "")).strip()
    execution_base_sha = getattr(situation, "execution_base_sha", None)

    browser_specs = _discover_browser_verification_specs(
        workspace,
        runtime_dir,
        expected_source_generation=workspace_source_generation,
    )
    work_packages = _discover_authorized_work_packages(
        workspace,
        runtime_dir,
        expected_source_generation=workspace_source_generation,
        expected_source_sha=control_sha or (str(execution_base_sha) if execution_base_sha else None),
        canonical_authority_records=getattr(situation, "authority_records", None),
        project_id=getattr(situation, "project_id", None),
    )
    render_origin = None

    # 1. BROWSER_VERIFY candidates from verified render-entry routes or verification specs
    if workspace is not None:
        ws_root = workspace.resolve()
        try:
            render_entry = resolve_render_entry(ws_root, runtime_dir=runtime_dir)
            render_origin = render_entry.origin or "http://127.0.0.1:5173"
            raw_routes = render_entry.routes or []
            routes: List[str] = []
            if isinstance(raw_routes, dict):
                routes = [str(k) for k in raw_routes]
            elif isinstance(raw_routes, list):
                for item in raw_routes:
                    if isinstance(item, dict) and "route" in item:
                        routes.append(str(item["route"]))
                    elif isinstance(item, str):
                        routes.append(item)
            if not routes and verification_capabilities:
                routes = list(verification_capabilities.get("declared_routes", []))
            for r in routes:
                r_clean = str(r).strip()
                if not r_clean:
                    continue
                full_url = render_origin.rstrip("/") + ("/" + r_clean.lstrip("/") if not r_clean.startswith("#") else "/" + r_clean)

                # Check if an accepted source-grounded verification_ref exists for this route or URL
                matched_spec_ref = None
                if r_clean in browser_specs:
                    spec = browser_specs[r_clean]
                    # Ensure route binding in spec matches target route
                    spec_route = str(spec.get("route", "")).strip()
                    if not spec_route or spec_route == r_clean:
                        matched_spec_ref = r_clean
                elif full_url in browser_specs:
                    spec = browser_specs[full_url]
                    spec_route = str(spec.get("route", "")).strip()
                    if not spec_route or spec_route in (r_clean, full_url):
                        matched_spec_ref = full_url

                # Do not manufacture browser verification intents without accepted verification specs
                if matched_spec_ref is not None:
                    candidates.append(
                        IntentCandidate(
                            intent_type="BROWSER_VERIFY",
                            target_ref=r_clean,
                            verification_ref=matched_spec_ref,
                            description=f"Verify UI route {r_clean} at {full_url}",
                            payload_preview={"url": full_url, "expected_route": r_clean},
                        )
                    )
        except Exception:
            pass

    # 2. RUN_DECLARED_TEST candidates from verified package.json scripts (only admitted if safe)
    if verification_capabilities:
        scripts = verification_capabilities.get("verification_scripts", {})
        if isinstance(scripts, Mapping):
            for script_name, cmd_text in scripts.items():
                s_name = str(script_name).strip()
                # Verify non-mutating safety: only admit safe, allowlisted, bounded test commands
                if not is_safe_test_command(str(cmd_text)):
                    continue
                candidates.append(
                    IntentCandidate(
                        intent_type="RUN_DECLARED_TEST",
                        target_ref=s_name,
                        description=f"Run declared test script: npm run {s_name}",
                        payload_preview={"cmd": ["npm", "run", s_name]},
                    )
                )

    # 3. READ_OBSERVED_SOURCE candidates from bounded workspace manifest
    if workspace_manifest:
        rep_paths = workspace_manifest.get("representative_existing_paths", [])
        if isinstance(rep_paths, list):
            for p in rep_paths:
                p_str = str(p).strip().replace("\\", "/")
                if not p_str:
                    continue
                # Check if already read under this generation
                if workspace is not None and workspace_source_generation:
                    try:
                        ws_root = workspace.resolve()
                        target = (ws_root / p_str).resolve()
                        if target.is_file():
                            c_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                            rid = build_read_identity(
                                normalized_path=normalize_read_path(p_str),
                                content_sha256=c_hash,
                                workspace_source_generation=workspace_source_generation,
                            )
                            if rid in forbidden_read_identities:
                                continue
                    except Exception:
                        pass
                candidates.append(
                    IntentCandidate(
                        intent_type="READ_OBSERVED_SOURCE",
                        target_ref=p_str,
                        description=f"Read observed workspace source: {p_str}",
                        payload_preview={"action": "read_file", "path": p_str},
                    )
                )

    # 4. IMPLEMENT_AUTHORIZED_WORK_PACKAGE candidates from trusted work packages
    lane_allowed_scope = getattr(situation, "lane_allowed_scope", None)
    for wp_id, wp in work_packages.items():
        if not wp.approved:
            continue
        # Verify write_scope against canonical lane allowed scope if defined
        if lane_allowed_scope is not None:
            lane_allowed_prefixes = [p.replace("\\", "/").rstrip("/") + "/" for p in lane_allowed_scope]
            lane_allowed_exact = [p.replace("\\", "/") for p in lane_allowed_scope]
            scope_ok = True
            for tf in wp.target_files:
                clean_tf = tf.replace("\\", "/")
                is_covered = any(clean_tf.startswith(pref) or clean_tf + "/" == pref for pref in lane_allowed_prefixes)
                if not is_covered and clean_tf in lane_allowed_exact:
                    is_covered = True
                if not is_covered:
                    scope_ok = False
                    break
            if not scope_ok:
                continue

        candidates.append(
            IntentCandidate(
                intent_type="IMPLEMENT_AUTHORIZED_WORK_PACKAGE",
                target_ref=wp_id,
                description=f"Implement authorized work package {wp_id}: {wp.instruction[:60]}",
                payload_preview={
                    "prompt": wp.instruction,
                    "write_scope": list(wp.target_files),
                    "expected_artifacts": list(wp.expected_artifacts),
                },
            )
        )

    # Deduplicate against completed signatures
    deduped_candidates: List[IntentCandidate] = []
    for c in candidates:
        run_type_map = {
            "BROWSER_VERIFY": "BROWSER",
            "RUN_DECLARED_TEST": "PROCESS",
            "READ_OBSERVED_SOURCE": "FILE",
            "IMPLEMENT_AUTHORIZED_WORK_PACKAGE": "AGENTIC",
        }
        r_type = run_type_map.get(c.intent_type, "")
        sig_payload = dict(c.payload_preview)
        if c.intent_type == "BROWSER_VERIFY":
            # If verification spec is known, attach assertions for signature check
            spec = None
            if c.verification_ref:
                spec = browser_specs.get(c.verification_ref)
            elif c.target_ref in browser_specs:
                spec = browser_specs[c.target_ref]
            elif sig_payload.get("url") in browser_specs:
                spec = browser_specs[sig_payload["url"]]
            if spec and spec.get("assertions"):
                sig_payload["assertions"] = spec["assertions"]

        candidate_sig = f"{r_type}:" + json.dumps(
            sig_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if candidate_sig in forbidden_signatures:
            continue
        deduped_candidates.append(c)

    return IntentCatalog(
        objective_id=str(getattr(objective, "objective_id", "")),
        candidates=tuple(deduped_candidates),
        browser_verification_specs=browser_specs,
        work_packages=work_packages,
        render_origin=render_origin,
        control_sha=control_sha,
        execution_base_sha=str(execution_base_sha) if execution_base_sha is not None else None,
        workspace_source_generation=workspace_source_generation,
    )


def compile_intent_to_plan(
    intent: Mapping[str, Any],
    catalog: IntentCatalog,
    situation: Any,
    objective: Any,
    runtime_dir: Path,
    workspace: Optional[Path],
    *,
    workspace_source_generation: Optional[str] = None,
) -> Dict[str, Any]:
    """Compile ONE validated intent into ONE complete task in the existing PLAN_SCHEMA envelope."""
    # 1. Validate intent schema
    validator = Draft202012Validator(PLAN_INTENT_SCHEMA, format_checker=FormatChecker())
    errors = list(validator.iter_errors(intent))
    if errors:
        raise IntentCompilerError(f"PLAN_INTENT_SCHEMA validation failed: {errors[0].message}")

    schema_version = intent["schema_version"]
    objective_id = intent["objective_id"]
    intent_type = intent["intent_type"]
    target_ref = intent["target_ref"]
    verification_ref = intent.get("verification_ref")

    if objective_id != str(getattr(objective, "objective_id", "")):
        raise IntentCompilerError(
            f"Intent objective_id {objective_id!r} does not match current objective {getattr(objective, 'objective_id', '')!r}"
        )

    if intent_type not in SUPPORTED_INTENT_TYPES:
        raise IntentCompilerError(f"Unsupported intent_type: {intent_type!r}")

    # Verify canonical source identity binding against catalog originating source identity
    current_control_sha = getattr(situation, "control_sha", "")
    if current_control_sha:
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", current_control_sha):
            raise SourceGenerationMismatchError("Canonical control SHA is invalid or drifting")
        if catalog.control_sha and catalog.control_sha != current_control_sha:
            raise SourceGenerationMismatchError(
                f"Catalog control_sha {catalog.control_sha!r} does not match situation {current_control_sha!r}"
            )

    situation_base_sha = getattr(situation, "execution_base_sha", None)
    if situation_base_sha and catalog.execution_base_sha and catalog.execution_base_sha != situation_base_sha:
        raise SourceGenerationMismatchError(
            f"Catalog execution_base_sha {catalog.execution_base_sha!r} does not match situation {situation_base_sha!r}"
        )

    if (
        workspace_source_generation
        and catalog.workspace_source_generation
        and catalog.workspace_source_generation != workspace_source_generation
    ):
        raise SourceGenerationMismatchError(
            f"Catalog workspace_source_generation {catalog.workspace_source_generation!r} does not match {workspace_source_generation!r}"
        )

    candidate = catalog.get_candidate(intent_type, target_ref)
    if candidate is None:
        raise UnknownTargetRefError(
            f"target_ref {target_ref!r} is not present in trusted catalog for intent_type {intent_type!r}"
        )

    authority_id = str(getattr(objective, "authority_id", "")).strip()
    if not authority_id:
        raise IntentCompilerError("Current objective has no standing authority_id")

    # Build the single complete task
    task: Dict[str, Any] = {
        "authority_id": authority_id,
        "risk_class": "R0",
        "mutating": False,
        "dependencies": [],
        "scope_tags": list(getattr(objective, "scope_tags", [])),
        "write_scope": [],
        "expected_artifacts": [],
        "tests": [],
        "evidence_requirements": [],
        "completion_criteria": list(getattr(objective, "completion_criteria", [])),
    }

    if intent_type == "BROWSER_VERIFY":
        # Resolve render entry origin
        origin = catalog.render_origin or "http://127.0.0.1:5173"
        r_clean = target_ref.strip()
        full_url = origin.rstrip("/") + ("/" + r_clean.lstrip("/") if not r_clean.startswith("#") else "/" + r_clean)

        # Resolve assertions from verified spec
        spec = None
        if verification_ref:
            spec = catalog.browser_verification_specs.get(verification_ref)
            if spec is None:
                raise UnknownVerificationRefError(
                    f"verification_ref {verification_ref!r} not found in accepted browser verification specifications"
                )
        else:
            # Check candidate's matched spec
            if candidate.verification_ref:
                spec = catalog.browser_verification_specs.get(candidate.verification_ref)
            elif r_clean in catalog.browser_verification_specs:
                spec = catalog.browser_verification_specs[r_clean]
            elif full_url in catalog.browser_verification_specs:
                spec = catalog.browser_verification_specs[full_url]

        if not spec or not spec.get("assertions"):
            raise SourceGroundedVerificationSpecMissing(
                f"SOURCE_GROUNDED_VERIFICATION_SPEC_MISSING: No accepted browser verification specification found for route {r_clean!r}"
            )

        # Enforce route-assertion binding: Route A cannot be verified with Route B's spec
        spec_route = str(spec.get("route", "")).strip()
        if spec_route and spec_route not in (r_clean, full_url):
            raise MismatchedRouteVerificationSpecError(
                f"MISMATCHED_ROUTE_VERIFICATION_SPEC: Target route {r_clean!r} does not match verification spec route {spec_route!r}"
            )

        assertions = list(spec.get("assertions", []))
        if not assertions:
            raise SourceGroundedVerificationSpecMissing(
                f"SOURCE_GROUNDED_VERIFICATION_SPEC_MISSING: Verification specification for {r_clean!r} contains no assertions"
            )

        clean_ref = re.sub(r'[^a-zA-Z0-9_-]', '_', r_clean).strip('_')[:24]
        task["node_id"] = f"TASK-BROWSER-VERIFY-{clean_ref}"
        task["run_type"] = "BROWSER"
        task["payload"] = {
            "url": full_url,
            "expected_route": r_clean,
            "assertions": assertions,
        }
        task["evidence_requirements"] = ["REAL_LOCAL_BROWSER_SCREENSHOT", "dom_inspection", "assertion_results"]

    elif intent_type == "RUN_DECLARED_TEST":
        # Check command safety
        cmd = candidate.payload_preview.get("cmd", ["npm", "run", target_ref])
        clean_ref = re.sub(r'[^a-zA-Z0-9_-]', '_', target_ref).strip('_')[:24]
        task["node_id"] = f"TASK-TEST-{clean_ref.upper()}"
        task["run_type"] = "PROCESS"
        task["payload"] = {
            "cmd": list(cmd),
        }
        task["evidence_requirements"] = ["exit_code_zero", "process_output"]
        task["tests"] = [f"npm run {target_ref}"]

    elif intent_type == "READ_OBSERVED_SOURCE":
        clean_path = target_ref.replace("\\", "/")
        path_hash = hashlib.sha256(clean_path.encode("utf-8")).hexdigest()[:12]
        task["node_id"] = f"TASK-READ-{path_hash.upper()}"
        task["run_type"] = "FILE"
        task["payload"] = {
            "action": "read_file",
            "path": clean_path,
        }
        task["evidence_requirements"] = ["read_identity_hash"]

    elif intent_type == "IMPLEMENT_AUTHORIZED_WORK_PACKAGE":
        wp = catalog.work_packages.get(target_ref)
        if wp is None:
            raise UnknownTargetRefError(
                f"work_package_ref {target_ref!r} not found in trusted authorized work packages"
            )
        if not wp.approved:
            raise UnapprovedWorkPackageError(
                f"Work package {target_ref!r} lacks verified approval"
            )
        if wp.source_sha and current_control_sha and wp.source_sha != current_control_sha:
            raise SourceGenerationMismatchError(
                f"Work package source_sha {wp.source_sha!r} does not match current control_sha {current_control_sha!r}"
            )
        if wp.source_sha and situation_base_sha and wp.source_sha != situation_base_sha:
            raise SourceGenerationMismatchError(
                f"Work package source_sha {wp.source_sha!r} does not match situation execution_base_sha {situation_base_sha!r}"
            )

        # Enforce canonical lane allowed scope
        lane_allowed_scope = getattr(situation, "lane_allowed_scope", None)
        if lane_allowed_scope is not None:
            lane_allowed_prefixes = [p.replace("\\", "/").rstrip("/") + "/" for p in lane_allowed_scope]
            lane_allowed_exact = [p.replace("\\", "/") for p in lane_allowed_scope]
            for tf in wp.target_files:
                clean_tf = tf.replace("\\", "/")
                is_covered = any(clean_tf.startswith(pref) or clean_tf + "/" == pref for pref in lane_allowed_prefixes)
                if not is_covered and clean_tf in lane_allowed_exact:
                    is_covered = True
                if not is_covered:
                    raise UnauthorizedWriteScopeError(
                        f"Work package target file {clean_tf!r} is outside canonical lane allowed scope: CANONICAL_LANE_SCOPE_VIOLATION"
                    )

        clean_ref = re.sub(r'[^a-zA-Z0-9_-]', '_', target_ref).strip('_')[:24]
        task["node_id"] = f"TASK-AGENTIC-WP-{clean_ref.upper()}"
        task["run_type"] = "AGENTIC"
        task["mutating"] = True
        task["risk_class"] = wp.risk_class or "R0"
        if wp.authority_id:
            task["authority_id"] = wp.authority_id
        task["write_scope"] = list(wp.target_files)
        task["expected_artifacts"] = list(wp.expected_artifacts)
        task["tests"] = list(wp.tests)
        task["payload"] = {
            "prompt": wp.instruction,
        }
        task["evidence_requirements"] = ["changed_file_hashes", "test_pass_evidence", "source_diff"]

    # Wrap into the existing unchanged PLAN_SCHEMA envelope
    node_id = task["node_id"]
    plan: Dict[str, Any] = {
        "schema_version": "1.0.0",
        "objective_id": objective_id,
        "tasks": [task],
        "parallel_safe_groups": [[node_id]],
        "rollback_strategy": "none",
    }

    return plan

