"""AOS-2 Credential-Free Live Proof Ergonomics Runner."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aos.benchmark import run_benchmark
from aos.provider_registry import load_routing_policy
from aos.validate import validate_file

DEFAULT_DESCRIPTOR = "descriptors/lari.descriptor.json"
DEFAULT_EXPECTATION = "descriptors/lari.shadow-expectation.json"
DEFAULT_ROUTING_POLICY = "descriptors/lari.planner-policy.json"
DEFAULT_REQUEST_PATH = ".aos-control/live-proof-request.json"
DEFAULT_PROVIDERS = ["gemini", "groq"]
DEFAULT_SOURCE_MODE = "pinned_proof"
DEFAULT_REPEAT = 3

EXPECTED_MAIN = "9109859e6d6a231598c22f68224f512f198c9a49"
EXPECTED_BRANCH = "feature/aos-2-shadow-orchestrator"
EXPECTED_PROOF_SHA = "4c55eecdbe064c74b34af31a1daf9851689e4fe8"


def get_git_info() -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], bool]:
    """Retrieve git branch, HEAD, origin/main, origin/feature, and tracked clean status."""
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
        ).strip()
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
        main = subprocess.check_output(
            ["git", "rev-parse", "origin/main"], text=True
        ).strip()
        origin_feature = subprocess.check_output(
            ["git", "rev-parse", f"origin/{EXPECTED_BRANCH}"], text=True
        ).strip()
        status_out = subprocess.check_output(
            ["git", "status", "--porcelain"], text=True
        ).strip()

        # Check if any tracked files are modified
        tracked_modified = False
        if status_out:
            lines = [line.strip() for line in status_out.splitlines() if line.strip()]
            for line in lines:
                # Untracked files start with ?? - ignore untracked .aos-runtime/
                if line.startswith("??"):
                    continue
                tracked_modified = True
                break

        return branch, head, main, origin_feature, not tracked_modified
    except Exception:
        return None, None, None, None, False


def validate_request_file(request_path: str = DEFAULT_REQUEST_PATH) -> Tuple[bool, str, Optional[str]]:
    """Validate request authorization file for CI mode."""
    p = Path(request_path)
    if not p.exists():
        return False, f"Live proof request file '{request_path}' not found", None

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"Failed to parse live proof request file: {e}", None

    gate = data.get("gate")
    authorized = data.get("authorized")
    request_id = data.get("request_id")

    if gate != "AOS-2":
        return False, f"Live proof request gate '{gate}' != 'AOS-2'", None

    if not authorized:
        return False, "Live proof request is not authorized (authorized=false)", None

    if not request_id or request_id == "NOT_AUTHORIZED":
        return False, f"Invalid live proof request_id '{request_id}'", None

    return True, "AUTHORIZED", str(request_id)


def run_readiness_checks(
    descriptor_path: str = DEFAULT_DESCRIPTOR,
    expectation_path: str = DEFAULT_EXPECTATION,
    routing_policy_path: str = DEFAULT_ROUTING_POLICY,
    provider_ids: Optional[List[str]] = None,
    source_mode: str = DEFAULT_SOURCE_MODE,
    verify_git: bool = True,
) -> Tuple[bool, str, Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str]]:
    """Perform pre-credential readiness checks on environment, git state, and canonical files."""
    providers = provider_ids or DEFAULT_PROVIDERS
    aos_revision = None

    # 1. Git State Invariants
    if verify_git:
        branch, head, main, origin_feature, is_clean = get_git_info()
        aos_revision = head
        if branch != EXPECTED_BRANCH:
            return False, f"Git branch '{branch}' != expected '{EXPECTED_BRANCH}'", None, None, None
        if not head or head != origin_feature:
            return False, f"Local HEAD '{head}' != origin/{EXPECTED_BRANCH} '{origin_feature}'", None, None, None
        if main != EXPECTED_MAIN:
            return False, f"origin/main '{main}' != expected '{EXPECTED_MAIN}'", None, None, None
        if not is_clean:
            return False, "Tracked git working tree is dirty", None, None, None
    else:
        try:
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
            aos_revision = head
        except Exception:
            aos_revision = "0000000000000000000000000000000000000000"

    # 2. Source mode check
    if source_mode != "pinned_proof":
        return False, f"Source mode '{source_mode}' != 'pinned_proof'", None, None, aos_revision

    # 3. Canonical File Validation
    res_desc, _ = validate_file("project_descriptor", descriptor_path)
    if not res_desc.is_valid:
        return False, f"Project descriptor '{descriptor_path}' is invalid", None, None, aos_revision

    res_exp, _ = validate_file("shadow_expectation", expectation_path)
    if not res_exp.is_valid:
        return False, f"Shadow expectation '{expectation_path}' is invalid", None, None, aos_revision

    res_pol, _ = validate_file("planner_routing_policy", routing_policy_path)
    if not res_pol.is_valid:
        return False, f"Routing policy '{routing_policy_path}' is invalid", None, None, aos_revision

    # 4. Expectation Data Checks
    with open(expectation_path, "r", encoding="utf-8") as f:
        exp_data = json.load(f)

    if exp_data.get("expected_source_sha") != EXPECTED_PROOF_SHA:
        return False, f"Shadow expectation source SHA '{exp_data.get('expected_source_sha')}' != '{EXPECTED_PROOF_SHA}'", None, None, aos_revision

    # 5. Routing Policy Checks
    try:
        registry = load_routing_policy(routing_policy_path)
    except Exception as e:
        return False, f"Failed to load routing policy: {e}", None, None, aos_revision

    if registry.allow_paid_fallback:
        return False, "Routing policy has allow_paid_fallback=True (must be False)", None, None, aos_revision

    for pid in providers:
        entry = registry.get_provider(pid)
        if entry is None:
            return False, f"Provider '{pid}' not found in routing policy", None, None, aos_revision
        if not entry.enabled:
            return False, f"Provider '{pid}' is disabled in routing policy", None, None, aos_revision
        if entry.billing_class != "FREE_TIER":
            return False, f"Provider '{pid}' billing_class '{entry.billing_class}' != FREE_TIER", None, None, aos_revision

    if "openai" in providers:
        return False, "OpenAI cannot be selected for free-provider proof", None, None, aos_revision

    return True, "READINESS_OK", exp_data, registry.risk_routes, aos_revision


def scan_traces_for_credentials(
    trace_dir: Path, gemini_key: str, groq_key: str
) -> bool:
    """Scan all json traces in trace_dir for exact credential string occurrences."""
    if not trace_dir.exists():
        return True

    for p in trace_dir.glob("*.json"):
        try:
            content = p.read_text(encoding="utf-8")
            if gemini_key and len(gemini_key) > 5 and gemini_key in content:
                return False
            if groq_key and len(groq_key) > 5 and groq_key in content:
                return False
        except Exception:
            continue
    return True


def execute_live_proof(
    dry_run: bool = False,
    ci: bool = False,
    prompt_func=getpass.getpass,
    input_func=input,
    descriptor_path: str = DEFAULT_DESCRIPTOR,
    expectation_path: str = DEFAULT_EXPECTATION,
    routing_policy_path: str = DEFAULT_ROUTING_POLICY,
    request_path: str = DEFAULT_REQUEST_PATH,
    provider_ids: Optional[List[str]] = None,
    repeat: int = DEFAULT_REPEAT,
    trace_dir_override: Optional[Path] = None,
    verify_git: bool = True,
) -> Tuple[str, int]:
    """Orchestrate the secure live proof runner in interactive, dry-run, or CI mode."""
    providers = provider_ids or DEFAULT_PROVIDERS

    # Step 1: Readiness check BEFORE any secret prompt or call
    ok, reason, exp_data, _, aos_revision = run_readiness_checks(
        descriptor_path=descriptor_path,
        expectation_path=expectation_path,
        routing_policy_path=routing_policy_path,
        provider_ids=providers,
        source_mode="pinned_proof",
        verify_git=verify_git,
    )
    if not ok:
        print(f"READINESS CHECK FAILED: {reason}", file=sys.stderr)
        print("STATUS: HOLD", file=sys.stderr)
        return "HOLD", 1

    # Step 2: Handle CI request authorization validation
    request_id = "LOCAL_EXECUTION"
    if ci:
        req_ok, req_reason, req_id = validate_request_file(request_path)
        if not req_ok:
            print(f"CI_HOLD: {req_reason}", file=sys.stderr)
            print("STATUS: HOLD", file=sys.stderr)
            return "HOLD", 1
        request_id = req_id or "UNKNOWN_REQUEST_ID"

    # Step 3: Handle dry-run mode
    if dry_run:
        print("AOS-2 Live Free Provider Proof (Dry Run)")
        print()
        print("Readiness checks: PASS")
        print(f"AOS Revision: {aos_revision}")
        print("Project descriptor: VALID")
        print("Shadow expectation: VALID")
        print("Routing policy: VALID")
        print("Source mode: pinned_proof")
        print(f"Target proof SHA: {EXPECTED_PROOF_SHA}")
        print(f"Providers: {', '.join(providers)}")
        print(f"Mode: {'CI' if ci else 'INTERACTIVE'}")
        if ci:
            print(f"Request ID: {request_id}")
            print("Request Authorization: AUTHORIZED")
        print()
        print("GEMINI_API_KEY: NOT REQUIRED IN DRY RUN")
        print("GROQ_API_KEY: NOT REQUIRED IN DRY RUN")
        print("Provider calls: 0")
        print()
        print("Status: READY_FOR_CREDENTIAL_INPUT")
        return "READY_FOR_CREDENTIAL_INPUT", 0

    # Step 4: Credential acquisition
    gemini_key = ""
    groq_key = ""

    if ci:
        gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
        groq_key = os.environ.get("GROQ_API_KEY", "").strip()
        if not gemini_key or not groq_key:
            print("CI_HOLD: Missing required environment variable GEMINI_API_KEY or GROQ_API_KEY", file=sys.stderr)
            print("STATUS: HOLD", file=sys.stderr)
            return "HOLD", 1
    else:
        print("AOS-2 Live Free Provider Proof Setup")
        print("-----------------------------------")
        print(f"AOS Revision: {aos_revision}")

        existing_gemini = os.environ.get("GEMINI_API_KEY")
        existing_groq = os.environ.get("GROQ_API_KEY")

        if existing_gemini:
            ans = input_func("Use existing GEMINI_API_KEY for this proof? [y/N]: ").strip().lower()
            if ans in ("y", "yes"):
                gemini_key = existing_gemini
            else:
                gemini_key = prompt_func("Gemini API key: ")
        else:
            gemini_key = prompt_func("Gemini API key: ")

        if existing_groq:
            ans = input_func("Use existing GROQ_API_KEY for this proof? [y/N]: ").strip().lower()
            if ans in ("y", "yes"):
                groq_key = existing_groq
            else:
                groq_key = prompt_func("Groq API key: ")
        else:
            groq_key = prompt_func("Groq API key: ")

        if not gemini_key or not groq_key:
            print("Error: Both Gemini and Groq API keys are required.", file=sys.stderr)
            print("STATUS: HOLD", file=sys.stderr)
            return "HOLD", 1

    # Step 5: Ephemeral environment execution inside try/finally
    orig_gemini = os.environ.get("GEMINI_API_KEY")
    orig_groq = os.environ.get("GROQ_API_KEY")

    results = None
    try:
        os.environ["GEMINI_API_KEY"] = gemini_key.strip()
        os.environ["GROQ_API_KEY"] = groq_key.strip()

        print("\nExecuting live free provider benchmark...")
        results = run_benchmark(
            descriptor_path=descriptor_path,
            expectation_path=expectation_path,
            routing_policy_path=routing_policy_path,
            provider_ids=providers,
            repeat=repeat,
            trace_dir_override=trace_dir_override,
            source_mode="pinned_proof",
        )
    finally:
        # ALWAYS cleanup ephemeral environment variables
        if orig_gemini:
            os.environ["GEMINI_API_KEY"] = orig_gemini
        else:
            os.environ.pop("GEMINI_API_KEY", None)

        if orig_groq:
            os.environ["GROQ_API_KEY"] = orig_groq
        else:
            os.environ.pop("GROQ_API_KEY", None)

    # Step 6: Trace Secret Scan
    t_dir = trace_dir_override or (Path(__file__).resolve().parent.parent.parent / ".aos-runtime" / "shadow")
    scan_ok = scan_traces_for_credentials(t_dir, gemini_key.strip(), groq_key.strip())
    if not scan_ok:
        print("CREDENTIAL_LEAK_DETECTED: API key found in generated shadow trace!", file=sys.stderr)
        print("STATUS: HOLD", file=sys.stderr)
        return "HOLD", 1

    # Step 7: Post-run Tracked Repository Mutation Check
    if verify_git:
        _, _, _, _, post_clean = get_git_info()
        if not post_clean:
            print("REPOSITORY_MUTATION_DETECTED: Tracked files were modified during benchmark!", file=sys.stderr)
            print("STATUS: HOLD", file=sys.stderr)
            return "HOLD", 1

    # Step 8: Human-friendly final summary & Manifest
    final_status = "HOLD"
    if results and results.get("benchmark_status") == "PASS":
        final_status = "LIVE_FREE_PROVIDER_PROOF_READY_FOR_INDEPENDENT_VERIFICATION"

    manifest_dir = Path(__file__).resolve().parent.parent.parent / ".aos-runtime" / "live-proof"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_id = f"manifest-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}.json"
    manifest_data = {
        "run_id": manifest_id,
        "request_id": request_id,
        "mode": "CI" if ci else "INTERACTIVE",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "aos_revision": aos_revision,
        "branch": EXPECTED_BRANCH,
        "origin_main_revision": EXPECTED_MAIN,
        "proof_source_sha": EXPECTED_PROOF_SHA,
        "providers": providers,
        "source_mode": "pinned_proof",
        "repeat": repeat,
        "final_status": final_status,
    }
    with open(manifest_dir / manifest_id, "w", encoding="utf-8") as mf:
        json.dump(manifest_data, mf, indent=2)

    if final_status != "LIVE_FREE_PROVIDER_PROOF_READY_FOR_INDEPENDENT_VERIFICATION":
        print("\nAOS-2 Live Free Provider Proof Results")
        print("-------------------------------------")
        print(f"AOS Revision:\n{aos_revision}")
        print(f"Request ID:\n{request_id}")
        for pid, pdata in (results.get("providers") or {}).items():
            st = pdata.get("status", "HOLD")
            print(f"{pid.capitalize()}: {st}")
            for r in (pdata.get("runs") or []):
                if r.get("disposition") != "SHADOW_ACCEPT":
                    if r.get("planner_disposition"):
                        print(f"  Planner disposition: {r.get('planner_disposition')}")
                    if r.get("failed_policy_checks"):
                        for fc in r["failed_policy_checks"]:
                            print(f"  FAIL {fc.get('check_id')}: {fc.get('message')}")
        print("\nOverall: HOLD")
        return "HOLD", 1

    gemini_pass = results["providers"].get("gemini", {}).get("status") == "PASS"
    groq_pass = results["providers"].get("groq", {}).get("status") == "PASS"
    total_runs = results.get("total_runs", 0)
    total_pass = results.get("total_pass", 0)

    print("\nAOS-2 Live Free Provider Proof")
    print("------------------------------")
    print(f"AOS Revision:\n{aos_revision}")
    print(f"Request ID:\n{request_id}")
    print()
    print(f"Gemini:\n{repeat}/{repeat} PASS" if gemini_pass else f"Gemini:\nHOLD")
    print()
    print(f"Groq:\n{repeat}/{repeat} PASS" if groq_pass else f"Groq:\nHOLD")
    print()
    print(f"Overall:\n{total_pass}/{total_runs} PASS")
    print()
    print("OpenAI calls:\n0")
    print()
    print("Mutation:\nfalse")
    print()
    print("Credential cleanup:\nPASS")
    print()
    print("Trace secret scan:\nPASS")
    print()
    print("Status:\nLIVE_FREE_PROVIDER_PROOF_READY_FOR_INDEPENDENT_VERIFICATION")

    return "LIVE_FREE_PROVIDER_PROOF_READY_FOR_INDEPENDENT_VERIFICATION", 0


def main(args: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AOS-2 Live Free Provider Proof Runner")
    parser.add_argument("--dry-run", action="store_true", help="Perform pre-credential readiness checks without prompting or network calls")
    parser.add_argument("--ci", action="store_true", help="Execute in remote CI mode using environment credentials and authorization request file")
    parser.add_argument("--descriptor", default=DEFAULT_DESCRIPTOR, help="Path to project descriptor")
    parser.add_argument("--expectation", default=DEFAULT_EXPECTATION, help="Path to shadow expectation")
    parser.add_argument("--routing-policy", default=DEFAULT_ROUTING_POLICY, help="Path to planner routing policy")
    parser.add_argument("--request", default=DEFAULT_REQUEST_PATH, help="Path to live proof request authorization JSON file")
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT, help="Number of benchmark runs per provider")

    parsed = parser.parse_args(args)

    status, code = execute_live_proof(
        dry_run=parsed.dry_run,
        ci=parsed.ci,
        descriptor_path=parsed.descriptor,
        expectation_path=parsed.expectation,
        routing_policy_path=parsed.routing_policy,
        request_path=parsed.request,
        repeat=parsed.repeat,
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
