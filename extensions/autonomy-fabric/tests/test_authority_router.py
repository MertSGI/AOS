"""Unit tests for Authority Router (R5)."""

import pytest
from extensions.autonomy_fabric.authority_router import (
    AuthorityRouter,
    DecisionCategory,
    DecisionContext,
)


def test_classify_autonomous_technical_issues():
    router = AuthorityRouter()
    
    dec1 = router.route_decision("d1", "broken_build", "Fix build error", "Compilation failure")
    assert dec1.category == DecisionCategory.TECHNICAL_DECISION
    assert dec1.can_autonomously_resolve is True

    dec2 = router.route_decision("d2", "generic_design_threshold_failure", "Design quality fail", "Score low")
    assert dec2.category == DecisionCategory.DESIGN_QUALITY_GATE
    assert dec2.can_autonomously_resolve is True


def test_classify_strict_human_issues():
    router = AuthorityRouter()

    dec1 = router.route_decision(
        "d3",
        "product_acceptance",
        "Approve Release V2",
        "Final approval for production deploy",
        recommended_option="Approve",
        alternative_options=["Reject", "Request Revision"],
        consequences={"Approve": "Deploys live to users"},
    )
    assert dec1.category == DecisionCategory.HUMAN_PRODUCT_DECISION
    assert dec1.can_autonomously_resolve is False
    assert dec1.recommended_option == "Approve"
    assert "Reject" in dec1.alternative_options

    dec2 = router.route_decision("d4", "production_go", "Production Go", "Go live")
    assert dec2.category == DecisionCategory.PRODUCTION_DECISION
    assert dec2.can_autonomously_resolve is False
