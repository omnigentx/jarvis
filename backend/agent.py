import asyncio
import os
from pathlib import Path
import re
from fast_agent import FastAgent, RequestParams

# --- MONKEY PATCH START ---
# Fix for MakerAgent cloning issue (missing worker_agent in spawn)
from typing import Any
from fast_agent.agents.llm_agent import LlmAgent
from fast_agent.agents.workflow.maker_agent import MakerAgent

def _fixed_clone_constructor_kwargs(self) -> dict[str, Any]:
    # Call LlmAgent directly since super() in a standalone function is tricky usually,
    # but here 'self' is the instance.
    kwargs = LlmAgent._clone_constructor_kwargs(self)
    kwargs["worker_agent"] = self.worker_agent
    kwargs["k"] = self.k
    kwargs["max_samples"] = self.max_samples
    kwargs["match_strategy"] = self.match_strategy
    kwargs["match_fn"] = self.match_fn
    kwargs["red_flag_max_length"] = self.red_flag_max_length
    kwargs["red_flag_validator"] = self.red_flag_validator
    return kwargs

MakerAgent._clone_constructor_kwargs = _fixed_clone_constructor_kwargs
# --- MONKEY PATCH END ---

# --- TagRelayAgent: Hook to preserve [[[...]]] system tags ---
from fast_agent.agents.mcp_agent import McpAgent
from fast_agent.agents.agent_types import AgentConfig
from fast_agent.agents.tool_runner import ToolRunnerHooks
from fast_agent.types import PromptMessageExtended

TAG_PATTERN = re.compile(r'\[\[\[[A-Z_]+:\s*[^\]]+\]\]\]')

async def _ensure_tags_relayed(runner, messages: list[PromptMessageExtended]) -> None:
    """before_llm_call hook: scan staged tool results for [[[...]]] tags and inject reminder.

    Uses before_llm_call (not after_tool_call) because _stage_tool_response()
    resets _delta_messages after after_tool_call, wiping any appended messages.
    before_llm_call fires AFTER staging, so appended messages survive.
    """
    tags_found = []
    for message in messages:
        if not message.tool_results:
            continue
        for call_id, result in message.tool_results.items():
            if result.isError:
                continue
            for content_block in result.content:
                if hasattr(content_block, 'text'):
                    found = TAG_PATTERN.findall(content_block.text)
                    tags_found.extend(found)
    if tags_found:
        tag_str = " ".join(tags_found)
        runner.append_messages(
            f"SYSTEM: Your response MUST include this exact tag: {tag_str}"
        )

TAG_RELAY_HOOKS = ToolRunnerHooks(before_llm_call=_ensure_tags_relayed)

class TagRelayAgent(McpAgent):
    """McpAgent with tag relay hook for leaf agents (no child agents)."""

    def __init__(self, config: AgentConfig, context=None, **kwargs):
        super().__init__(config, context=context, **kwargs)
        self.tool_runner_hooks = TAG_RELAY_HOOKS

# Create the application
fast = FastAgent("Jarvis", config_path="fastagent.config.yaml")

# Define Skills Directory
SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fast-agent/skills")

import logging as _logging
_agent_logger = _logging.getLogger("agent")

def get_skills(*names):
    """Load SkillManifest objects for specific skills by name."""
    from fast_agent.spawn.config_reader import get_skills as _get_skills
    return _get_skills(SKILLS_DIR, *names)

# Core skills used by most agents
CORE_SKILLS = get_skills("user-context")

# --- Static Agents (require TagRelayAgent or specific hooks) ---

@fast.agent(
    name="PersonalAgent",
    instruction="Bạn là trợ lý cá nhân...\n\n{{agentSkills}}",
    skills=CORE_SKILLS + get_skills("personal-assistant", "cron-management", "scrape-web"),
    servers=["serpapi", "time-service", "gmail", "calendar", "cron-server", "scrapling-server"],
    request_params=RequestParams(parallel_tool_calls=True),
)
async def personal_agent(prompt: str):
    pass

@fast.agent(
    name="IoTAgent",
    instruction="Bạn là chuyên gia IoT...\n\n{{agentSkills}}",
    skills=CORE_SKILLS + get_skills("iot-control"),
    servers=["iot-control", "time-service", "gmail"],
    tools={"time-service": ["get_current_time", "wait_for_seconds"]},
    request_params=RequestParams(parallel_tool_calls=True),
)
async def iot_agent(prompt: str):
    pass

@fast.custom(
    TagRelayAgent,
    name="MusicAgent",
    instruction="Bạn là chuyên gia âm nhạc...\n\n{{agentSkills}}",
    skills=CORE_SKILLS + get_skills("music-playback"),
    servers=["media-server"],
    tools={"media-server": ["search_youtube"]}
)
async def music_agent(prompt: str):
    pass

@fast.custom(
    TagRelayAgent,
    name="AudioReaderAgent",
    instruction="Bạn là chuyên gia tìm truyện và phát audio.\n\n{{agentSkills}}",
    skills=CORE_SKILLS + get_skills("audio-reading"),
    servers=["story-server", "library-server"],
    tools={
        "story-server": ["find_story_chapter"],
        "library-server": ["local_list_stories", "local_list_chapters", "local_search"]
    },
    request_params=RequestParams(parallel_tool_calls=True),
)
async def audio_reader_agent(prompt: str):
    pass

