# API route modules
from fastapi import APIRouter

from routes.auth import router as auth_router
from routes.sessions import router as sessions_router
from routes.tts import router as tts_router
from routes.library import router as library_router
from routes.stories import router as stories_router
from routes.chat import router as chat_router
from routes.agents import router as agents_router
from routes.agents import resources_router
from routes.agent_timeline import router as agent_timeline_router
from routes.inject import router as inject_router
from routes.token_usage import router as token_usage_router
from routes.scheduler import router as scheduler_router
from routes.notifications import router as notifications_router
from routes.approvals import router as approvals_router
from routes.setup import router as setup_router
from routes.settings import router as settings_router
from routes.yaml_config import router as yaml_router
from routes.skills import router as skills_router
from routes.mcp import router as mcp_router
from routes.oauth import router as oauth_router
from routes.system import router as system_router
from routes.voice import router as voice_router
from routes.ws_voice import router as ws_voice_router
from routes.team_template import router as team_template_router
from routes.team_templates_factory import router as team_templates_factory_router
from routes.context_compaction import router as context_compaction_router
from routes.memory_settings import router as memory_settings_router
from routes.memory import router as memory_router
from routes.gateways import router as gateways_router

all_routers: list[APIRouter] = [
    auth_router,
    sessions_router,
    tts_router,
    library_router,
    stories_router,
    chat_router,
    agents_router,
    resources_router,
    agent_timeline_router,
    inject_router,
    token_usage_router,
    scheduler_router,
    notifications_router,
    approvals_router,
    setup_router,
    settings_router,
    yaml_router,
    skills_router,
    mcp_router,
    oauth_router,
    system_router,
    voice_router,
    ws_voice_router,
    team_template_router,
    team_templates_factory_router,
    context_compaction_router,
    memory_settings_router,
    memory_router,
    gateways_router,
]
