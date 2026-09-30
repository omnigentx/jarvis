"""Operator configuration never implies that a daemon/image is available."""

from unittest.mock import AsyncMock

import pytest

from services.plugins.runtime_profile import runtime_profile


@pytest.mark.asyncio
async def test_missing_cli_is_not_runtime_ready(monkeypatch):
    monkeypatch.setattr("services.plugins.runtime_profile.shutil.which", lambda _: None)
    assert (await runtime_profile())["runtime_available"] is False


@pytest.mark.asyncio
async def test_missing_image_or_daemon_is_not_runtime_ready(monkeypatch):
    monkeypatch.setattr(
        "services.plugins.runtime_profile.shutil.which", lambda _: "/bin/docker"
    )
    monkeypatch.setenv("JARVIS_PLUGIN_SANDBOX_IMAGE", "sha256:" + "a" * 64)
    process = AsyncMock()
    process.returncode = 1
    process.communicate.return_value = (b"", None)
    monkeypatch.setattr(
        "services.plugins.runtime_profile.asyncio.create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    result = await runtime_profile()
    assert result["runtime_available"] is False
    assert result["reason"] == "image_or_daemon_unavailable"