# --- Dynamic Agents ---
# ResearchAgent, FinanceAgent, CodingAgent, CrawlStoriesAgent
# are loaded from .fast-agent/agent_cards/ at runtime via load_agent_card()
# and auto-attached to Jarvis as tools

# --- Master Agent (Jarvis) ---

# Conditionally include agent_spawner — it crashes in Docker containers
# where the subprocess can't start. Set DISABLE_AGENT_SPAWNER=1 to skip.
_SPAWNER_ENABLED = os.environ.get("DISABLE_AGENT_SPAWNER", "").strip() not in ("1", "true", "yes")
_JARVIS_SERVERS = ["sequential-thinking", "scrapling-server", "serpapi", "cron-server", "time-service", "approval-server"]
_JARVIS_TOOLS = {}
if _SPAWNER_ENABLED:
    _JARVIS_SERVERS.append("agent_spawner")
    _JARVIS_TOOLS["agent_spawner"] = [
        "spawn_and_run_isolated",
        "spawn_and_run_background",
        "spawn_agent",
        "list_active_spawns",
        "cancel_spawn_tool",
        "restart_spawn",
        "remove_spawned_agent",
        "list_available_servers_tool",
        "spawn_team_tool",
        "get_team_status",
        "get_team_result",
        "list_team_templates_tool",
        "send_team_message",
        "resume_team_tool",
    ]
else:
    _agent_logger.warning("[AGENT] agent_spawner disabled via DISABLE_AGENT_SPAWNER env var")

@fast.agent(
    name="Jarvis",
    instruction="""\
    Bạn là Jarvis, trợ lý AI cao cấp.

    NHIỆM VỤ CỦA BẠN:
    1. Tiếp nhận yêu cầu của người dùng.
    2. Phân loại ý định và gọi Agent chuyên trách.
    3. Nếu yêu cầu phức tạp, hãy phối hợp nhiều Agent.

    QUY TẮC GỌI AGENT (BẮT BUỘC):
    Luôn dùng tool agent__<TênAgent> để giao việc cho Agent có sẵn:
    - agent__PersonalAgent: Email, lịch, nhắc nhở (dùng cron scheduler), quản lý cá nhân
    - agent__IoTAgent: Điều khiển thiết bị IoT, đèn, quạt
    - agent__MusicAgent: Phát nhạc, tìm bài hát
    - agent__AudioReaderAgent: Đọc/phát audio truyện
    Ngoài ra, các Agent động cũng xuất hiện dưới dạng tool agent__<AgentName>.
    Chỉ dùng agent_spawner khi cần TẠO MỚI agent tại runtime (spawn).

    <team_rules>
    MANDATORY team interaction rules:
    - PM self-orchestrates. Jarvis MONITORS ONLY via get_team_status. Team status is auto-delivered to PM.
    - spawn_team_tool already delivers the task to PM via its parameters. Do NOT call send_team_message right after spawning — that is redundant!
    - Use send_team_message(session_id, message) ONLY for follow-up directives, feedback, or course corrections AFTER the team is already working.
    - NEVER bypass PM to contact team members directly.
    - To resume a completed team with follow-up work: use resume_team_tool(session_id, follow_up_task).
    - Report results to user ONLY when team completes or errors.
    <violation>Directly contacting team members or sending duplicate messages to PM after spawn is a VIOLATION.</violation>
    </team_rules>

    QUY TẮC ĐỊNH DẠNG OUTPUT:
    - Trình bày bằng Markdown khi phù hợp (heading, bullet, bảng, code block).
    - Khi mô tả workflow / kiến trúc / timeline có nhiều bước hoặc nhiều actor,
      kèm 1 mermaid block (flowchart, sequence, gantt) — chỉ khi diagram thật
      sự rõ hơn text. Dashboard tự render mermaid.
    - Quy tắc đọc cho TTS được nạp riêng ở chat endpoint khi cần đọc thành
      tiếng — đừng tự thêm vào output mặc định.

    {{agentSkills}}
    """,
    skills=CORE_SKILLS + get_skills("terminal-execution", "delegation-strategy", "scrape-web", "skill-authoring", "cron-management"),
    servers=_JARVIS_SERVERS,
    tools=_JARVIS_TOOLS,
    agents=[
        "PersonalAgent",
        "IoTAgent",
        "MusicAgent",
        "AudioReaderAgent",
    ],
    default=True,
    request_params=RequestParams(use_history=True, parallel_tool_calls=False),
)
async def jarvis_main(prompt: str = "Xin chào"):
    async with fast.run() as agent:
        # Set tag relay hook on Jarvis (AgentsAsToolsAgent) after creation
        agent["Jarvis"].tool_runner_hooks = TAG_RELAY_HOOKS
        await agent.interactive()

if __name__ == "__main__":
    asyncio.run(jarvis_main())
