"""Per-engine TTS plugins that bypass RealtimeTTS.

Each module exposes a TTSProvider subclass and a ``build_provider(params,
secrets)`` callable. The dispatcher in :mod:`services.tts_realtime` calls
it directly when the engine name does not map to a RealtimeTTS engine
class (Edge has its own optimized path; Soniox uses a hand-rolled
WebSocket client because RealtimeTTS does not ship a Soniox engine).
"""
