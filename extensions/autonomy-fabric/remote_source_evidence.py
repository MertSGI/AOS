"""AOS Candidate Remote Source Evidence Manifest (Section 7).

Machine-readable evidence summarizing the published candidate, registry, router,
and security policies.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Any
import datetime
import json

EVIDENCE_DATA: Dict[str, Any] = {
    "program_id": "AOS-NATIVE-EXECUTION-FABRIC-V2-20260914-01",
    "canonical_base_sha": "3491775e36ce129d11ee10ae5e632f8afbf63830",
    "candidate_branch": "feature/aos-native-execution-fabric-v2",
    "candidate_sha": "77b49fb42a9785bec274e89d64ed35f4d6bb0758",
    "remote_branch_present": True,
    "local_remote_sha_match": True,
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
        "extensions/autonomy-fabric/persistent_coordinator.py",
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
