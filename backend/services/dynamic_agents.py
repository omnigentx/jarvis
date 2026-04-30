"""Dynamic agent reload service.

Provides a background task that monitors the `.reload_needed` signal file
and triggers agent card loading when changes are detected.
"""

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

AGENT_CARDS_DIR = Path(__file__).parent.parent / ".fast-agent" / "agent_cards"
RELOAD_SIGNAL = Path(__file__).parent.parent / ".fast-agent" / ".reload_needed"
POLL_INTERVAL = 2.0  # seconds


async def preload_agent_cards(agent_app) -> list[str]:
    """Pre-load agent cards from disk and attach to Jarvis.

    Called once during server startup after agent_app is initialized.

    Returns:
        List of loaded agent names.
    """
    if not AGENT_CARDS_DIR.exists():
        AGENT_CARDS_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("[DYNAMIC] Created agent_cards directory: %s", AGENT_CARDS_DIR)
        return []

    cards = list(AGENT_CARDS_DIR.glob("*.md"))
    if not cards:
        logger.info("[DYNAMIC] No agent cards found in %s", AGENT_CARDS_DIR)
        return []

    try:
        loaded = await agent_app.load_agent_card(str(AGENT_CARDS_DIR), "Jarvis")
        logger.info("[DYNAMIC] ✓ Pre-loaded dynamic agents: %s", loaded)
        return loaded
    except Exception as e:
        logger.error("[DYNAMIC] Failed to pre-load agent cards: %s", e, exc_info=True)
        return []


async def signal_reload_loop(agent_app):
    """Background task: poll for .reload_needed signal and reload agents.

    The signal file is touched by:
    - agent_spawner MCP's spawn_agent() tool
    - Manual touch for hot-reloading edited cards
    - Future: API endpoint for agent management UI
    """
    logger.info("[DYNAMIC] Reload loop started (polling every %.1fs)", POLL_INTERVAL)

    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL)

            if not RELOAD_SIGNAL.exists():
                continue

            # Consume the signal
            RELOAD_SIGNAL.unlink(missing_ok=True)
            logger.info("[DYNAMIC] Reload signal detected, loading agent cards...")

            try:
                loaded = await agent_app.load_agent_card(
                    str(AGENT_CARDS_DIR), "Jarvis"
                )
                logger.info("[DYNAMIC] ✓ Reloaded agents: %s", loaded)
            except Exception as e:
                logger.error("[DYNAMIC] Reload failed: %s", e)

        except asyncio.CancelledError:
            logger.info("[DYNAMIC] Reload loop stopped")
            break
        except Exception as e:
            logger.error("[DYNAMIC] Unexpected error in reload loop: %s", e)
            await asyncio.sleep(5)  # Back off on errors
