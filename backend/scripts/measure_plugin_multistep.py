"""Opt-in live LLM comparison, never a default CI network test.

Compare full-body system context with approved on-demand skill metadata over
three identical turns. Uses the operator's local provider configuration. No
external MCP servers, code writes, team spawning or vendor files committed.
Run from backend: UV_FROZEN=1 uv run python -m scripts.measure_plugin_multistep PACKAGE OUTPUT
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from fast_agent.core.fastagent import FastAgent

from services.plugins.package import inspect_package
from services.plugins.runtime_adapter import apply_to_agent

PROMPTS = [
    "Using the provided frontend design skill, give its title and the concrete line-length guidance. Read the skill if needed. Keep the answer under 70 words.",
    "Using that same skill, give three typography and visual hierarchy checks for a plugin settings UI. Reuse the content already read. Keep it under 100 words.",
    "Give a concise acceptance checklist for mobile: typography, focus, reduced motion, and error recovery. Distinguish explicit skill guidance from your inferred checks. Do not read the file again unless necessary. Keep it under 120 words.",
]


async def measure(root: Path) -> dict:
    package = inspect_package(root)
    if package.blockers or len(package.skills) != 1:
        raise ValueError("Use one reviewed skill-only package")
    body = (root / package.skills[0].path).read_text()
    fast = FastAgent(
        "Plugin measurement",
        config_path="fastagent.config.yaml",
        parse_cli_args=False,
        quiet=True,
    )
    await fast.app.initialize()
    fast.context.no_shell = True
    fast.agent(
        name="FullBody",
        model="openai.coding-agent",
        servers=[],
        skills=[],
        instruction="Answer only the design evaluation requested. Skill content follows.\n"
        + body,
    )(lambda: None)
    fast.agent(
        name="OnDemand",
        model="openai.coding-agent",
        servers=[],
        skills=[],
        instruction="Answer only the design evaluation requested. Use available skill content when relevant.\n{{agentSkills}}",
    )(lambda: None)
    results = {}
    async with fast.run() as app:
        for name in ("FullBody", "OnDemand"):
            agent = app.get_agent(name)
            if name == "OnDemand" and not await apply_to_agent(
                agent, root, package, None
            ):
                raise RuntimeError("Live skill update not acknowledged")
            steps = []
            for prompt in PROMPTS:
                start = time.perf_counter()
                before = len(agent.usage_accumulator.turns)
                response = await agent.send(prompt)
                turns = agent.usage_accumulator.turns[before:]
                steps.append(
                    {
                        "input": prompt,
                        "output": response,
                        "elapsed_seconds": round(time.perf_counter() - start, 3),
                        "llm_calls": len(turns),
                        "input_tokens": sum(turn.input_tokens for turn in turns),
                        "output_tokens": sum(turn.output_tokens for turn in turns),
                        "cache_hit_tokens": sum(
                            turn.cache_usage.cache_hit_tokens for turn in turns
                        ),
                    }
                )
            history = agent.message_history
            calls = [
                call.params.name
                for message in history
                for call in (message.tool_calls or {}).values()
            ]
            results[name] = {
                "steps": steps,
                "tool_calls": calls,
                "input_tokens": sum(step["input_tokens"] for step in steps),
                "output_tokens": sum(step["output_tokens"] for step in steps),
                "llm_calls": sum(step["llm_calls"] for step in steps),
                "elapsed_seconds": round(
                    sum(step["elapsed_seconds"] for step in steps), 3
                ),
            }
    return {
        "model": "openai.coding-agent",
        "package_digest": package.digest,
        "skill_chars": len(body),
        "scope": "isolated real fast-agent; not the full Jarvis/team overhead; one sequential A/B sample",
        "pricing": "custom proxy model; no verified monetary price, so no dollar-cost claim",
        "results": results,
    }


if __name__ == "__main__":
    output = asyncio.run(measure(Path(sys.argv[1]).resolve()))
    Path(sys.argv[2]).write_text(json.dumps(output, ensure_ascii=False, indent=2))
