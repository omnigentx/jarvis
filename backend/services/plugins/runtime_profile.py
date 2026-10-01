"""Bounded operator profile probe, executed only on an explicit UI read."""

from __future__ import annotations

import asyncio
import os
import shutil

from services.plugins.sandbox import IMAGE


async def runtime_profile() -> dict:
    image = os.environ.get("JARVIS_PLUGIN_SANDBOX_IMAGE")
    result = {
        "image": image,
        "runtime_available": False,
        "network": "none",
        "read_only": True,
        "transports": ["stdio"],
        "reason": "docker_unavailable",
    }
    if shutil.which("docker") is None:
        return result
    if not image or not IMAGE.fullmatch(image):
        return {**result, "reason": "image_not_configured"}
    process = None
    try:
        async with asyncio.timeout(4):
            process = await asyncio.create_subprocess_exec(
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                image,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
            if process.returncode != 0 or not stdout.strip().startswith(b"sha256:"):
                return {**result, "reason": "image_or_daemon_unavailable"}
        return {**result, "runtime_available": True, "reason": None}
    except (OSError, TimeoutError):
        return {**result, "reason": "runtime_probe_failed"}
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
