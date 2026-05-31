"""
Centralized logging configuration for Jarvis backend.
All logs go into logs/ directory with RotatingFileHandler.
"""
import os
import sys
import logging
from logging.handlers import RotatingFileHandler

from helpers.logging_filters import RedactingFormatter

# Logs directory (relative to backend/)
LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# Defaults
DEFAULT_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def setup_logging():
    """Initialize centralized logging. Call once at startup (server.py)."""
    os.makedirs(LOGS_DIR, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, DEFAULT_LOG_LEVEL, logging.INFO))

    # Use the redacting formatter on every handler so the rendered output
    # (including exception tracebacks from exc_info=True) is scrubbed of
    # secret-looking values.  Pair this with the handler-level
    # RedactSecretsFilter below — filter catches record.msg early so other
    # consumers see the scrubbed text; formatter catches the final render
    # including traceback text the filter cannot reach.
    formatter = RedactingFormatter(LOG_FORMAT)

    # Remove only FileHandlers pointing to old locations (cleanup legacy)
    for handler in root_logger.handlers[:]:
        if isinstance(handler, logging.FileHandler) and not isinstance(handler, RotatingFileHandler):
            root_logger.removeHandler(handler)

    # Add console handler if not already present
    # Console defaults to WARNING to reduce noise; file keeps INFO
    # Override with LOG_CONSOLE_LEVEL=INFO for verbose terminal
    console_level = os.environ.get("LOG_CONSOLE_LEVEL", "WARNING").upper()
    has_console = any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
                      for h in root_logger.handlers)
    if not has_console:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(getattr(logging, console_level, logging.WARNING))
        console.setFormatter(formatter)
        root_logger.addHandler(console)

    # Add main rotating file handler if not already present
    main_log_path = os.path.join(LOGS_DIR, "jarvis.log")
    has_main = any(isinstance(h, RotatingFileHandler) and h.baseFilename == os.path.abspath(main_log_path)
                   for h in root_logger.handlers)
    if not has_main:
        main_file = RotatingFileHandler(
            main_log_path,
            maxBytes=5 * 1024 * 1024,  # 5MB
            backupCount=3,
            encoding="utf-8",
        )
        main_file.setLevel(logging.DEBUG)
        main_file.setFormatter(formatter)
        root_logger.addHandler(main_file)

    # Add tools rotating file handler to specific tool loggers
    tools_log_path = os.path.join(LOGS_DIR, "tools.log")
    tools_file = RotatingFileHandler(
        tools_log_path,
        maxBytes=2 * 1024 * 1024,  # 2MB
        backupCount=2,
        encoding="utf-8",
    )
    tools_file.setLevel(logging.INFO)
    tools_file.setFormatter(formatter)

    for name in ("story_server", "iot_server", "library_server"):
        tool_logger = logging.getLogger(name)
        # Avoid duplicate handlers
        if not any(isinstance(h, RotatingFileHandler) for h in tool_logger.handlers):
            tool_logger.addHandler(tools_file)

    # Background jobs rotating file handler
    bg_log_path = os.path.join(LOGS_DIR, "background_jobs.log")
    bg_file = RotatingFileHandler(
        bg_log_path,
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
        encoding="utf-8",
    )
    bg_file.setLevel(logging.INFO)
    bg_file.setFormatter(formatter)

    for name in ("background_jobs", "tts_pregen_job"):
        bg_logger = logging.getLogger(name)
        if not any(isinstance(h, RotatingFileHandler) for h in bg_logger.handlers):
            bg_logger.addHandler(bg_file)

    # Spawn activity log (team agent events)
    spawn_log_path = os.path.join(LOGS_DIR, "spawn_activity.log")
    spawn_file = RotatingFileHandler(
        spawn_log_path,
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
        encoding="utf-8",
    )
    spawn_file.setLevel(logging.INFO)
    spawn_file.setFormatter(formatter)

    spawn_logger = logging.getLogger("spawn_activity")
    if not any(isinstance(h, RotatingFileHandler) for h in spawn_logger.handlers):
        spawn_logger.addHandler(spawn_file)

    # Reduce noise from chatty third-party libs
    for noisy in ("httpcore", "httpx", "uvicorn.access", "uvicorn.error",
                   "mcp.server.fastmcp", "mcp.client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Defense-in-depth: scrub secret-looking values from every log line.
    # Attached at handler level (not logger level) so records propagated
    # from child loggers are also caught.
    from helpers.logging_filters import RedactSecretsFilter
    redact = RedactSecretsFilter()
    _attach_filter_once(root_logger.handlers, redact)
    for child_name in ("story_server", "iot_server", "library_server",
                       "background_jobs", "tts_pregen_job", "spawn_activity"):
        _attach_filter_once(logging.getLogger(child_name).handlers, redact)


def _attach_filter_once(handlers, flt):
    """Attach ``flt`` to each handler, skipping handlers that already
    carry a filter of the same class (the same handler instance is shared
    across multiple named loggers, so this loop visits it more than once)."""
    flt_cls = type(flt)
    for h in handlers:
        if not any(isinstance(existing, flt_cls) for existing in h.filters):
            h.addFilter(flt)
