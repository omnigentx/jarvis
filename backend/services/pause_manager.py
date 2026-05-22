"""Backward-compat shim. Real implementation lives in :mod:`services.pause_controller`.

Kept so existing callers (``from services.pause_manager import pause_manager``)
keep working through the multi-phase PauseController refactor. New code
should import from ``services.pause_controller`` directly.
"""

from services.pause_controller import (
    PauseController as PauseManager,
    pause_controller as pause_manager,
)

__all__ = ["PauseManager", "pause_manager"]
