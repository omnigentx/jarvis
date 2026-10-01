"""Event-driven, safe-boundary capability updates for live agent runtimes."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class PendingUpdate:
    apply: Callable[[], Awaitable[bool]]
    future: asyncio.Future[bool]


class RuntimeCapabilities:
    """Serialize capability mutations against active ToolRunner boundaries.

    No timers or prompt injections are involved. A busy runtime queues a change
    until all concurrent turns are at a boundary or have completed. Cancellation
    of a queued request prevents late application.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._boundary = asyncio.Condition(self._lock)
        self._queue: deque[PendingUpdate] = deque()
        self._running: set[int] = set()
        self._versions: dict[int, int] = {}
        self._owners: dict[int, asyncio.Task] = {}
        self._revision = 0

    async def update(self, apply: Callable[[], Awaitable[bool]]) -> bool:
        future = asyncio.get_running_loop().create_future()
        self._queue.append(PendingUpdate(apply, future))
        try:
            async with self._lock:
                await self._drain()
            return await future
        except asyncio.CancelledError:
            future.cancel()
            raise
        finally:
            if future.cancelled():
                asyncio.create_task(self._flush())

    async def _drain(self) -> None:
        self._queue = deque(
            request for request in self._queue if not request.future.cancelled()
        )
        if self._running:
            return
        while self._queue:
            request = self._queue.popleft()
            if request.future.cancelled():
                continue
            try:
                acknowledged = await request.apply()
                if acknowledged is True:
                    self._revision += 1
                if not request.future.done():
                    request.future.set_result(acknowledged is True)
            except asyncio.CancelledError:
                request.future.cancel()
                raise
            except Exception as exc:  # noqa: BLE001 — propagate via the correlated runtime future
                if not request.future.done():
                    request.future.set_exception(exc)

    async def before_llm(self, runner: Any, _messages: Any) -> None:
        identity = id(runner)
        owner = asyncio.current_task()
        if owner is not None and identity not in self._owners:
            self._owners[identity] = owner
            owner.add_done_callback(lambda task: self._owner_done(identity, task))
        async with self._lock:
            self._running.discard(identity)
            # Park at the boundary while another concurrent turn still owns
            # the old tool snapshot. The last arrival applies the update and
            # wakes every parked runner before any resumes its next LLM call.
            while self._queue and self._running:
                await self._boundary.wait()
            await self._drain()
            self._boundary.notify_all()
            if self._versions.get(identity, 0) != self._revision:
                await runner.refresh_tools()
                self._versions[identity] = self._revision
            self._running.add(identity)

    def _owner_done(self, identity: int, task: asyncio.Task) -> None:
        if self._owners.get(identity) is task:
            self._owners.pop(identity, None)
            self._versions.pop(identity, None)
            self._running.discard(identity)
            if self._queue:
                asyncio.create_task(self._flush())

    async def _flush(self) -> None:
        async with self._lock:
            await self._drain()
            self._boundary.notify_all()

    async def turn_done(self, runner: Any, _message: Any) -> None:
        identity = id(runner)
        async with self._lock:
            self._running.discard(identity)
            self._versions.pop(identity, None)
            self._owners.pop(identity, None)
            await self._drain()
            self._boundary.notify_all()

    async def before_tools(self, runner: Any, message: Any) -> None:
        names = {call.params.name for call in (message.tool_calls or {}).values()}
        mutations = {
            "plugin_management__plugin_add",
            "plugin_management__plugin_activate",
        }
        if not names & mutations:
            return
        allowed = mutations | {"plugin_management__plugin_list"}
        if names - allowed:
            raise ValueError(
                "Invoke plugin management in a separate batch from ordinary tools"
            )
        # Only management calls remain in this batch: they cannot execute an
        # old plugin tool snapshot. Release this runner before its RPC waits
        # for self-activation; other in-flight runners still protect theirs.
        async with self._lock:
            self._running.discard(id(runner))
            await self._drain()
            self._boundary.notify_all()

    def hooks(self):
        from fast_agent.agents.tool_runner import ToolRunnerHooks

        return ToolRunnerHooks(
            before_llm_call=self.before_llm,
            before_tool_call=self.before_tools,
            after_turn_complete=self.turn_done,
        )
