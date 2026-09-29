"""Proof-bound recovery authorization for terminal command lineages."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from aos.runtime_contract import CONTRACT_VERSION, utc_now
from aos.runtime_store import atomic_json, exclusive_file_lock, read_json


TERMINAL_STATES = frozenset({"PROJECT_COMPLETE", "HUMAN_REQUIRED", "FAILED"})


def _canonical_hash(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class RecoveryProofStore:
    """Stores immutable proof envelopes and records their one-time acceptance."""

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root.expanduser().resolve()
        self.root = self.runtime_root / "recovery-proofs"
        self.lock_path = self.runtime_root / "recovery-proofs.lock"

    def issue(
        self,
        *,
        command_id: str,
        project_id: str,
        canonical_revision: str,
        runtime_source_sha: str,
        root_cause: str,
        evidence: Dict[str, Any],
        authority: str,
        expires_at: str,
        repair_identity: Optional[str] = None,
        candidate_identity: Optional[str] = None,
        recovery_fingerprint: Optional[str] = None,
    ) -> Dict[str, Any]:
        issued_at = utc_now()
        body: Dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "command_id": command_id,
            "project_id": project_id,
            "canonical_revision": canonical_revision,
            "runtime_source_sha": runtime_source_sha,
            "root_cause": root_cause,
            "evidence": evidence,
            "evidence_sha256": _canonical_hash(evidence),
            "authority": authority,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "repair_identity": repair_identity,
            "candidate_identity": candidate_identity,
            "recovery_fingerprint": recovery_fingerprint,
        }
        body["proof_id"] = f"recovery-{_canonical_hash(body)[:32]}"
        path = self.root / f"{body['proof_id']}.json"
        with exclusive_file_lock(self.lock_path):
            if path.exists():
                raise ValueError(f"Recovery proof already exists: {body['proof_id']}")
            atomic_json(path, body)
        return dict(body)

    def read(self, proof_id: str) -> Dict[str, Any]:
        return read_json(self.root / f"{proof_id}.json", {})

    def validate(
        self,
        proof_id: str,
        *,
        command: Dict[str, Any],
        state: Dict[str, Any],
        canonical_revision: str,
        runtime_source_sha: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        proof = self.read(proof_id)
        if not proof:
            raise ValueError(f"Recovery proof not found: {proof_id}")
        if proof.get("accepted_at"):
            raise ValueError(f"Recovery proof already consumed: {proof_id}")
        project = command.get("project") if isinstance(command.get("project"), dict) else {}
        expected = {
            "command_id": str(command.get("command_id") or ""),
            "project_id": str(project.get("project_id") or ""),
            "canonical_revision": canonical_revision,
            "runtime_source_sha": runtime_source_sha,
        }
        for field, value in expected.items():
            if str(proof.get(field) or "") != str(value):
                raise ValueError(f"Recovery proof {field} mismatch")
        if str(state.get("state") or "") not in TERMINAL_STATES:
            raise ValueError("Recovery proof is only valid for a terminal command")
        evidence = proof.get("evidence")
        if not isinstance(evidence, dict) or proof.get("evidence_sha256") != _canonical_hash(evidence):
            raise ValueError("Recovery proof evidence hash mismatch")
        current = now or datetime.now(timezone.utc)
        if _parse_timestamp(str(proof.get("expires_at") or "")) <= current:
            raise ValueError("Recovery proof expired")
        return proof

    def accept(self, proof_id: str, *, accepted_by: str) -> Dict[str, Any]:
        path = self.root / f"{proof_id}.json"
        with exclusive_file_lock(self.lock_path):
            proof = read_json(path, {})
            if not proof:
                raise ValueError(f"Recovery proof not found: {proof_id}")
            if proof.get("accepted_at"):
                raise ValueError(f"Recovery proof already consumed: {proof_id}")
            proof["accepted_at"] = utc_now()
            proof["accepted_by"] = accepted_by
            atomic_json(path, proof)
        return proof
