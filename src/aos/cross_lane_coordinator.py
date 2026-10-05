"""Cross-lane coordinator and downstream gate evaluator for AOS Runtime V1.

Ensures that downstream authorized commands (e.g. UI V2) become eligible
automatically when canonical preconditions are satisfied, without issuing new
commands or recovery proofs.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, List, Mapping

from aos.runtime_admission import AdmissionRecord, AdmissionState, CommandAdmissionStore
from aos.runtime_store import read_json

logger = logging.getLogger(__name__)

_HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
_R2_GATE_IDS = frozenset(
    {"P7N2-DISCOVERY-MARKETPLACE_R2", "P7N2-DISCOVERY_MARKETPLACE-R2"}
)


class DownstreamActivationError(RuntimeError):
    """A canonical gate was proven but its existing command could not activate."""

    def __init__(self, command_id: str, classification: str) -> None:
        self.command_id = command_id
        self.classification = classification
        super().__init__(f"{classification}:{command_id}")


def _ui_v2_canonical_preconditions(state: Mapping[str, Any]) -> bool:
    """Require structured R1/R2 acceptance evidence, never milestone text."""
    contract = state.get("phase7_node2_contract")
    slices = contract.get("delivery_slices") if isinstance(contract, Mapping) else None
    if not isinstance(slices, Mapping):
        return False
    if slices.get("R1") != "ACCEPTED_PROVEN" or slices.get("R2") != "ACCEPTED_PROVEN":
        return False

    chain = state.get("phase7_accepted_execution_chain")
    r2_sha = str(chain.get("node2_r2") or "") if isinstance(chain, Mapping) else ""
    if not _HEX40.fullmatch(r2_sha):
        return False

    gates = state.get("accepted_gates")
    if not isinstance(gates, list):
        return False
    return any(
        isinstance(gate, Mapping)
        and str(gate.get("gate") or "").upper() in _R2_GATE_IDS
        and gate.get("status") == "CLOSED_PROVEN"
        and str(gate.get("tested_sha") or "").lower() == r2_sha.lower()
        for gate in gates
    )

DOWNSTREAM_GATES = {
    # UI V2 command depends on R1 and R2 canonical acceptance
    "continue-61be4ab1af53cfa646d773ce": {
        "label": "lari_ui_v2",
        "predicate": _ui_v2_canonical_preconditions,
        "description": "UI V2 requires R1 and R2 canonical acceptance",
    }
}


def evaluate_downstream_gates(
    control_dir: Path,
    admission_store: CommandAdmissionStore,
) -> List[AdmissionRecord]:
    """Evaluate downstream dependency gates against canonical state.
    
    If preconditions are met, transition pre-existing HOLD command lineages
    to ACTIVE with authority DOWNSTREAM_GATE_SATISFIED.
    """
    state_path = control_dir / "docs" / "project-control" / "STATE.json"
    if not state_path.is_file():
        alt = control_dir / "STATE.json"
        if alt.is_file():
            state_path = alt
        else:
            return []

    canonical_state = read_json(state_path, {})
    if not canonical_state:
        return []

    activated: List[AdmissionRecord] = []
    for command_id, gate in DOWNSTREAM_GATES.items():
        predicate = gate["predicate"]
        if not predicate(canonical_state):
            continue

        try:
            record = admission_store.get(command_id)
            if record.state == AdmissionState.SUPERSEDED.value:
                continue
            if record.state == AdmissionState.ACTIVE.value:
                continue
            updated = admission_store.set_state(
                command_id,
                AdmissionState.ACTIVE,
                authority="DOWNSTREAM_GATE_SATISFIED",
                reason=f"Canonical dependency satisfied: {gate['description']}",
                recovery_proof_id=None,
            )
            activated.append(updated)
            logger.info("Downstream gate satisfied; activated command %s (%s)", command_id, gate["label"])
        except Exception as exc:
            classification = f"DOWNSTREAM_ACTIVATION_{type(exc).__name__.upper()}"
            logger.error(
                "Failed to activate downstream command %s (%s)",
                command_id,
                classification,
            )
            raise DownstreamActivationError(command_id, classification) from exc

    return activated
