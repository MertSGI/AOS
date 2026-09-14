"""AOS Candidate Remote Source Evidence Manifest (Section 7).

Machine-readable evidence summarizing the published candidate, registry, router,
and security policies.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Any
import datetime
import subprocess
import json

def measure_git_remote_truth():
    """Dynamically measures local git HEAD, current branch, and remote origin ref."""
    try:
        proc_sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        local_sha = proc_sha.stdout.strip() if proc_sha.returncode == 0 else "UNKNOWN"

        proc_br = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, timeout=10)
        current_branch = proc_br.stdout.strip() if proc_br.returncode == 0 else "UNKNOWN"

        remote_sha = "UNKNOWN"
        remote_branch_present = False
        if current_branch and current_branch != "UNKNOWN":
            proc_ls = subprocess.run(
                ["git", "ls-remote", "origin", current_branch],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc_ls.returncode == 0 and proc_ls.stdout.strip():
                lines = proc_ls.stdout.strip().splitlines()
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == f"refs/heads/{current_branch}":
                        remote_sha = parts[0]
                        remote_branch_present = True
                        break

        local_remote_sha_match = (
            remote_branch_present
            and local_sha != "UNKNOWN"
            and local_sha == remote_sha
        )
        manifest_matches_remote = local_remote_sha_match

        return {
            "local_sha": local_sha,
            "branch": current_branch,
            "remote_sha": remote_sha,
            "remote_branch_present": remote_branch_present,
            "local_remote_sha_match": local_remote_sha_match,
            "manifest_sha_matches_remote_head": manifest_matches_remote,
        }
    except Exception as e:
        return {
            "local_sha": "FAILED",
            "branch": "FAILED",
            "remote_sha": "FAILED",
            "remote_branch_present": False,
            "local_remote_sha_match": False,
            "manifest_sha_matches_remote_head": False,
            "error": str(e),
        }

_git_truth = measure_git_remote_truth()

EVIDENCE_DATA: Dict[str, Any] = {
    "program_id": "AOS-NATIVE-EXECUTION-FABRIC-V2-20260914-01",
    "canonical_base_sha": "3491775e36ce129d11ee10ae5e632f8afbf63830",
    "candidate_branch": _git_truth["branch"],
    "candidate_sha": _git_truth["local_sha"],
    "remote_sha": _git_truth["remote_sha"],
    "remote_branch_present": _git_truth["remote_branch_present"],
    "local_remote_sha_match": _git_truth["local_remote_sha_match"],
    "manifest_sha_matches_remote_head": _git_truth["manifest_sha_matches_remote_head"],
    "controller_acceptance_source": "EXTERNAL_CONTROLLER_RULING_REQUIRED",
    "changed_files": [
        "extensions/autonomy-fabric/supervisor.py",
        "extensions/autonomy-fabric/worker_registry.py"
    ],
    "new_files": [
        "benchmarks/autonomy-fabric/native_vs_ag_benchmark.py",
        "extensions/autonomy-fabric/autonomy_metrics.py",
        "extensions/autonomy-fabric/execution_backend.py",
        "extensions/autonomy-fabric/execution_router.py",
        "extensions/autonomy-fabric/native_workers.py",
        "extensions/autonomy-fabric/patch_engine.py",
        "extensions/autonomy-fabric/persistent_coordinator.py",
        "extensions/autonomy-fabric/remote_source_evidence.py",
        "extensions/autonomy-fabric/tests/test_controller_acceptance_proofs.py",
        "extensions/autonomy-fabric/tests/test_execution_router.py",
        "extensions/autonomy-fabric/tests/test_native_workers.py",
        "extensions/autonomy-fabric/tests/test_persistent_coordinator.py",
        "extensions/autonomy-fabric/tests/test_zero_ag_pilot.py"
    ],
    "deleted_files": [],
    "backend_registry": [
        "native_file_worker",
        "native_process_worker",
        "native_git_worker",
        "github_ci_worker",
        "browser_execution_backend",
        "model_reasoning_backend",
        "antigravity_backend"
    ],
    "router_policy": {
        "preferred_order": ["NATIVE", "CI/GITHUB", "MODEL+NATIVE", "AG_SPECIALIST"],
        "ag_required_default": False,
        "automatic_failover": True,
        "fail_closed_on_authority_boundary": True
    },
    "authority_boundaries": {
        "production_go": "FAIL_CLOSED_HUMAN_REQUIRED",
        "destructive_reset": "PROHIBITED",
        "force_push": "PROHIBITED",
        "arbitrary_shell": "PROHIBITED",
        "unauthorized_path_mutation": "FAIL_CLOSED_ROLLBACK"
    },
    "secret_redaction_policy": {
        "rules": [
            "api_key_regex_scrub",
            "bearer_token_regex_scrub",
            "github_pat_regex_scrub",
            "zero_credentials_in_evidence",
            "zero_credentials_in_git"
        ]
    }
}

if __name__ == "__main__":
    print(json.dumps(EVIDENCE_DATA, indent=2))
