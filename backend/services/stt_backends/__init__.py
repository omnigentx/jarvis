"""Per-backend STT plugins.

Each module in this package exposes a top-level ``build(config)`` callable
that returns an STT service object duck-typed to ``RealtimeSTTService``
(``feed_audio``, ``set_hook``, ``start_listen_loop``, ``shutdown``,
plus the ``on_*`` callback methods).

The dispatcher in :mod:`services.stt_realtime` looks up the right
backend module by ``config["backend"]`` and calls its ``build(config)``.
Adding a new engine is a 2-touch change: add a registry entry plus a
new module here. Nothing in ``ws_voice`` or ``runtime_config`` needs to
care which backend produced the service.
"""
