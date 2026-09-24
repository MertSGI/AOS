from extensions.autonomy_fabric.execution_backend import (
    ExecutionBackend, ExecutionCapability, ExecutionCost, ExecutionHealth,
    ExecutionRequest, ExecutionResult, ExecutionTrustZone,
)
from extensions.autonomy_fabric.resource_orchestrator import ResourceOrchestrator


class Backend(ExecutionBackend):
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    supported_capabilities = {ExecutionCapability.MODEL_REASONING}

    def __init__(self, name, cost, context=0, quality=1, latency=0, health=ExecutionHealth.HEALTHY):
        self.backend_id = name
        self.cost = cost
        self.context_window_tokens = context
        self.quality_tier = quality
        self.expected_latency_ms = latency
        self.health = health

    def get_health(self): return self.health
    def execute(self, request): raise AssertionError("ranking must not invoke")


def request(**requirements):
    return ExecutionRequest(
        task_id="t", project_id="p", workspace=".", operation_class="REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING], authority_id="a",
        payload={"resource_requirements": requirements},
    )


def test_zero_cost_first_is_subject_to_context_quality_and_latency_adequacy():
    local = Backend("local", ExecutionCost.FREE_LOCAL, context=4096, quality=1, latency=5000)
    free = Backend("free_cloud", ExecutionCost.FREE_TIER_CLOUD, context=32000, quality=2, latency=500)
    subscription = Backend("subscription", ExecutionCost.SUBSCRIPTION_INCLUDED, context=128000, quality=3, latency=1000)
    orchestrator = ResourceOrchestrator()
    assert orchestrator.select([subscription, free, local], request(context_tokens=2000)) == "local"
    assert orchestrator.select([subscription, free, local], request(context_tokens=8000)) == "free_cloud"
    assert orchestrator.select([subscription, free, local], request(minimum_quality=3)) == "subscription"
    assert orchestrator.select([subscription, free, local], request(maximum_latency_ms=700)) == "free_cloud"


def test_paid_never_becomes_default_escape_hatch_and_unavailable_is_skipped():
    unavailable = Backend("local", ExecutionCost.FREE_LOCAL, health=ExecutionHealth.UNAVAILABLE)
    paid = Backend("paid", ExecutionCost.PAID_CLOUD)
    ranks = ResourceOrchestrator().rank([paid, unavailable], request())
    assert not any(item.eligible for item in ranks)
    assert "PAID_DEFAULT_DENIED" in next(item for item in ranks if item.backend_id == "paid").reasons


def test_ranking_is_deterministic_by_backend_id_on_tie():
    a = Backend("a", ExecutionCost.FREE_LOCAL)
    b = Backend("b", ExecutionCost.FREE_LOCAL)
    assert ResourceOrchestrator().select([b, a], request()) == "a"
