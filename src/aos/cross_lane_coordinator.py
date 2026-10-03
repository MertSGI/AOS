"""Cross-lane coordinator and downstream gate evaluator for AOS Runtime V1.

Ensures that downstream authorized commands (e.g. UI V2) become eligible
automatically when canonical preconditions are satisfied, without issuing new
commands or recovery proofs.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from aos.runtime_admission import AdmissionRecord, AdmissionState, CommandAdmissionStore
from aos.runtime_store import read_json

logger = logging.getLogger(__name__)

DOWNSTREAM_GATES = {
    # UI V2 command depends on R1 and R2 canonical acceptance
    "continue-61be4ab1af53cfa646d773ce": {
        "label": "lari_ui_v2",
        "predicate": lambda state: (
            "R3" in str(state.get("current_status") or "").upper()
            or "R3" in str(state.get("current_milestone") or "").upper()
            or "R3" in str(state.get("next_action") or "").upper()
            or "R2_ACCEPTED" in str(state.get("current_status") or "").upper()
            or "R2" in str(state.get("current_milestone") or "").upper()
        ),
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

        record = admission_store.get(command_id)
        if record.state == AdmissionState.SUPERSEDED.value:
            continue
        if record.state == AdmissionState.ACTIVE.value:
            continue

        try:
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
            logger.error("Failed to activate downstream command %s: %s", command_id, exc)

    return activated

