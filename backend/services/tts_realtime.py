"""RealtimeTTS adapter — implements TTSProvider over the RealtimeTTS library.

Why this exists:
* The legacy MP3 streaming path (/api/tts/{request_id}) expects a TTSProvider
  that yields MP3 bytes. RealtimeTTS engines emit raw PCM via callbacks, so
  this adapter bridges the two: we run RealtimeTTS in a worker thread, capture
  PCM chunks via ``on_audio_chunk``, transcode to MP3 with pydub, and yield
  through an asyncio queue.
* For Edge specifically we bypass RealtimeTTS and reuse the optimized
  EdgeTTSProvider (3-tier chunking, native MP3 from edge-tts) — no PCM
  round-trip, no transcoding overhead.

The same provider object is used by:
* routes/tts.py for MP3 streaming (notifications, chat audio, cron)
* routes/ws_voice.py for raw PCM streaming over WebSocket (hands-free path),
  via ``stream_pcm`` which skips the MP3 transcode.
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Any, AsyncIterator, Optional

from services.tts import TTSProvider, EdgeTTSProvider

logger = logging.getLogger(__name__)


_REALTIMETTS_ENGINE_CLS: dict[str, str] = {
    "edge": "EdgeEngine",
    "system": "SystemEngine",
    "azure": "AzureEngine",
    "elevenlabs": "ElevenlabsEngine",
    "openai": "OpenAIEngine",
}


def _import_engine_cls(engine_name: str):
    """Lazy import RealtimeTTS engine class to avoid heavy imports at startup."""
    cls_name = _REALTIMETTS_ENGINE_CLS.get(engine_name)
    if not cls_name:
        raise ValueError(f"Unknown RealtimeTTS engine: {engine_name!r}")
    import RealtimeTTS  # noqa: F401 — top-level package
    cls = getattr(__import__("RealtimeTTS", fromlist=[cls_name]), cls_name, None)
    if cls is None:
        raise ImportError(f"RealtimeTTS engine class {cls_name!r} not available — install extra '{engine_name}'")
    return cls


def _instantiate_engine(engine_name: str, params: dict[str, Any], secrets: dict[str, str]):
    """Build an engine instance, merging declared params + injected secrets.

    Each engine has slightly different constructor signatures; we forward only
    what's truthy to avoid blowing up on optional kwargs. Secrets win on key
    collision so the form values never override the encrypted ones.
    """
    cls = _import_engine_cls(engine_name)
    kwargs = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    kwargs.update({k: v for k, v in (secrets or {}).items() if v})
    return cls(**kwargs)


class RealtimeTTSProvider(TTSProvider):
    """Adapter wrapping a single RealtimeTTS engine.

    The engine instance is built once at construction; ``stream_audio`` and
    ``stream_pcm`` create a fresh ``TextToAudioStream`` per call so different
    requests don't share queue state.
    """

    def __init__(
        self,
        engine_name: str,
        params: Optional[dict[str, Any]] = None,
        secrets: Optional[dict[str, str]] = None,
        sample_rate: int = 24000,
    ) -> None:
        self.engine_name = engine_name
        self.params = dict(params or {})
        self.sample_rate = sample_rate
        self._engine = _instantiate_engine(engine_name, self.params, secrets or {})

    # ---- TTSProvider API ---------------------------------------------------

    async def generate_audio(self, text: str) -> Optional[bytes]:
        if not text or not text.strip():
            return None
        chunks: list[bytes] = []
        async for chunk in self.stream_audio(text):
            chunks.append(chunk)
        return b"".join(chunks) if chunks else None

    async def stream_audio(self, text: str) -> AsyncIterator[bytes]:
        """Yield MP3 bytes. Internally captures PCM and transcodes via pydub."""
        if not text or not text.strip():
            return
        async for mp3 in self._stream_with_transcode(text, encode_mp3=True):
            yield mp3

    async def stream_pcm(self, text: str) -> AsyncIterator[bytes]:
        """Yield raw PCM (int16, mono) — used by /ws/voice/out for low-latency."""
        if not text or not text.strip():
            return
        async for pcm in self._stream_with_transcode(text, encode_mp3=False):
            yield pcm

    # ---- internals ---------------------------------------------------------

    async def _stream_with_transcode(self, text: str, *, encode_mp3: bool) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        sentinel = object()

        def on_chunk(chunk: bytes) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except asyncio.QueueFull:
                logger.warning("[RealtimeTTS] queue full, dropping chunk (%d bytes)", len(chunk))

        def run() -> None:
            try:
                from RealtimeTTS import TextToAudioStream
                stream = TextToAudioStream(self._engine, muted=True, on_audio_chunk=on_chunk)
                stream.feed(text)
                stream.play()
            except Exception as exc:
                logger.exception("[RealtimeTTS] play failed: %s", exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, sentinel)

        loop.run_in_executor(None, run)

        pcm_buffer = io.BytesIO() if encode_mp3 else None
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            if encode_mp3:
                pcm_buffer.write(item)
            else:
                yield item

        if encode_mp3 and pcm_buffer is not None and pcm_buffer.tell() > 0:
            mp3_bytes = await loop.run_in_executor(None, _pcm_to_mp3, pcm_buffer.getvalue(), self.sample_rate)
            if mp3_bytes:
                yield mp3_bytes


def _pcm_to_mp3(pcm: bytes, sample_rate: int) -> bytes:
    """Transcode raw int16 mono PCM to MP3 via pydub (ffmpeg backend)."""
    try:
        from pydub import AudioSegment
        seg = AudioSegment(data=pcm, sample_width=2, frame_rate=sample_rate, channels=1)
        out = io.BytesIO()
        seg.export(out, format="mp3", bitrate="64k")
        return out.getvalue()
    except Exception as exc:
        logger.error("[RealtimeTTS] PCM→MP3 transcode failed: %s", exc)
        return b""


# ---- factory ---------------------------------------------------------------


def build_chat_provider(config: dict[str, Any], secrets: Optional[dict[str, dict[str, str]]] = None) -> TTSProvider:
    """Build the chat TTS provider from a registry config dict.

    ``config`` shape (single source of truth, stored as JSON in system_config):
        {"engine": "edge", "params": {"voice": "...", "rate": "+20%"}}

    Edge bypasses RealtimeTTS for performance (3-tier chunking, native MP3).
    Other engines go through RealtimeTTS via :class:`RealtimeTTSProvider`.
    """
    engine = (config or {}).get("engine") or "edge"
    params = (config or {}).get("params") or {}

    if engine == "edge":
        return EdgeTTSProvider(
            voice=params.get("voice") or "vi-VN-NamMinhNeural",
            rate=params.get("rate") or "+20%",
        )

    engine_secrets = (secrets or {}).get(engine, {})
    return RealtimeTTSProvider(engine_name=engine, params=params, secrets=engine_secrets)


def build_stories_provider(config: dict[str, Any]) -> EdgeTTSProvider:
    """Build the stories TTS provider — always Edge, by design.

    Returns ``EdgeTTSProvider`` concretely (not the abstract type) so callers
    can rely on the locked engine choice at type level.
    """
    cfg = config or {}
    return EdgeTTSProvider(
        voice=cfg.get("voice") or "vi-VN-NamMinhNeural",
        rate=cfg.get("rate") or "+20%",
    )
