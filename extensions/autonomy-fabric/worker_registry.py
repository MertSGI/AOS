"""AOS Worker / Device Registry (R8).

Tracks multi-PC worker node capabilities without secret exposure.
Provides abstract CredentialProvider interfaces for secure local secret storage.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Any
import datetime
import uuid


class WorkerStatus(str, Enum):
    ONLINE = "ONLINE"
    BUSY = "BUSY"
    OFFLINE = "OFFLINE"
    DEGRADED = "DEGRADED"
    UNAUTHORIZED = "UNAUTHORIZED"


@dataclass
class WorkerCapabilities:
    os: str
    architecture: str
    python_version: str
    node_version: Optional[str] = None
    git_version: Optional[str] = None
    antigravity_cli_version: Optional[str] = None
    browser_capability: bool = False
    github_capability: bool = False
    vercel_capability: bool = False
    local_workspace_roots: List[str] = field(default_factory=list)


@dataclass
class WorkerNode:
    worker_id: str
    machine_fingerprint: str
    capabilities: WorkerCapabilities
    status: WorkerStatus = WorkerStatus.ONLINE
    last_seen: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


class CredentialProvider:
    """Abstract interface for local credential retrieval (DPAPI, OS Keychain, Vault)."""

    def get_credential(self, credential_key: str) -> Optional[str]:
        raise NotImplementedError

    def store_credential(self, credential_key: str, secret: str) -> bool:
        raise NotImplementedError


class LocalMemoryCredentialProvider(CredentialProvider):
    """Offline deterministic memory credential provider for testing."""

    def __init__(self):
        self._store: Dict[str, str] = {}

    def get_credential(self, credential_key: str) -> Optional[str]:
        return self._store.get(credential_key)

    def store_credential(self, credential_key: str, secret: str) -> bool:
        self._store[credential_key] = secret
        return True


class WorkerRegistry:
    """Multi-PC worker node registry."""

    def __init__(self):
        self._workers: Dict[str, WorkerNode] = {}

    def register_worker(
        self,
        worker_id: str,
        machine_fingerprint: str,
        capabilities: WorkerCapabilities,
        status: WorkerStatus = WorkerStatus.ONLINE,
    ) -> WorkerNode:
        node = WorkerNode(
            worker_id=worker_id,
            machine_fingerprint=machine_fingerprint,
            capabilities=capabilities,
            status=status,
        )
        self._workers[worker_id] = node
        return node

    def update_heartbeat(self, worker_id: str, status: Optional[WorkerStatus] = None) -> Optional[WorkerNode]:
        if worker_id not in self._workers:
            return None
        worker = self._workers[worker_id]
        worker.last_seen = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if status:
            worker.status = status
        return worker

    def get_worker(self, worker_id: str) -> Optional[WorkerNode]:
        return self._workers.get(worker_id)

    def find_eligible_worker(self, required_capabilities: Dict[str, Any]) -> Optional[WorkerNode]:
        """Schedules tasks only to workers that satisfy required capabilities and are ONLINE."""
        for worker in self._workers.values():
            if worker.status not in (WorkerStatus.ONLINE, WorkerStatus.BUSY):
                continue

            caps = worker.capabilities
            match = True
            for req_key, req_val in required_capabilities.items():
                if hasattr(caps, req_key):
                    actual_val = getattr(caps, req_key)
                    if isinstance(req_val, bool) and actual_val != req_val:
                        match = False
                        break
                    elif isinstance(req_val, str) and actual_val != req_val:
                        match = False
                        break
            if match:
                return worker
        return None
