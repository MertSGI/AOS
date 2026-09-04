"""Reference Intelligence Subsystem (R11).

Extracts and registers structural design principles from metadata analysis
without third-party code vendoring or repository cloning.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from extensions.design_intelligence.contracts import ReferenceSource, ReferenceSignal


class ReferenceIntelligence:
    """Registry and metadata analyzer for design principles and candidate components."""

    def __init__(self):
        self._sources: Dict[str, ReferenceSource] = {}
        self._signals: List[ReferenceSignal] = []

    def register_candidate_source(
        self,
        url_or_name: str,
        purpose: str,
        strength: str,
        integration_cost: str,
        dependency_cost: str,
        license_provenance: str,
        supply_chain_risk: str,
        generic_design_risk: str,
        recommended_use: str,
        do_not_use_conditions: Optional[List[str]] = None,
        source_id: Optional[str] = None,
    ) -> ReferenceSource:
        sid = source_id or f"src-{len(self._sources) + 1}"
        source = ReferenceSource(
            source_id=sid,
            url_or_name=url_or_name,
            purpose=purpose,
            strength=strength,
            integration_cost=integration_cost,
            dependency_cost=dependency_cost,
            license_provenance=license_provenance,
            supply_chain_risk=supply_chain_risk,
            generic_design_risk=generic_design_risk,
            recommended_use=recommended_use,
            do_not_use_conditions=do_not_use_conditions or [],
        )
        self._sources[sid] = source
        return source

    def extract_design_signal(
        self,
        source_id: str,
        category: str,
        observation: str,
        extracted_principle: str,
        confidence_score: float = 1.0,
    ) -> ReferenceSignal:
        if source_id not in self._sources:
            raise KeyError(f"Reference source {source_id} not found")
        sig = ReferenceSignal(
            signal_id=f"sig-{len(self._signals) + 1}",
            source_id=source_id,
            category=category,
            observation=observation,
            extracted_principle=extracted_principle,
            confidence_score=confidence_score,
        )
        self._signals.append(sig)
        return sig

    def query_signals(self, category: Optional[str] = None) -> List[ReferenceSignal]:
        if not category:
            return list(self._signals)
        return [s for s in self._signals if s.category == category]
