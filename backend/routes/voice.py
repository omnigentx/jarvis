"""Voice routes — registry surface for the Setup Wizard and Voice settings tab.

Endpoints:
* ``GET  /api/voice/engines``               — full registry (TTS + STT) so the
  UI can render forms generically. Secrets are listed by KEY ONLY.
* ``GET  /api/voice/engines/{name}/voices`` — probe live voices for an engine
  (Edge: edge-tts catalog; ElevenLabs: API; etc).
* ``GET  /api/voice/active``                — current active config from DB.
* ``POST /api/voice/active``                — persist a new active config; the
  config-change listener rebuilds the provider singleton in-process.
* ``POST /api/voice/secrets/{engine}/{key}`` — persist an encrypted secret.
* ``POST /api/voice/test/tts``              — synthesize a short sample and
  return MP3 bytes (preview button in UI).
* ``GET  /api/voice/requirements/{engine}`` — best-effort prerequisite check
  (binaries on PATH, etc).

Settings CRUD (history/import/export) rides on the existing
``/api/settings/{category}/{key}`` endpoints with category=``voice`` — no
duplicate plumbing here.
"""
from __future__ import annotations

import io
import logging
import shutil
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.auth import verify_api_key
from services import voice_engine_registry as registry
from services import voice_config as vc
from services import shared_state as state

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/voice", tags=["voice"])


# ---- Schemas --------------------------------------------------------------


class TTSChatConfigPayload(BaseModel):
    engine: str
    params: dict[str, Any] = Field(default_factory=dict)


class TTSStoriesConfigPayload(BaseModel):
    voice: Optional[str] = None
    rate: Optional[str] = None


class STTConfigPayload(BaseModel):
    backend: str
    params: dict[str, Any] = Field(default_factory=dict)
    wake_word: dict[str, Any] = Field(default_factory=lambda: {"backend": "off", "params": {}})


class ActiveConfigPayload(BaseModel):
    tts_chat: Optional[TTSChatConfigPayload] = None
    tts_stories: Optional[TTSStoriesConfigPayload] = None
    stt: Optional[STTConfigPayload] = None


class TestTTSPayload(BaseModel):
    text: str
    engine: Optional[str] = None  # if None, uses currently active chat config
    params: Optional[dict[str, Any]] = None


class SecretPayload(BaseModel):
    value: str


# ---- helpers --------------------------------------------------------------


def _config_service():
    from services.config_service import config_service
    return config_service


# ---- endpoints ------------------------------------------------------------


@router.get("/engines")
async def list_engines(_=Depends(verify_api_key)):
    """Return the full registry — TTS engines + STT backends + their schemas.

    Secrets are surfaced as a list of slot names; their plaintext is never
    included. Frontend renders an obfuscated input + "set" button per slot.
    """
    return {
        "tts": registry.list_tts_engines(),
        "stt": registry.list_stt_backends(),
    }


@router.get("/engines/{engine}/voices")
async def list_engine_voices(engine: str, _=Depends(verify_api_key)):
    """Live voice catalog for an engine. Best-effort — falls back to defaults
    if a probe fails (e.g. no network, missing API key).
    """
    if engine == "edge":
        try:
            import edge_tts
            voices = await edge_tts.list_voices()
            return {"voices": [{"id": v["ShortName"], "label": f"{v['ShortName']} ({v['Locale']})"} for v in voices]}
        except Exception as exc:
            logger.warning("[voice] edge voice probe failed: %s", exc)
            return {"voices": [], "error": str(exc)}
    spec = registry.get_tts_engine(engine)
    if not spec:
        raise HTTPException(404, f"Unknown engine {engine!r}")
    # Static fallback: read defaults from spec
    voice_param = next((p for p in spec.get("params", []) if p["key"] == "voice"), None)
    return {"voices": [{"id": o, "label": o} for o in (voice_param or {}).get("options", [])]}


@router.get("/active")
async def get_active_config(_=Depends(verify_api_key)):
    cs = _config_service()
    return {
        "tts_chat": vc.get_chat_config(cs),
        "tts_stories": vc.get_stories_config(cs),
        "stt": vc.get_stt_config(cs),
    }


@router.post("/active")
async def set_active_config(payload: ActiveConfigPayload, _=Depends(verify_api_key)):
    """Persist any subset of the active voice config. Hot-reload happens via
    the ConfigService change listener — no manual provider rebuild here.
    """
    cs = _config_service()
    try:
        if payload.tts_chat is not None:
            vc.set_chat_config(cs, payload.tts_chat.model_dump())
        if payload.tts_stories is not None:
            # Drop None fields so the locked schema stays clean.
            stories = {k: v for k, v in payload.tts_stories.model_dump().items() if v is not None}
            vc.set_stories_config(cs, stories)
        if payload.stt is not None:
            vc.set_stt_config(cs, payload.stt.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"status": "ok"}


