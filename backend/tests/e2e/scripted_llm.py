"""Fixture-driven LLM for E2E tests.

Reads a YAML transcript (sequence of assistant turns with optional tool_calls)
and plays them back in order when the agent calls the LLM. This lets us write
deterministic integration tests that exercise the full ToolRunner path
(tool dispatch, hooks, history) without hitting a real model.

Strict by design:
- Exhausting the script raises (no silent MESSAGES EXHAUSTED fallback).
- Unknown fixture keys raise (catch typos early).

Fixture schema:

    turns:
      - tool_calls:
          <call_id>:
            name: <tool_name>
            arguments: {<key>: <value>}
        content: ""                # optional, defaults to ""
      - content: "final answer"    # terminal turn (no tool_calls)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from mcp import CallToolRequest, Tool
from mcp.types import CallToolRequestParams

from fast_agent.core.prompt import Prompt
from fast_agent.llm.internal.passthrough import PassthroughLLM
from fast_agent.llm.request_params import RequestParams
from fast_agent.mcp.prompt_message_extended import PromptMessageExtended
from fast_agent.types.llm_stop_reason import LlmStopReason


_ALLOWED_TURN_KEYS = {"content", "tool_calls"}
_ALLOWED_TOOL_CALL_KEYS = {"name", "arguments"}


class ScriptedLLM(PassthroughLLM):
    """LLM that returns pre-scripted assistant turns from a YAML fixture."""

    def __init__(self, turns: list[dict[str, Any]], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._turns = turns
        self._index = 0
        # Per-turn record of the messages the agent sent into the LLM. Useful
        # for asserting hook behavior (e.g. TagRelayAgent injecting reminders).
        self.observed_messages: list[list[PromptMessageExtended]] = []

    @staticmethod
    def load_turns_from_yaml(path: str | Path) -> list[dict[str, Any]]:
        """Parse + validate a fixture file, return the turn list.

        Separate from :meth:`from_yaml` so callers (e.g. the subprocess
        entrypoint that instantiates via ``ModelFactory`` with no ctor args)
        can reuse the exact same validation without going through ``cls()``.
        """
        # Explicit UTF-8 — fixtures contain Vietnamese prompts that would
        # otherwise break under a non-UTF-8 default locale (common on Windows CI).
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        turns = data.get("turns") if isinstance(data, dict) else None
        if not isinstance(turns, list) or not turns:
            raise ValueError(f"Fixture {path} missing non-empty 'turns' list")
        for i, turn in enumerate(turns):
            if not isinstance(turn, dict):
                raise ValueError(
                    f"Fixture {path} turn[{i}] must be a mapping, got {type(turn).__name__}"
                )
            unknown = set(turn.keys()) - _ALLOWED_TURN_KEYS
            if unknown:
                raise ValueError(f"Fixture {path} turn[{i}] has unknown keys: {unknown}")
            tool_calls = turn.get("tool_calls")
            if tool_calls is None:
                continue
            # Reject the common mistake `tool_calls: [...]` — the schema
            # keys by call_id, so a list would otherwise crash with an
            # opaque AttributeError on .items().
            if not isinstance(tool_calls, dict):
                raise ValueError(
                    f"Fixture {path} turn[{i}].tool_calls must be a mapping "
                    f"(keyed by call_id), got {type(tool_calls).__name__}"
                )
            for cid, tc in tool_calls.items():
                if not isinstance(tc, dict):
                    raise ValueError(
                        f"Fixture {path} turn[{i}].tool_calls[{cid}] must be a mapping, "
                        f"got {type(tc).__name__}"
                    )
                unknown_tc = set(tc.keys()) - _ALLOWED_TOOL_CALL_KEYS
                if unknown_tc:
                    raise ValueError(
                        f"Fixture {path} turn[{i}].tool_calls[{cid}] has unknown keys: {unknown_tc}"
                    )
                if "name" not in tc:
                    raise ValueError(
                        f"Fixture {path} turn[{i}].tool_calls[{cid}] missing required 'name'"
                    )
        return turns

    @classmethod
    def from_yaml(cls, path: str | Path, **kwargs: Any) -> "ScriptedLLM":
        return cls(turns=cls.load_turns_from_yaml(path), **kwargs)

    async def _apply_prompt_provider_specific(
        self,
        multipart_messages: list[PromptMessageExtended],
        request_params: RequestParams | None = None,
        tools: list[Tool] | None = None,
        is_template: bool = False,
    ) -> PromptMessageExtended:
        self.observed_messages.append(list(multipart_messages))
        if self._index >= len(self._turns):
            raise RuntimeError(
                f"ScriptedLLM exhausted after {len(self._turns)} turns — "
                f"agent called LLM more times than the fixture scripted"
            )
        turn = self._turns[self._index]
        self._index += 1

        content_text = turn.get("content", "") or ""
        raw_tool_calls = turn.get("tool_calls") or {}

        if raw_tool_calls:
            tool_calls: dict[str, CallToolRequest] = {
                cid: CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(
                        name=tc["name"],
                        arguments=tc.get("arguments") or {},
                    ),
                )
                for cid, tc in raw_tool_calls.items()
            }
            return Prompt.assistant(
                content_text,
                stop_reason=LlmStopReason.TOOL_USE,
                tool_calls=tool_calls,
            )

        return Prompt.assistant(content_text, stop_reason=LlmStopReason.END_TURN)

    @property
    def turns_consumed(self) -> int:
        return self._index

    @property
    def turns_total(self) -> int:
        return len(self._turns)
