"""Atomic per-agent model configuration and immutable call snapshots."""
from dataclasses import dataclass
from threading import RLock

class UnauthorizedModelChange(PermissionError):
    """Raised when an agent targets an unauthorized agent."""

@dataclass(frozen=True)
class CallSnapshot:
    agent_id: str
    model_id: str
    config_revision: int

class ModelAuthorizationPolicy:
    def __init__(self, privilege_levels: dict[str, int]):
        self._levels = dict(privilege_levels)

    def can_change(self, requester: str, target: str) -> bool:
        return (requester in self._levels and target in self._levels and
                (requester == target or self._levels[requester] < self._levels[target]))

class InMemoryModelConfigStore:
    def __init__(self, provider_default: str):
        self._default = provider_default
        self._overrides: dict[str, str] = {}
        self._revision = 0
        self._lock = RLock()

    def set(self, agent_id: str, model_id: str) -> int:
        with self._lock:
            self._overrides[agent_id] = model_id
            self._revision += 1
            return self._revision

    def snapshot(self, agent_id: str) -> tuple[str, int]:
        with self._lock:
            return self._overrides.get(agent_id, self._default), self._revision

class ModelSelectionService:
    def __init__(self, store: InMemoryModelConfigStore, policy: ModelAuthorizationPolicy | None = None):
        self._store = store
        self._policy = policy or ModelAuthorizationPolicy({})

    def set_model(self, requester: str, target: str, model_id: str) -> int:
        if not self._policy.can_change(requester, target):
            raise UnauthorizedModelChange(f"{requester} cannot change {target}")
        if not model_id or not model_id.strip():
            raise ValueError("model_id must not be empty")
        return self._store.set(target, model_id)

    def snapshot(self, agent_id: str) -> CallSnapshot:
        model, revision = self._store.snapshot(agent_id)
        return CallSnapshot(agent_id, model, revision)
