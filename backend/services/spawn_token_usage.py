"""Versioned per-call spawn usage adapter with replay-stable event identity."""

from __future__ import annotations
import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def persist_spawn_usage(
    agent_name: str, data: dict[str, Any], raw: dict[str, Any]
) -> None:
    """Accept existing producer v1, deduplicate atomically in the persistence layer.

    The wire timestamp is generated once by SpawnEvent and retained on replay.
    Untimestamped legacy callers can be measured but cannot be safely deduplicated.
    Cumulative formats are rejected, rather than silently changing billing semantics.
    """
    from services.sse_progress import _persist_and_broadcast_token_usage

    if data.get("usage_kind", "per_call_v1") != "per_call_v1":
        logger.error(
            "[TOKEN] Unsupported spawn usage contract: %s", data.get("usage_kind")
        )
        return
    try:
        fields = {
            key: int(data.get(key, 0) or 0)
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_hit_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
            )
        }
        if any(value < 0 for value in fields.values()):
            raise ValueError("negative token count")
        run_id = raw.get("run_id") or data.get("run_id") or ""
        stamp = raw.get("timestamp")
        event_id = None
        if run_id and stamp is not None:
            identity = json.dumps([run_id, agent_name, stamp, data], sort_keys=True)
            event_id = "spawn-v1:" + hashlib.sha256(identity.encode()).hexdigest()
        _persist_and_broadcast_token_usage(
            agent_name,
            run_id,
            {
                "input": fields["input_tokens"],
                "output": fields["output_tokens"],
                "total": fields["input_tokens"] + fields["output_tokens"],
                "model": data.get("model", "unknown"),
                "cache_hit": fields["cache_hit_tokens"],
                "cache_read": fields["cache_read_tokens"],
                "cache_write": fields["cache_write_tokens"],
                "reasoning": fields["reasoning_tokens"],
                "source_event_id": event_id,
            },
        )
    except (TypeError, ValueError):
        logger.exception("[TOKEN] Invalid per-call spawn usage for %s", agent_name)
