"""Read-only network + isolated real runtime probe; no LLM and no Jarvis DB.

Run from backend: PYTHONPATH=.:fast-agent/src uv run python scripts/probe_plugin_runtime.py
No downloaded plugin content is kept after the probe exits.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from fast_agent.agents.agent_types import AgentConfig
from fast_agent.agents.mcp_agent import McpAgent
from fast_agent.context import Context
from services.plugins.activation import apply_skills, remove_skills
from services.plugins.archive import download_package
from services.plugins.package import inspect_package

REPOSITORY = "anthropics/claude-code"
COMMIT = "732e167ee9d71296b4b63d6f529ac1334513826a"


async def probe() -> dict:
    with tempfile.TemporaryDirectory(prefix="jarvis-plugin-probe-") as temporary:
        root = Path(temporary) / "package"
        started = time.perf_counter()
        await download_package(REPOSITORY, COMMIT, "plugins/frontend-design", root, {REPOSITORY})
        package = inspect_package(root)
        if package.blockers:
            raise RuntimeError("Probe package has unsupported capabilities")
        context = Context()
        context.no_shell = True
        agent = McpAgent(AgentConfig(name="Evidence", instruction="Assist. {{agentSkills}}",
                                    servers=[], skills=[]), context=context)
        app = SimpleNamespace(get_agent=lambda _: agent)
        before = id(agent)
        activated = await apply_skills(root, package, "Evidence", app=app)
        if not activated:
            raise RuntimeError("Runtime did not acknowledge activation")
        result = await agent.call_tool("read_skill", {"path": str(agent.skill_manifests[0].path)})
        removed = await remove_skills(root, package, "Evidence", app=app)
        if result.isError or not removed or agent.skill_manifests or agent.shell_runtime_enabled or before != id(agent):
            raise RuntimeError("Runtime reader/removal/capability invariant failed")
        return {"source": REPOSITORY, "commit": COMMIT, "plugin": package.name,
                "digest": package.digest, "skills": len(package.skills), "blockers": package.blockers,
                "activation_ack": activated, "read_skill_error": result.isError,
                "read_skill_chars": sum(len(block.text) for block in result.content if hasattr(block, "text")),
                "detach_ack": removed, "remaining_skills": len(agent.skill_manifests),
                "shell_enabled": agent.shell_runtime_enabled,
                "elapsed_seconds": round(time.perf_counter() - started, 3), "llm_calls": 0}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2))
