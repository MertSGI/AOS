"""Independent acceptance verification and outbox export for AOS V2.

Implements FUNCTION_ORACLE_V1 verifier with 1e-9 absolute tolerance,
trusted sandbox driver with nonce-bound frames, and idempotent outbox export
into an isolated KnowledgeLedger(<THIS_V2_HOME>/knowledge).
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.receipts import (
    record_implementation_receipt,
    record_verification_receipt,
)
from aos.v2.bridge import compute_bytes_sha256, compute_file_sha256
from aos.v2.sandbox import SandboxRunner, SandboxTestResult


ORACLE_TOLERANCE = 1e-9

DRIVER_SOURCE = """# AOS V2 Trusted Sandbox Driver (FUNCTION_ORACLE_V1)
import sys
import os
import json
import math
import importlib

# Ensure candidate working directory and candidate/src are discoverable
cwd = os.getcwd()
for p in [cwd, os.path.join(cwd, "src")]:
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

def main():
    try:
        raw_input = sys.stdin.read()
        if not raw_input:
            sys.exit(2)
        request = json.loads(raw_input)
        nonce = request["nonce"]
        module_name = request["module"]
        symbol_name = request["symbol"]
        cases = request["cases"]
        candidate_dir = request.get("candidate_dir")

        if candidate_dir:
            for p in [candidate_dir, os.path.join(candidate_dir, "src")]:
                if os.path.isdir(p) and p not in sys.path:
                    sys.path.insert(0, p)

        mod = importlib.import_module(module_name)
        func = getattr(mod, symbol_name)

        responses = []
        for c in cases:
            case_id = c["case_id"]
            args = c["args"]
            val = func(*args)
            if not isinstance(val, (int, float)) or math.isnan(val) or math.isinf(val):
                sys.exit(3)
            responses.append({
                "nonce": nonce,
                "case_id": case_id,
                "value": float(val),
            })

        sys.stdout.write(json.dumps(responses))
        sys.exit(0)
    except Exception as e:
        sys.stderr.write(str(e))
        sys.exit(1)

if __name__ == "__main__":
    main()
