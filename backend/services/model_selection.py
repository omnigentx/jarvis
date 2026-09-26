"""Per-agent model selection with atomic revisions and call snapshots."""
from dataclasses import dataclass
from threading import RLock
from typing import Any

class UnauthorizedModelChange(PermissionError): pass
class UnknownModel(ValueError): pass

@dataclass(frozen=True)
class CallSnapshot:
    agent_id: str
    model_id: str
    config_revision: int
    provider_options: dict[str, Any]

class ModelAuthorizationPolicy:
    def __init__(self, privilege_levels: dict[str, int]): self._levels = dict(privilege_levels)
    def can_change(self, requester: str, target: str) -> bool:
        return requester in self._levels and target in self._levels and (requester == target or self._levels[requester] < self._levels[target])

class AgentDefinitionConfigStore:
    """SQLite-backed adapter using the canonical agent definition CRUD."""
    def __init__(self, provider_default: str, app_default: str | None = None):
        self._provider_default, self._app_default = provider_default, app_default
    def set(self, agent_id: str, model_id: str) -> int:
        from services import agent_definitions
        agent_definitions.update_definition(agent_id, model=model_id)
        return agent_definitions.get_rev()
    def snapshot(self, agent_id: str) -> tuple[str, int]:
        from services import agent_definitions
        row = agent_definitions.get_definition(agent_id) or {}
        return row.get("model") or self._app_default or self._provider_default, agent_definitions.get_rev()

class InMemoryModelConfigStore:
    def __init__(self, provider_default: str, app_default: str | None = None):
        self._provider_default, self._app_default = provider_default, app_default
        self._overrides: dict[str, str] = {}; self._revision = 0; self._lock = RLock()
    def set(self, agent_id: str, model_id: str) -> int:
        with self._lock: self._overrides[agent_id] = model_id; self._revision += 1; return self._revision
    def snapshot(self, agent_id: str) -> tuple[str, int]:
        with self._lock: return self._overrides.get(agent_id, self._app_default or self._provider_default), self._revision

class ModelSelectionService:
    def __init__(self, store: InMemoryModelConfigStore, policy: ModelAuthorizationPolicy | None = None, known_models: set[str] | None = None):
        self._store, self._policy, self._known = store, policy or ModelAuthorizationPolicy({}), known_models
    def set_model(self, requester: str, target: str, model_id: str) -> int:
        if not self._policy.can_change(requester, target): raise UnauthorizedModelChange(f"{requester} cannot change {target}")
        if self._known is not None and model_id not in self._known: raise UnknownModel(model_id)
        if not model_id or not model_id.strip(): raise ValueError("model_id must not be empty")
        return self._store.set(target, model_id)
    def snapshot(self, agent_id: str, provider_options: dict[str, Any] | None = None) -> CallSnapshot:
        model, revision = self._store.snapshot(agent_id)
        return CallSnapshot(agent_id, model, revision, dict(provider_options or {}))

    def capture_for_call(self, agent_id: str, provider_options: dict[str, Any] | None = None) -> CallSnapshot:
        """Capture once immediately before an adapter call; adapters must reuse it."""
        return self.snapshot(agent_id, provider_options)
