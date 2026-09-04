"""Unit tests for Worker / Device Registry (R8)."""

import pytest
from extensions.autonomy_fabric.worker_registry import (
    WorkerRegistry,
    WorkerNode,
    WorkerCapabilities,
    WorkerStatus,
    LocalMemoryCredentialProvider,
)


def test_worker_registration_and_capability_matching():
    registry = WorkerRegistry()
    caps1 = WorkerCapabilities(
        os="Windows",
        architecture="x64",
        python_version="3.14",
        browser_capability=True,
        github_capability=True,
    )
    w1 = registry.register_worker("w-1", "fingerprint-win-1", caps1)

    caps2 = WorkerCapabilities(
        os="Linux",
        architecture="x64",
        python_version="3.12",
        browser_capability=False,
        github_capability=False,
    )
    w2 = registry.register_worker("w-2", "fingerprint-nix-1", caps2)

    # Search for worker requiring browser capability
    match_browser = registry.find_eligible_worker({"browser_capability": True})
    assert match_browser is not None
    assert match_browser.worker_id == "w-1"

    # Search for worker requiring Linux OS
    match_linux = registry.find_eligible_worker({"os": "Linux"})
    assert match_linux is not None
    assert match_linux.worker_id == "w-2"


def test_credential_provider_abstraction():
    cp = LocalMemoryCredentialProvider()
    assert cp.get_credential("API_KEY") is None

    cp.store_credential("API_KEY", "secret_token_123")
    assert cp.get_credential("API_KEY") == "secret_token_123"
