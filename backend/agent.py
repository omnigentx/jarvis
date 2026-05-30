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
    instruction="You are a personal assistant.\n\n{{agentSkills}}",
    skills=CORE_SKILLS + get_skills("personal-assistant", "cron-management", "scrape-web"),
    servers=["serpapi", "time-service", "gmail", "calendar", "cron-server", "scrapling-server"],
    request_params=RequestParams(parallel_tool_calls=True),
)
async def personal_agent(prompt: str):
    pass

@fast.agent(
    name="IoTAgent",
    instruction="You are an IoT specialist.\n\n{{agentSkills}}",
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
    instruction="You are a music specialist.\n\n{{agentSkills}}",
    skills=CORE_SKILLS + get_skills("music-playback"),
    servers=["media-server"],
    tools={"media-server": ["search_youtube"]}
)
async def music_agent(prompt: str):
    pass

@fast.custom(
    TagRelayAgent,
    name="AudioReaderAgent",
    instruction="You are a specialist in finding stories and playing audio.\n\n{{agentSkills}}",
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

@fast.agent(
    name="ResearchAgent",
    instruction="""\
You are a research specialist. Find accurate information from the internet and synthesize news. Always cite sources when possible.

RESEARCH DUTIES:
- Find accurate information from the internet and synthesize news.
- Always cite sources when possible.

Tool priority order:
1. serpapi → Top priority for any search request.
2. ScraplingServer → When serpapi does not return the desired results, or when you need to access a specific URL directly.
3. chrome-devtools → Last resort — only when you truly need to interact with a web page (click, fill form, login). Very slow and complex, avoid if possible.

Rule: Always try serpapi first → if serpapi does not return the desired results, use ScraplingServer → only use chrome-devtools when interaction is strictly required (click, login, navigate).

{{agentSkills}}""",
    skills=CORE_SKILLS + get_skills("proactive-mode", "research", "scrape-web"),
    servers=["serpapi", "scrapling-server", "chrome-devtools", "time-service"],
)
async def research_agent(prompt: str):
    pass

@fast.agent(
    name="FinanceAgent",
    instruction="""\
You are a finance specialist. Provide market information, stock prices, gold, crypto, and financial analysis.

FINANCE DUTIES:
- Provide market information, stock prices, gold, crypto, and financial analysis.
- Use serpapi to search for the most accurate financial information.
- Always refresh real time via get_current_time before querying date-based data.
- When you need to access a specific URL, use ScraplingServer.

{{agentSkills}}""",
    skills=CORE_SKILLS + get_skills("proactive-mode", "finance", "research", "scrape-web"),
    servers=["serpapi", "scrapling-server", "time-service"],
)
async def finance_agent(prompt: str):
    pass

@fast.agent(
    name="CrawlStoriesAgent",
    instruction="""\
You are a web story crawler specialist. Collect story data (Crawler) from the internet into the local library.

CRAWL WORKFLOW (MANUAL FLOW):

0. SMART URL HANDLING — INDEX/OVERVIEW LINK:
- If the user provides an Index/Overview link (Introduction page) -> DO NOT use it for Analyze.
- ACTION:
  1. get_story_chapters(overview_link).
  2. Get the URL of Chapter 1 and total_chapters.
  3. Use the Chapter 1 URL for the next steps.

1. FIND CHAPTER 1 URL:
- If the user has not provided a chapter link -> search_stories(query).

2. ANALYZE STRUCTURE (IMPORTANT):
- Call get_story_page_structure(url).
- Find the highest-scoring Selector (usually contains 'content', 'chapter', long text).
- AUTO-LEARN: If you see "TOP NEXT LINK CANDIDATES", pick the highest-scoring selector and call add_story_provider to teach the system.

3. VERIFY (TEST FIRST — IMPORTANT):
- Call test_crawl_chapter(url, content_selector=...).
- MENTAL CHECK:
  - If you see: "Advertisement", "Sorry", "Posted at...", "Please..." -> IT IS GARBAGE.
  - ACTION: Skip this selector. Pick another selector. Test again until you see clean story content (e.g. "Chapter 1...", actual narrative text).

4. CRAWL FULL:
- Call crawl_story(url, content_selector="#...", title_selector="h1", speed=1.0, max_chapters=total_chapters).
- Returns job_id.
- Inform the user: "Download started..." with tag [[[CRAWL_STARTED: job_id]]].

5. TRACKING:
- If the user asks -> get_crawl_status(job_id).

{{agentSkills}}""",
    skills=CORE_SKILLS + get_skills("proactive-mode", "crawling", "scrape-web"),
    servers=["story-server", "scrapling-server"],
)
async def crawl_stories_agent(prompt: str):
    pass

# --- Master Agent (Jarvis) ---

# Conditionally include agent_spawner — it crashes in Docker containers
# where the subprocess can't start. Set DISABLE_AGENT_SPAWNER=1 to skip.
_SPAWNER_ENABLED = os.environ.get("DISABLE_AGENT_SPAWNER", "").strip() not in ("1", "true", "yes")
_JARVIS_SERVERS = ["sequential-thinking", "scrapling-server", "serpapi", "cron-server", "time-service", "approval-server"]
_JARVIS_TOOLS = {}

# Self-improving Jarvis (experimental).
# The skill_server is ALWAYS registered with Jarvis. The actual on/off gate
# lives inside skill_server.py and reads the DB flag
# `experimental/SELF_IMPROVING_ENABLED` on every tool call — so the toggle
# in Settings → Experimental hot-reloads without a backend restart.
# When the flag is off the tools return a structured 503 explaining the
# disabled state; Jarvis surfaces that to the user verbatim.
# Note: `skill_server` (underscore) — see fastagent.config.yaml comment for why.
_JARVIS_SERVERS.append("skill_server")

# Self-managed MCP catalog. Same gating model as skill_server: tools return a
# structured 503 when SELF_IMPROVING_ENABLED is off. Lets Jarvis register
# off-the-shelf MCPs (Path A) and scaffold/test/promote new ones (Path B).
_JARVIS_SERVERS.append("mcp_admin")

if _SPAWNER_ENABLED:
    _JARVIS_SERVERS.append("agent_spawner")
    _JARVIS_TOOLS["agent_spawner"] = [
        "spawn_and_run_isolated",
        "spawn_and_run_background",
        "spawn_agent",
        "list_active_spawns",
        "cancel_spawn_tool",
        "restart_spawn",
        "resume_spawn",
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
    You are Jarvis, an advanced AI assistant.

    YOUR DUTIES:
    1. Receive user requests.
    2. If the request needs a specialized tool/data source, dispatch to the appropriate Agent.
    3. Otherwise (chitchat, venting, advice, general Q&A) — reply directly. Do NOT delegate.
    4. For complex requests, coordinate multiple Agents.

    AGENT INVOCATION RULES:
    Delegate via agent__<AgentName> ONLY when the request matches an agent's domain:
    - agent__PersonalAgent: Email, calendar events, reminders (cron) — task management only, NOT casual conversation
    - agent__IoTAgent: Control IoT devices, lights, fans
    - agent__MusicAgent: Play music, find songs
    - agent__AudioReaderAgent: Read/play audio stories
    - agent__ResearchAgent: Search information on the internet, synthesize news
    - agent__FinanceAgent: Finance, stock prices, gold, crypto
    - agent__CrawlStoriesAgent: Crawl stories from the web into the local library
    Dynamic Agents (created at runtime via spawn_agent) also appear as tools agent__<AgentName>.
    Only use agent_spawner when you need to CREATE A NEW agent at runtime (spawn).

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

    OUTPUT FORMAT RULES:
    - Use Markdown when appropriate (heading, bullet, table, code block).
    - When describing a workflow / architecture / timeline with multiple steps or actors,
      include one mermaid block (flowchart, sequence, gantt) — only when the diagram is
      genuinely clearer than text. The dashboard renders mermaid automatically.
    - TTS reading rules are loaded separately at the chat endpoint when audio output is
      needed — do not add them to the default output.

    {{agentSkills}}
    """,
    skills=CORE_SKILLS + get_skills("terminal-execution", "delegation-strategy", "scrape-web", "skill-creator", "mcp-authoring", "cron-management"),
    servers=_JARVIS_SERVERS,
    tools=_JARVIS_TOOLS,
    agents=[
        "PersonalAgent",
        "IoTAgent",
        "MusicAgent",
        "AudioReaderAgent",
        "ResearchAgent",
        "FinanceAgent",
        "CrawlStoriesAgent",
    ],
    default=True,
    request_params=RequestParams(use_history=True, parallel_tool_calls=False),
)
async def jarvis_main(prompt: str = "Hello"):
    async with fast.run() as agent:
        # Set tag relay hook on Jarvis (AgentsAsToolsAgent) after creation
        agent["Jarvis"].tool_runner_hooks = TAG_RELAY_HOOKS
        await agent.interactive()

if __name__ == "__main__":
    asyncio.run(jarvis_main())
