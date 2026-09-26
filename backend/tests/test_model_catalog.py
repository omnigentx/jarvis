"""A catalog entry is not accepted until a tiny inference request succeeds."""
from unittest.mock import MagicMock

import httpx
import pytest

from services import model_catalog


def test_bounded_probe_rejects_listed_but_unusable_model(monkeypatch):
    monkeypatch.setattr(model_catalog, "_gateway", lambda: ("https://gateway.example/v1", "test-key"))
    model_catalog._PROBE_CACHE.clear()
    response = httpx.Response(403, json={"error": {"message": "invalid bearer"}},
                              request=httpx.Request("POST", "https://gateway.example/v1/chat/completions"))
    send = MagicMock(return_value=response)
    monkeypatch.setattr(model_catalog.httpx, "post", send)
    with pytest.raises(model_catalog.CatalogUnavailable, match="bounded inference probe"):
        model_catalog.probe_model("kr/gpt-5.6-luna")
    assert send.call_args.kwargs["json"]["max_tokens"] == 1
    assert send.call_args.kwargs["timeout"] == 15.0


def test_successful_probe_is_cached_for_a_short_interval(monkeypatch):
    monkeypatch.setattr(model_catalog, "_gateway", lambda: ("https://gateway.example/v1", "test-key"))
    model_catalog._PROBE_CACHE.clear()
    response = httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]},
                              request=httpx.Request("POST", "https://gateway.example/v1/chat/completions"))
    send = MagicMock(return_value=response)
    monkeypatch.setattr(model_catalog.httpx, "post", send)
    model_catalog.probe_model("cx/gpt-6-luna")
    model_catalog.probe_model("cx/gpt-6-luna")
    assert send.call_count == 1
