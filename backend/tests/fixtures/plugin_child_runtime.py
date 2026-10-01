"""Acceptance helper: a real McpAgent in a separate long-lived OS process."""
from __future__ import annotations

import asyncio
import json
import sys

from fast_agent.agents.agent_types import AgentConfig
from fast_agent.agents.mcp_agent import McpAgent
from fast_agent.config import MCPSettings, Settings
from fast_agent.context import Context
from fast_agent.mcp_server_registry import ServerRegistry
from services.plugins.team_runtime import start_team_runtime, _servers


async def main() -> None:
    record = json.loads(sys.argv[1])
    settings = Settings(mcp=MCPSettings(servers={}))
    context = Context(config=settings, server_registry=ServerRegistry(settings), no_shell=True)
    agent = McpAgent(AgentConfig(name=record['agent_name'], instruction='Assist. {{agentSkills}}',
                                servers=[], skills=[]), context=context)
    await agent.initialize()
    identity = id(agent)
    try:
        await start_team_runtime(agent, record['run_id'], record)
        print('EVIDENCE:' + json.dumps({'started':True,'identity':identity}), flush=True)
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line or line.strip() == 'quit':
                break
            tools = (await agent.list_tools()).tools
            data = {'identity':id(agent), 'tools':[tool.name for tool in tools],
                    'skills':[skill.name for skill in agent.skill_manifests]}
            if agent.skill_manifests:
                result = await agent.call_tool('read_skill', {'path':str(agent.skill_manifests[0].path)})
                data['skill_body'] = result.content[0].text
            echo = next((tool.name for tool in tools if tool.name.endswith('echo')), None)
            if echo:
                result = await agent.call_tool(echo, {'value':'child'})
                data['tool_result'] = result.content[0].text
                data['tool_error'] = result.isError
            print('EVIDENCE:' + json.dumps(data), flush=True)
    finally:
        if record['run_id'] in _servers:
            await _servers.pop(record['run_id']).close()
        await agent.shutdown()


asyncio.run(main())