@router.get("/secrets")
async def list_secrets_status(_=Depends(verify_api_key)):
    """Per-engine secret status — slot name + has_value bool, no plaintext.

    UI uses this to render "Set" / "Not set" badges on each engine card so
    the user knows whether they can pick a paid engine without entering keys
    again.
    """
    cs = _config_service()
    out: dict[str, dict[str, bool]] = {}
    for engine, spec in registry.list_tts_engines().items():
        slots = spec.get("secrets", []) or []
        if not slots:
            continue
        out[engine] = {
            slot: bool(cs.get("voice", f"secrets.{engine}.{slot}"))
            for slot in slots
        }
    return {"engines": out}


@router.post("/secrets/{engine}/{slot}")
async def set_secret(engine: str, slot: str, body: SecretPayload, _=Depends(verify_api_key)):
    cs = _config_service()
    try:
        vc.set_engine_secret(cs, engine, slot, body.value)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"status": "ok"}


@router.delete("/secrets/{engine}/{slot}")
async def delete_secret(engine: str, slot: str, _=Depends(verify_api_key)):
    """Clear a previously-set secret — frees the user from manual SQL surgery
    when rotating away from a paid engine.
    """
    spec = registry.get_tts_engine(engine)
    if not spec or slot not in spec.get("secrets", []):
        raise HTTPException(400, f"Engine {engine!r} has no declared secret {slot!r}")
    cs = _config_service()
    cs.set("voice", f"secrets.{engine}.{slot}", None)  # None deletes the row
    return {"status": "ok"}


@router.get("/requirements/{engine}")
async def check_requirements(engine: str, _=Depends(verify_api_key)):
    spec = registry.get_tts_engine(engine)
    if not spec:
        raise HTTPException(404, f"Unknown engine {engine!r}")
    cs = _config_service()
    missing_bins = [b for b in spec.get("requires", []) if shutil.which(b) is None]
    secrets_present = {
        s: bool(cs.get("voice", f"secrets.{engine}.{s}"))
        for s in spec.get("secrets", [])
    }
    return {
        "ok": not missing_bins and all(secrets_present.values()),
        "missing_binaries": missing_bins,
        "secrets_present": secrets_present,
    }


@router.post("/test/stt")
async def test_stt(_=Depends(verify_api_key)):
    """Quick STT smoke-test — transcribes RealtimeSTT's bundled warmup_audio.wav
    using faster-whisper directly. Returns the transcript so users can confirm
    the model + language pipeline before granting mic permissions.

    Why not go through ``AudioToTextRecorder``: the recorder's ``.text()`` is
    designed for live mic capture and blocks until VAD detects an
    end-of-speech transition. Feeding a pre-recorded WAV once doesn't
    complete that cycle, so the call would hang forever. faster-whisper's
    ``WhisperModel.transcribe(path)`` is the right primitive for a
    file-based smoke probe.
    """
    from services import voice_config as _vc
    cs = _config_service()
    cfg = _vc.get_stt_config(cs)
    params = cfg.get("params") or {}
    # Force tiny model so the probe stays fast; users can still configure
    # bigger models for real transcription via the WS path.
    model_size = "tiny"
    compute_type = params.get("compute_type") or "int8"
    language = params.get("language") or "auto"
    if language == "auto":
        language = None  # let Whisper auto-detect

    try:
        from pathlib import Path
        import RealtimeSTT
        pkg_dir = Path(list(RealtimeSTT.__path__)[0])
        warmup = pkg_dir / "warmup_audio.wav"
        if not warmup.exists():
            raise HTTPException(500, f"warmup audio not found at {warmup}")

        # Lazy import — keeps startup time low for non-voice flows.
        from faster_whisper import WhisperModel
        import asyncio

        def _transcribe() -> str:
            # Run sync model load + transcription off the event loop so the
            # FastAPI worker stays responsive (model cold-load ~2s, hot ~0.5s).
            # faster-whisper segments come with their own leading whitespace,
            # so concatenate without an extra separator.
            model = WhisperModel(model_size, compute_type=compute_type)
            segments, _info = model.transcribe(str(warmup), language=language)
            return "".join(s.text for s in segments).strip()

        text = await asyncio.to_thread(_transcribe)
        return {"transcript": text or ""}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"STT test failed: {exc}")


@router.post("/test/tts")
async def test_tts(payload: TestTTSPayload, _=Depends(verify_api_key)):
    """Synthesize a short text sample and stream the MP3 back for preview."""
    if not payload.text or not payload.text.strip():
        raise HTTPException(400, "text must not be empty")

    cs = _config_service()
    if payload.engine:
        config = {"engine": payload.engine, "params": payload.params or {}}
    else:
        config = vc.get_chat_config(cs)

    secrets = vc.get_engine_secrets(cs, config["engine"])
    from services.tts_realtime import build_chat_provider
    provider = build_chat_provider(config, secrets={config["engine"]: secrets})

    async def _gen():
        async for chunk in provider.stream_audio(payload.text):
            if chunk:
                yield chunk

    return StreamingResponse(_gen(), media_type="audio/mpeg")
