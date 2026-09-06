"""Reference Intelligence Subsystem (R11).

Extracts and registers structural design principles from metadata analysis
without third-party code vendoring or repository cloning.
"""

from dataclasses import dataclass, field
import datetime
from typing import Dict, List, Optional, Any
from extensions.design_intelligence.contracts import ReferenceSource, ReferenceSignal


class ReferenceIntelligence:
    """Registry and metadata analyzer for design principles and candidate components (V1.1)."""

    REFERENCE_SOURCE_PLACEHOLDER_REJECTED = "YES"

    def __init__(self):
        self._sources: Dict[str, ReferenceSource] = {}
        self._signals: List[ReferenceSignal] = []

    def validate_reference_source(self, source: ReferenceSource) -> bool:
        """Validates reference source structurally per Section 7."""
        # 1. Identity check: real http/https URL or explicit named-source identity with unambiguous canonical id
        url_or_name = source.url_or_name.strip()
        is_http_url = url_or_name.startswith("http://") or url_or_name.startswith("https://")
        is_named_identity = len(url_or_name) >= 5 and not any(p.lower() in url_or_name.lower() for p in ["ref-", "editorial luxury", "placeholder", "sample", "generic"])
        
        if not (is_http_url or is_named_identity):
            return False

        # 2. Non-empty required fields
        if not source.purpose or not source.purpose.strip():
            return False
        if not source.observation or not source.observation.strip():
            return False
        if not source.license_provenance or not source.license_provenance.strip():
            return False
        if not source.generic_design_risk or not source.generic_design_risk.strip():
            return False
        if not source.recommended_use or not source.recommended_use.strip():
            return False

        # 3. Valid retrieval timestamp
        if not source.retrieval_timestamp or not source.retrieval_timestamp.strip():
            return False
        try:
            datetime.datetime.fromisoformat(source.retrieval_timestamp)
        except Exception:
            return False

        return True

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
        observation: str = "",
        retrieval_timestamp: Optional[str] = None,
    ) -> ReferenceSource:
        sid = source_id or f"src-{len(self._sources) + 1}"
        ts = retrieval_timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat()
        
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
            observation=observation,
            retrieval_timestamp=ts,
            is_placeholder=False,
        )

        if not self.validate_reference_source(source):
            source.is_placeholder = True
            raise ValueError(f"Reference source '{url_or_name}' rejected: REFERENCE_SOURCE_PLACEHOLDER_REJECTED=YES. Failed structural provenance validation.")

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

        source = self._sources[source_id]
        if not self.validate_reference_source(source):
            raise ValueError(f"Cannot extract signal: Reference source {source_id} failed provenance validation")

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
        valid_signals = []
        for s in self._signals:
            if s.source_id in self._sources and self.validate_reference_source(self._sources[s.source_id]):
                if not category or s.category == category:
                    valid_signals.append(s)
        return valid_signals