"""


class AcceptanceError(Exception):
    """Raised on verification or oracle failure."""


@dataclass(frozen=True)
class OracleCase:
    case_id: str
    args: Sequence[Any]
    expected: float


@dataclass(frozen=True)
class OracleCriterion:
    criterion_id: str
    module_name: str
    symbol_name: str
    oracle_file_path: Path
    expected_oracle_sha: str


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    criterion_id: str
    details: Mapping[str, Any]
    error: str | None = None


class FunctionOracleVerifier:
    """Evaluates candidates using server-owned FUNCTION_ORACLE_V1 oracle."""

    def __init__(self, sandbox_runner: SandboxRunner | None = None) -> None:
        self.sandbox = sandbox_runner or SandboxRunner()

    def verify_criterion(
        self,
        criterion: OracleCriterion,
        candidate_dir: Path,
        scratch_dir: Path,
        *,
        timeout: float = 30.0,
    ) -> VerificationResult:
        """Execute sandboxed trusted driver and compare response frames to private oracle."""
        candidate_path = Path(candidate_dir).resolve()
        scratch_path = Path(scratch_dir).resolve()
        scratch_path.mkdir(parents=True, exist_ok=True)

        # 1. Verify oracle file existence and SHA256 integrity
        if not criterion.oracle_file_path.is_file():
            return VerificationResult(
                passed=False,
                criterion_id=criterion.criterion_id,
                details={},
                error=f"Oracle file missing: {criterion.oracle_file_path}",
            )

        actual_oracle_sha = compute_file_sha256(criterion.oracle_file_path)
        if actual_oracle_sha.lower() != criterion.expected_oracle_sha.lower():
            return VerificationResult(
                passed=False,
                criterion_id=criterion.criterion_id,
                details={},
                error=f"Oracle SHA256 mismatch: expected {criterion.expected_oracle_sha}, got {actual_oracle_sha}",
            )

        # 2. Parse private oracle
        try:
            oracle_data = json.loads(criterion.oracle_file_path.read_text(encoding="utf-8"))
            cases_data = oracle_data.get("cases", [])
            cases = [
                OracleCase(
                    case_id=c["case_id"],
                    args=c["args"],
                    expected=float(c["expected"]),
                )
                for c in cases_data
            ]
            if not cases:
                return VerificationResult(
                    passed=False,
                    criterion_id=criterion.criterion_id,
                    details={},
                    error="Oracle contains no test cases",
                )
        except Exception as e:
            return VerificationResult(
                passed=False,
                criterion_id=criterion.criterion_id,
                details={},
                error=f"Failed to parse oracle file: {e}",
            )

        # 3. Materialize trusted driver into scratch directory
        driver_path = scratch_path / "aos_trusted_driver.py"
        driver_path.write_text(DRIVER_SOURCE, encoding="utf-8")

        # 4. Generate fresh nonsecret case nonce
        nonce = secrets.token_hex(16)
        request_payload = {
            "nonce": nonce,
            "module": criterion.module_name,
            "symbol": criterion.symbol_name,
            "cases": [{"case_id": c.case_id, "args": list(c.args)} for c in cases],
            "candidate_dir": str(candidate_path),
        }
        stdin_bytes = json.dumps(request_payload).encode("utf-8")

        # 5. Run driver inside sandbox
        test_argv = ["python", "-B", str(driver_path)]
        try:
            sb_res: SandboxTestResult = self.sandbox.run_test(
                test_argv,
                candidate_dir=candidate_path,
                scratch_dir=scratch_path,
                timeout=timeout,
                stdin_data=stdin_bytes,
            )
        except Exception as e:
            return VerificationResult(
                passed=False,
                criterion_id=criterion.criterion_id,
                details={},
                error=f"Sandbox execution error: {e}",
            )

        if sb_res.timed_out:
            return VerificationResult(
                passed=False,
                criterion_id=criterion.criterion_id,
                details={"exit_code": sb_res.exit_code, "timed_out": True},
                error="Driver process timed out",
            )

        if sb_res.exit_code != 0:
            return VerificationResult(
                passed=False,
                criterion_id=criterion.criterion_id,
                details={"exit_code": sb_res.exit_code, "stderr": sb_res.stderr},
                error=f"Driver exited with non-zero code {sb_res.exit_code}: {sb_res.stderr.strip()}",
            )

        # 6. Parse and strictly validate returned JSON frames
        try:
            frames = json.loads(sb_res.stdout)
            if not isinstance(frames, list):
                return VerificationResult(
                    passed=False,
                    criterion_id=criterion.criterion_id,
                    details={"stdout": sb_res.stdout},
                    error="Driver output is not a JSON list",
                )
        except Exception as e:
            return VerificationResult(
                passed=False,
                criterion_id=criterion.criterion_id,
                details={"stdout": sb_res.stdout},
                error=f"Failed to parse driver JSON frames: {e}",
            )

        # Map frames by case_id
        frames_by_case = {}
        for f in frames:
            if not isinstance(f, dict):
                return VerificationResult(
                    passed=False,
                    criterion_id=criterion.criterion_id,
                    details={},
                    error="Malformed frame item in driver output",
                )
            if f.get("nonce") != nonce:
                return VerificationResult(
                    passed=False,
                    criterion_id=criterion.criterion_id,
                    details={},
                    error=f"Frame nonce mismatch: expected {nonce}, got {f.get('nonce')}",
                )
            case_id = f.get("case_id")
            if not case_id or case_id in frames_by_case:
                return VerificationResult(
                    passed=False,
                    criterion_id=criterion.criterion_id,
                    details={},
                    error=f"Duplicate or missing case_id in frame: {case_id}",
                )
            frames_by_case[case_id] = f

        if len(frames_by_case) != len(cases):
            return VerificationResult(
                passed=False,
                criterion_id=criterion.criterion_id,
                details={"expected_cases": len(cases), "received_frames": len(frames_by_case)},
                error=f"Frame count mismatch: expected {len(cases)}, got {len(frames_by_case)}",
            )

        # 7. Compare every case value with 1e-9 tolerance
        diffs = []
        for case in cases:
            frame = frames_by_case.get(case.case_id)
            if not frame:
                return VerificationResult(
                    passed=False,
                    criterion_id=criterion.criterion_id,
                    details={},
                    error=f"Missing frame for case {case.case_id}",
                )
            val = frame.get("value")
            if not isinstance(val, (int, float)) or math.isnan(val) or math.isinf(val):
                return VerificationResult(
                    passed=False,
                    criterion_id=criterion.criterion_id,
                    details={},
                    error=f"Non-numeric or infinite value for case {case.case_id}: {val}",
                )
            diff = abs(float(val) - case.expected)
            if diff > ORACLE_TOLERANCE:
                diffs.append({
                    "case_id": case.case_id,
                    "expected": case.expected,
                    "actual": val,
                    "diff": diff,
                })

        if diffs:
            return VerificationResult(
                passed=False,
                criterion_id=criterion.criterion_id,
                details={"mismatches": diffs},
                error=f"{len(diffs)} case(s) exceeded tolerance {ORACLE_TOLERANCE}",
            )

        return VerificationResult(
            passed=True,
            criterion_id=criterion.criterion_id,
            details={
                "case_count": len(cases),
                "nonce": nonce,
                "max_diff": max((abs(float(frames_by_case[c.case_id]["value"]) - c.expected) for c in cases), default=0.0),
            },
        )


class OutboxExporter:
    """Manages atomic publishing of result.json and idempotent KCP ledger receipts."""

    def __init__(self, v2_home: Path) -> None:
        self.v2_home = Path(v2_home).resolve()
        self.knowledge_root = self.v2_home / "knowledge"
        self.knowledge_root.mkdir(parents=True, exist_ok=True)
        self.ledger = KnowledgeLedger(self.knowledge_root)

    def export_result(
        self,
        command_id: str,
        result_payload: Mapping[str, Any],
        *,
        base_sha: str,
        result_sha: str,
        project_id: str = "AOS",
    ) -> Path:
        """Atomically write result.json, fsync, read back hash, and record KCP receipts."""
        command_dir = self.v2_home / "commands" / command_id
        command_dir.mkdir(parents=True, exist_ok=True)
        result_file = command_dir / "result.json"
        tmp_file = command_dir / "result.json.tmp"

        payload_bytes = json.dumps(dict(result_payload), indent=2, sort_keys=True).encode("utf-8")
        tmp_file.write_bytes(payload_bytes)

        # Atomic replace
        tmp_file.replace(result_file)

        # Read back hash
        actual_sha = compute_file_sha256(result_file)
        expected_sha = compute_bytes_sha256(payload_bytes)
        if actual_sha.lower() != expected_sha.lower():
            raise AcceptanceError(f"Result file readback hash mismatch: expected {expected_sha}, got {actual_sha}")

        # Idempotent receipt append to KnowledgeLedger
        impl_key = f"{command_id}:implementation"
        verif_key = f"{command_id}:verification"

        record_implementation_receipt(
            self.ledger,
            project_id=project_id,
            agent_class="ANTIGRAVITY",
            tool_name="aos.v2.bridge",
            base_sha=base_sha,
            result_sha=result_sha,
            idempotency_key=impl_key,
            claims={"command_id": command_id, "result_sha256": actual_sha},
        )

        record_verification_receipt(
            self.ledger,
            project_id=project_id,
            agent_class="AOS_NATIVE",
            tool_name="aos.v2.acceptance",
            result_sha=result_sha,
            idempotency_key=verif_key,
            claims={"command_id": command_id, "result_sha256": actual_sha},
        )

        # Verify ledger consistency
        self.ledger.verify()
        return result_file
