"""Tests for logging_config.py — verify loggers are correctly configured."""

import logging
import os
from pathlib import Path

import pytest


class TestSetupLogging:
    """Tests for the centralized logging setup."""

    def test_jarvis_log_handler_exists(self):
        """Root logger should have a RotatingFileHandler for jarvis.log."""
        from core.logging_config import setup_logging, LOGS_DIR
        setup_logging()

        root = logging.getLogger()
        has_main = any(
            isinstance(h, logging.handlers.RotatingFileHandler)
            and "jarvis.log" in h.baseFilename
            for h in root.handlers
        )
        assert has_main, "Root logger should have a jarvis.log RotatingFileHandler"

    def test_spawn_activity_logger_has_handler(self):
        """spawn_activity logger should have a dedicated file handler."""
        from core.logging_config import setup_logging
        setup_logging()

        spawn_logger = logging.getLogger("spawn_activity")
        has_spawn_handler = any(
            isinstance(h, logging.handlers.RotatingFileHandler)
            and "spawn_activity.log" in h.baseFilename
            for h in spawn_logger.handlers
        )
        assert has_spawn_handler, "spawn_activity logger should have spawn_activity.log handler"

    def test_tools_logger_has_handler(self):
        """Tool loggers should have a tools.log handler."""
        from core.logging_config import setup_logging
        setup_logging()

        for name in ("story_server", "iot_server", "library_server"):
            tool_logger = logging.getLogger(name)
            has_tools = any(
                isinstance(h, logging.handlers.RotatingFileHandler)
                and "tools.log" in h.baseFilename
                for h in tool_logger.handlers
            )
            assert has_tools, f"{name} logger should have tools.log handler"

    def test_noisy_loggers_are_silenced(self):
        """Third-party noisy loggers should be set to WARNING."""
        from core.logging_config import setup_logging
        setup_logging()

        for name in ("httpcore", "httpx", "uvicorn.access"):
            assert logging.getLogger(name).level >= logging.WARNING

    def test_logs_dir_created(self):
        """Logs directory should be created if it doesn't exist."""
        from core.logging_config import setup_logging, LOGS_DIR
        setup_logging()

        assert os.path.isdir(LOGS_DIR), f"Logs directory should exist: {LOGS_DIR}"
