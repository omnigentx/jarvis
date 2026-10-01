"""Host hooks and acknowledged updates for in-process agent capabilities."""

from __future__ import annotations

from typing import Any
from weakref import WeakKeyDictionary

from services.plugins.runtime_control import RuntimeCapabilities

_controls: WeakKeyDictionary[Any, RuntimeCapabilities] = WeakKeyDictionary()


def register_runtime(agent: Any) -> RuntimeCapabilities:
    """Attach once to the actual agent instance, including dynamic replacements."""
    control = _controls.get(agent)
    if control is None:
        from services.sse_progress import merge_hooks

        control = RuntimeCapabilities()
        hooks = control.hooks()
        existing = agent.tool_runner_hooks
        agent.tool_runner_hooks = merge_hooks(hooks, existing) if existing else hooks
        _controls[agent] = control
    return control


async def apply_live_skills(root, package, agent_name: str) -> bool:
    from services import shared_state
    from services.plugins.activation import apply_skills

    app = shared_state.agent_app
    if app is None:
        return False
    try:
        agent = app.get_agent(agent_name)
    except (KeyError, ValueError):
        return False
    if agent is None:
        return False
    return await register_runtime(agent).update(
        lambda: apply_skills(root, package, agent_name, app=app)
    )


async def remove_live_skills(root, package, agent_name: str) -> bool:
    from services import shared_state
    from services.plugins.activation import remove_skills

    app = shared_state.agent_app
    if app is None:
        return False
    try:
        agent = app.get_agent(agent_name)
    except (KeyError, ValueError):
        return False
    if agent is None:
        return False
    return await register_runtime(agent).update(
        lambda: remove_skills(root, package, agent_name, app=app)
    )
