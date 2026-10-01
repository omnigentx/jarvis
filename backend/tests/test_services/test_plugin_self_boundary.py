"""Public before-tool hook prevents self-management waiting on its own turn."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from services.plugins.runtime_control import RuntimeCapabilities


def request(*names):
    return SimpleNamespace(
        tool_calls={
            str(i): SimpleNamespace(params=SimpleNamespace(name=name))
            for i, name in enumerate(names)
        }
    )


@pytest.mark.asyncio
async def test_self_management_applies_and_refreshes_before_next_model_call():
    control = RuntimeCapabilities()
    runner = SimpleNamespace(refresh_tools=AsyncMock())
    await control.before_llm(runner, [])
    hook = control.hooks().before_tool_call
    assert hook is not None
    await hook(runner, request("plugin_management__plugin_add"))
    apply = AsyncMock(return_value=True)
    assert await asyncio.wait_for(control.update(apply), 0.25)
    await control.before_llm(runner, [])
    runner.refresh_tools.assert_awaited_once()


@pytest.mark.asyncio
async def test_mixed_batch_fails_before_tools_and_retains_boundary():
    control = RuntimeCapabilities()
    runner = SimpleNamespace(refresh_tools=AsyncMock())
    await control.before_llm(runner, [])
    hook = control.hooks().before_tool_call
    assert hook is not None
    with pytest.raises(ValueError, match="separate"):
        await hook(
            runner,
            request("plugin_management__plugin_activate", "filesystem__write_file"),
        )
    apply = AsyncMock(return_value=True)
    update = asyncio.create_task(control.update(apply))
    await asyncio.sleep(0)
    apply.assert_not_called()
    await control.turn_done(runner, None)
    assert await update


@pytest.mark.asyncio
async def test_self_management_does_not_bypass_other_inflight_runner():
    control = RuntimeCapabilities()
    one = SimpleNamespace(refresh_tools=AsyncMock())
    two = SimpleNamespace(refresh_tools=AsyncMock())
    await control.before_llm(one, [])
    await control.before_llm(two, [])
    hook = control.hooks().before_tool_call
    assert hook is not None
    await hook(one, request("plugin_management__plugin_add"))
    apply = AsyncMock(return_value=True)
    update = asyncio.create_task(control.update(apply))
    await asyncio.sleep(0)
    apply.assert_not_called()
    await control.turn_done(two, None)
    assert await asyncio.wait_for(update, 0.25)
