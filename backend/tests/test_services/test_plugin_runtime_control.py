"""Push updates wait for a safe runner boundary, without polling or prompts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.plugins.runtime_control import RuntimeCapabilities


@pytest.mark.asyncio
async def test_busy_turn_applies_at_next_llm_boundary_and_refreshes_tools():
    control = RuntimeCapabilities()
    runner = SimpleNamespace(refresh_tools=AsyncMock())
    await control.before_llm(runner, [])
    apply = AsyncMock(return_value=True)
    update = asyncio.create_task(control.update(apply))
    await asyncio.sleep(0)
    apply.assert_not_called()
    await control.before_llm(runner, [])
    assert await update is True
    apply.assert_awaited_once()
    runner.refresh_tools.assert_awaited_once()


@pytest.mark.asyncio
async def test_idle_update_applies_immediately_without_llm_call():
    control = RuntimeCapabilities()
    apply = AsyncMock(return_value=True)
    assert await control.update(apply) is True
    apply.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_pending_update_cannot_apply_later():
    control = RuntimeCapabilities()
    runner = SimpleNamespace(refresh_tools=AsyncMock())
    await control.before_llm(runner, [])
    apply = AsyncMock(return_value=True)
    update = asyncio.create_task(control.update(apply))
    await asyncio.sleep(0)
    update.cancel()
    with pytest.raises(asyncio.CancelledError):
        await update
    await control.before_llm(runner, [])
    apply.assert_not_called()
    runner.refresh_tools.assert_not_called()


@pytest.mark.asyncio
async def test_one_concurrent_runner_cannot_mutate_others_inflight_tools():
    control = RuntimeCapabilities()
    first = SimpleNamespace(refresh_tools=AsyncMock())
    second = SimpleNamespace(refresh_tools=AsyncMock())
    await control.before_llm(first, [])
    await control.before_llm(second, [])
    apply = AsyncMock(return_value=True)
    update = asyncio.create_task(control.update(apply))
    await asyncio.sleep(0)
    parked = asyncio.create_task(control.before_llm(first, []))
    await asyncio.sleep(0)
    apply.assert_not_called()
    assert not parked.done()
    await control.turn_done(second, None)
    await asyncio.wait_for(parked, 2)
    assert await update is True


@pytest.mark.asyncio
async def test_two_live_runners_apply_update_without_waiting_for_turn_end():
    control = RuntimeCapabilities()
    first = SimpleNamespace(refresh_tools=AsyncMock())
    second = SimpleNamespace(refresh_tools=AsyncMock())
    await control.before_llm(first, [])
    await control.before_llm(second, [])
    apply = AsyncMock(return_value=True)
    update = asyncio.create_task(control.update(apply))
    await asyncio.sleep(0)
    await asyncio.gather(control.before_llm(first, []), control.before_llm(second, []))
    try:
        assert update.done(), "both runners reached a boundary, but update starved"
        assert await update is True
        first.refresh_tools.assert_awaited_once()
        second.refresh_tools.assert_awaited_once()
    finally:
        update.cancel()
        await asyncio.gather(update, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_change_releases_parked_turn_without_other_runner_finishing():
    control = RuntimeCapabilities()
    first = SimpleNamespace(refresh_tools=AsyncMock())
    second = SimpleNamespace(refresh_tools=AsyncMock())
    await control.before_llm(first, [])
    await control.before_llm(second, [])
    apply = AsyncMock(return_value=True)
    update = asyncio.create_task(control.update(apply))
    await asyncio.sleep(0)
    parked = asyncio.create_task(control.before_llm(first, []))
    await asyncio.sleep(0)
    update.cancel()
    await asyncio.gather(update, return_exceptions=True)
    await asyncio.wait_for(parked, 0.1)
    apply.assert_not_called()
