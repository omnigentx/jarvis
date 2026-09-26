"""Live model catalog for the configured OpenAI-compatible gateway."""
from __future__ import annotations

import os
import hashlib
import threading
import time
from pathlib import Path

import httpx
import yaml


class CatalogUnavailable(RuntimeError):
    pass


_LOCK = threading.Lock()
_CACHE: tuple[str, float, frozenset[str]] | None = None
_SECRETS = Path(__file__).resolve().parent.parent / "fastagent.secrets.yaml"


def _gateway() -> tuple[str, str]:
    data = yaml.safe_load(_SECRETS.read_text(encoding="utf-8")) or {}
    section = data.get("openai") or {}
    base_url = os.environ.get("OPENAI_BASE_URL") or section.get("base_url")
    api_key = os.environ.get("OPENAI_API_KEY") or section.get("api_key")
    if not base_url or not api_key:
        raise CatalogUnavailable("OpenAI-compatible gateway is not configured")
    return str(base_url).rstrip("/"), str(api_key)


def available_models() -> frozenset[str]:
    global _CACHE
    base_url, api_key = _gateway()
    cache_key = base_url + ":" + hashlib.sha256(api_key.encode()).hexdigest()
    now = time.monotonic()
    with _LOCK:
        if _CACHE and _CACHE[0] == cache_key and now - _CACHE[1] < 30:
            return _CACHE[2]
    try:
        response = httpx.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0,
        )
        response.raise_for_status()
        models = frozenset(
            item["id"] for item in response.json().get("data", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise CatalogUnavailable("could not validate models with the configured gateway") from exc
    if not models:
        raise CatalogUnavailable("configured gateway returned an empty model catalog")
    with _LOCK:
        _CACHE = (cache_key, now, models)
    return models
