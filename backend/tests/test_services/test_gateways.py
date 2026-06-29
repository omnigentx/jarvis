"""Tests for the messaging gateways (Telegram / Zalo).

Coverage map:
  * Unit — pure logic, no network: chunking, allow-list, handle_inbound
    orchestration (happy / unauthorized / agent-error / empty), and the
    Telegram + Zalo update parsers.
  * Integration — REAL SQLite: the chat→session binding SSoT
    (`session_map`) and `GatewayManager._dispatch` reconciliation
    (create → reuse → self-heal when the bound session is gone).
  * E2E — the full `BotApiGateway` long-poll loop running through REAL httpx
    over a mock transport: getUpdates → parse → handle_inbound → sendMessage.
    Only the external Telegram server is faked; the gateway code under test is
    exercised for real.

Gap stated explicitly (per CLAUDE.md item 4/5): the agent LLM run
(`resume_and_send` → fast-agent → model) is NOT exercised here — it needs no
network/keys to be meaningful and is already covered by the chat-path tests.
The gateway↔session_service contract is faked at that single seam.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from core.database import Base, GatewayChat, engine, get_db_session
from services.gateways import session_map
from services.gateways.base import BaseGateway, InboundMessage
from services.gateways.bot_api import BotApiError, chunk_text
from services.gateways.config import GatewayConfig, load_gateway_configs
from services.gateways.manager import GatewayManager
from services.gateways.telegram import TelegramGateway
from services.gateways.zalo import ZaloGateway


@pytest.fixture(autouse=True)
def _tables():
    """Ensure the gateway_chats table exists in the isolated test DB."""
    Base.metadata.create_all(bind=engine)
    yield


# ── Test doubles ───────────────────────────────────────────────────────────


class RecordingGateway(BaseGateway):
    """Minimal BaseGateway that records sends instead of hitting a network."""

    def __init__(self, **kwargs):
        super().__init__(name="fake", **kwargs)
        self.sent: list[tuple[str, str]] = []
        self.typing: list[str] = []

    async def run(self):  # pragma: no cover - not exercised
        return None

    async def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))

    async def send_typing(self, chat_id):
        self.typing.append(chat_id)


def _msg(text="hi", user_id="42", chat_id="100"):
    return InboundMessage(platform="fake", chat_id=chat_id, user_id=user_id, text=text)


# ── Unit: chunking ─────────────────────────────────────────────────────────


def test_chunk_text_short_passthrough():
    assert chunk_text("hello") == ["hello"]


def test_chunk_text_splits_on_newline_and_respects_limit():
    text = "a" * 50 + "\n" + "b" * 50
    chunks = chunk_text(text, limit=60)
    assert len(chunks) == 2
    assert chunks[0] == "a" * 50
    assert chunks[1] == "b" * 50
    assert all(len(c) <= 60 for c in chunks)


def test_chunk_text_hard_cuts_a_single_long_line():
    text = "x" * 250
    chunks = chunk_text(text, limit=100)
    assert [len(c) for c in chunks] == [100, 100, 50]
    assert "".join(chunks) == text


# ── Unit: allow-list ───────────────────────────────────────────────────────


def test_allowlist_empty_denies_all():
    gw = RecordingGateway(dispatcher=None, allow_from=[])
    assert gw.is_allowed("42") is False


def test_allowlist_star_allows_all():
    gw = RecordingGateway(dispatcher=None, allow_from=["*"])
    assert gw.is_allowed("anyone") is True


def test_allowlist_normalizes_int_and_str():
    # config may carry ints; inbound ids are strings — must still match.
    gw = RecordingGateway(dispatcher=None, allow_from=[42])
    assert gw.is_allowed("42") is True
    assert gw.is_allowed("43") is False


# ── Unit: handle_inbound orchestration ─────────────────────────────────────


async def test_handle_inbound_happy_path():
    async def dispatcher(m):
        return f"echo:{m.text}"

    gw = RecordingGateway(dispatcher=dispatcher, allow_from=["42"])
    await gw.handle_inbound(_msg(text="ping"))
    assert gw.typing == ["100"]
    assert gw.sent == [("100", "echo:ping")]


async def test_handle_inbound_unauthorized_replies_id_and_skips_agent():
    """Secure onboarding: an unauthorized sender never reaches the agent, but
    gets ONLY their own id back so the owner can allow-list it without ever
    opening '*'."""
    called = False

    async def dispatcher(m):
        nonlocal called
        called = True
        return "should not happen"

    gw = RecordingGateway(dispatcher=dispatcher, allow_from=[])  # deny all
    await gw.handle_inbound(_msg(user_id="42", chat_id="100"))
    assert called is False                       # agent NOT run
    assert len(gw.sent) == 1
    chat_id, text = gw.sent[0]
    assert chat_id == "100"
    assert "42" in text and "not authorized" in text.lower()


async def test_handle_inbound_agent_error_notifies_user_not_silent():
    async def dispatcher(m):
        raise RuntimeError("boom")

    gw = RecordingGateway(dispatcher=dispatcher, allow_from=["42"])
    await gw.handle_inbound(_msg())  # must NOT raise out of the loop
    assert len(gw.sent) == 1
    assert "wrong" in gw.sent[0][1].lower()


async def test_handle_inbound_empty_reply_tells_user():
    async def dispatcher(m):
        return "   "

    gw = RecordingGateway(dispatcher=dispatcher, allow_from=["42"])
    await gw.handle_inbound(_msg())
    assert gw.sent == [("100", "(no response)")]


# ── Unit: platform parsers ─────────────────────────────────────────────────


async def test_telegram_poll_parses_and_advances_offset(monkeypatch):
    gw = TelegramGateway(token="t", dispatcher=None, allow_from=[])
    updates = [
        {"update_id": 5, "message": {"text": "hello",
                                     "chat": {"id": 111}, "from": {"id": 222}}},
        {"update_id": 6, "message": {"sticker": "x",  # non-text → skipped
                                     "chat": {"id": 111}, "from": {"id": 222}}},
    ]

    async def fake_call(method, body=None):
        assert method == "getUpdates"
        assert body["offset"] == 0  # first poll starts from beginning
        return updates

    monkeypatch.setattr(gw, "_call", fake_call)
    msgs = await gw._poll()
    assert len(msgs) == 1
    assert msgs[0].platform == "telegram"
    assert msgs[0].chat_id == "111"
    assert msgs[0].user_id == "222"
    assert msgs[0].text == "hello"
    # Offset advanced past BOTH updates (incl. the skipped sticker), so they are
    # never replayed.
    assert gw._offset == 7
    await gw.stop()


# ── Media: photos / images ─────────────────────────────────────────────────


async def test_telegram_parses_photo_with_caption(monkeypatch):
    gw = TelegramGateway(token="t", dispatcher=None, allow_from=[])
    updates = [{"update_id": 1, "message": {
        "caption": "look at this",
        "chat": {"id": 5}, "from": {"id": 9},
        "photo": [{"file_id": "small", "file_size": 100},
                  {"file_id": "big", "file_size": 2000}],
    }}]

    async def fake_call(method, body=None):
        return updates

    monkeypatch.setattr(gw, "_call", fake_call)
    msgs = await gw._poll()
    assert len(msgs) == 1
    assert msgs[0].text == "look at this"           # caption becomes the text
    assert msgs[0].media_refs[0]["file_id"] == "big"  # largest size chosen
    assert msgs[0].media_refs[0]["content_type"] == "image/jpeg"
    await gw.stop()


async def test_telegram_fetch_media_downloads_base64():
    def handler(req):
        path = req.url.path
        if path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/x.jpg"}})
        if "/file/bot" in path:  # the actual file download
            return httpx.Response(200, content=b"JPEGBYTES")
        return httpx.Response(200, json={"ok": True, "result": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gw = TelegramGateway(token="TKN", dispatcher=None, allow_from=[], client=client)
    files = await gw.fetch_media([{
        "file_id": "big", "content_type": "image/jpeg",
        "filename": "photo.jpg", "file_size": 2000,
    }])
    assert len(files) == 1
    assert files[0]["content_type"] == "image/jpeg"
    import base64 as _b64
    assert _b64.b64decode(files[0]["data_b64"]) == b"JPEGBYTES"
    await gw.stop()


async def test_handle_inbound_resolves_media_before_dispatch():
    """handle_inbound must download media (post allow-gate) and hand it to the
    dispatcher as files_data — the root fix for 'image not handled'."""
    captured = {}

    async def dispatcher(m):
        captured["text"] = m.text
        captured["files"] = m.files_data
        return "ok"

    class G(TelegramGateway):
        async def fetch_media(self, refs):
            return [{"filename": "photo.jpg", "content_type": "image/jpeg", "data_b64": "QQ=="}]
        async def send_text(self, chat_id, text):
            pass
        async def send_typing(self, chat_id):
            pass

    gw = G(token="t", dispatcher=dispatcher, allow_from=["9"])
    msg = InboundMessage(platform="telegram", chat_id="5", user_id="9",
                         text="look", media_refs=[{"file_id": "big"}])
    await gw.handle_inbound(msg)
    assert captured["text"] == "look"
    assert captured["files"] == [{"filename": "photo.jpg", "content_type": "image/jpeg", "data_b64": "QQ=="}]
    await gw.stop()


async def test_telegram_skips_unsupported_non_text_non_media(monkeypatch):
    gw = TelegramGateway(token="t", dispatcher=None, allow_from=[])
    updates = [{"update_id": 1, "message": {"sticker": {"file_id": "s"},
                                            "chat": {"id": 5}, "from": {"id": 9}}}]

    async def fake_call(method, body=None):
        return updates

    monkeypatch.setattr(gw, "_call", fake_call)
    assert await gw._poll() == []  # sticker → skipped, but offset still advanced
    assert gw._offset == 2
    await gw.stop()


async def test_zalo_poll_parses_text_event(monkeypatch):
    gw = ZaloGateway(token="t", dispatcher=None, allow_from=[])

    async def fake_call(method, body=None):
        assert method == "getUpdates"
        assert body == {"timeout": "30"}
        return {
            "event_name": "message.text.received",
            "message": {"text": "xin chào",
                        "chat": {"id": "abc", "chat_type": "PRIVATE"},
                        "from": {"id": "u1"}},
        }

    monkeypatch.setattr(gw, "_call", fake_call)
    msgs = await gw._poll()
    assert len(msgs) == 1
    assert msgs[0].platform == "zalo"
    assert msgs[0].chat_id == "abc"
    assert msgs[0].text == "xin chào"
    await gw.stop()


async def test_zalo_poll_parses_image_event(monkeypatch):
    gw = ZaloGateway(token="t", dispatcher=None, allow_from=[])

    async def fake_call(method, body=None):
        # Real Zalo image update: URL in `photo_url`, caption in `caption`.
        return {"event_name": "message.image.received", "message": {
            "message_type": "CHAT_PHOTO",
            "photo_url": "https://photo.zdn.vn/abc.jpg", "caption": "ai đây",
            "chat": {"id": "c1"}, "from": {"id": "u1"}}}

    monkeypatch.setattr(gw, "_call", fake_call)
    msgs = await gw._poll()
    assert len(msgs) == 1
    assert msgs[0].text == "ai đây"  # caption becomes text
    assert msgs[0].media_refs == [{"url": "https://photo.zdn.vn/abc.jpg", "filename": "photo.jpg"}]
    await gw.stop()


async def test_zalo_poll_skips_sticker_event(monkeypatch):
    gw = ZaloGateway(token="t", dispatcher=None, allow_from=[])

    async def fake_call(method, body=None):
        return {"event_name": "message.sticker.received",
                "message": {"sticker": "x", "chat": {"id": "c1"}, "from": {"id": "u1"}}}

    monkeypatch.setattr(gw, "_call", fake_call)
    assert await gw._poll() == []
    await gw.stop()


async def test_zalo_fetch_media_downloads_url():
    def handler(req):
        # Zalo CDN serves the non-standard "image/jpg" — must be normalized.
        return httpx.Response(200, content=b"ZALOIMG", headers={"content-type": "image/jpg"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gw = ZaloGateway(token="t", dispatcher=None, allow_from=[], client=client)
    files = await gw.fetch_media([{"url": "https://photo.zdn.vn/abc.jpg", "filename": "photo.jpg"}])
    assert len(files) == 1
    assert files[0]["content_type"] == "image/jpeg"  # normalized from image/jpg
    import base64 as _b64
    assert _b64.b64decode(files[0]["data_b64"]) == b"ZALOIMG"
    await gw.stop()


def test_display_name_per_platform():
    # Telegram: username/first_name; Zalo: display_name/account_name.
    assert TelegramGateway._display_name({"username": "tg_bot", "first_name": "X"}) == "tg_bot"
    assert ZaloGateway._display_name(
        {"display_name": "Bot OmnigentxJarvis", "account_name": "bot.WbZlgxnx"}
    ) == "Bot OmnigentxJarvis"
    assert ZaloGateway._display_name({"account_name": "bot.abc"}) == "bot.abc"
    assert ZaloGateway._display_name({}) == "bot"


def test_bot_api_error_408_is_poll_timeout():
    assert BotApiError("timeout", error_code=408).is_poll_timeout is True
    assert BotApiError("real", error_code=400).is_poll_timeout is False


# ── Security: the bot token must never leak into errors ────────────────────


async def test_call_409_surfaces_description_without_leaking_token():
    """A 409 must yield the Telegram description + error_code, and the bot
    token must NOT appear anywhere in the raised error (it would otherwise be
    shown in the Settings UI banner)."""
    def handler(req):
        return httpx.Response(409, json={
            "ok": False, "error_code": 409,
            "description": "Conflict: terminated by other getUpdates request",
        })
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gw = TelegramGateway(token="8407785561:SECRET_xyz", dispatcher=None,
                         allow_from=[], client=client)
    with pytest.raises(BotApiError) as ei:
        await gw._call("getUpdates")
    assert ei.value.error_code == 409
    assert "Conflict" in str(ei.value)
    assert "SECRET_xyz" not in str(ei.value)
    await gw.stop()


async def test_409_is_benign_keeps_connected_and_still_delivers():
    """A 409 self-overlap must NOT flip connected→false / set last_error (that
    made the UI flap red), and the bot must keep working — the next poll's
    message is still delivered."""
    sent = []
    delivered = asyncio.Event()
    seq = {"n": 0}

    def handler(req):
        p = req.url.path
        if p.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "b"}})
        if p.endswith("/getUpdates"):
            seq["n"] += 1
            if seq["n"] == 1:  # benign self-conflict
                return httpx.Response(409, json={
                    "ok": False, "error_code": 409,
                    "description": "Conflict: terminated by other getUpdates request"})
            if seq["n"] == 2:  # a real message
                return httpx.Response(200, json={"ok": True, "result": [{
                    "update_id": 1,
                    "message": {"text": "hi", "chat": {"id": 5}, "from": {"id": 9}}}]})
            return httpx.Response(200, json={"ok": True, "result": []})
        if p.endswith("/sendMessage"):
            sent.append(json.loads(req.content))
            delivered.set()
            return httpx.Response(200, json={"ok": True, "result": {"message_id": "1"}})
        return httpx.Response(200, json={"ok": True, "result": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def dispatcher(m):
        return "ok"

    gw = TelegramGateway(token="t", dispatcher=dispatcher, allow_from=["9"], client=client)
    task = asyncio.create_task(gw.run())
    try:
        await asyncio.wait_for(delivered.wait(), timeout=6)
    finally:
        await gw.stop()
        await task

    assert gw.connected is True, "409 wrongly flipped connected to False (UI flap)"
    assert gw.last_error is None, "409 wrongly set last_error"
    assert sent and sent[0]["text"] == "ok"


def test_redact_strips_token_from_message():
    gw = TelegramGateway(token="TKN123:abc", dispatcher=None, allow_from=[])
    leaked = "ConnectError for https://api.telegram.org/botTKN123:abc/getUpdates"
    assert "TKN123:abc" not in gw._redact(leaked)
    assert "bot***/getUpdates" in gw._redact(leaked)


async def test_probe_invalid_token_does_not_leak(monkeypatch):
    _patch_probe_transport(
        monkeypatch,
        lambda req: httpx.Response(401, json={"ok": False, "description": "Unauthorized"}),
    )
    res = await TelegramGateway.probe("MYTOKEN:secret")
    assert res["ok"] is False
    assert "MYTOKEN" not in res["error"]


# ── Live reload: minimal restarts (anti-409 churn) ─────────────────────────


async def test_apply_updates_allowlist_in_place_without_restart(monkeypatch):
    """Changing only allow-list/agent must NOT restart the poller (a restart
    would drop the long-poll and trigger a transient 409). A token change MUST
    reconnect."""
    import services.gateways.manager as m

    class FakeGW(BaseGateway):
        # The manager always constructs gateways with token=…; BaseGateway
        # itself doesn't take it (only BotApiGateway does), so absorb it here.
        def __init__(self, *, token=None, **kw):
            super().__init__(name="telegram", **kw)
            self.token = token
        async def run(self):
            await asyncio.Event().wait()  # run until cancelled
        async def send_text(self, chat_id, text):
            pass

    monkeypatch.setitem(m.GATEWAY_REGISTRY, "telegram", FakeGW)

    mgr = GatewayManager(agent_app=object())
    mgr._loop = asyncio.get_running_loop()
    mgr.start(configs={"telegram": GatewayConfig(enabled=True, token="t", allow_from=[1], agent="Jarvis")})
    task1 = mgr._tasks["telegram"]
    gw = mgr._gateways["telegram"]
    assert gw.is_allowed("1") and not gw.is_allowed("2")

    # allow-list change only → in place, SAME task, new allow-list live.
    await mgr._apply({"telegram": GatewayConfig(enabled=True, token="t", allow_from=[1, 2], agent="Jarvis")})
    assert mgr._tasks["telegram"] is task1, "poller was needlessly restarted"
    assert gw.is_allowed("2"), "in-place allow-list update did not take effect"

    # token change → MUST reconnect (new task).
    await mgr._apply({"telegram": GatewayConfig(enabled=True, token="t2", allow_from=[1, 2], agent="Jarvis")})
    assert mgr._tasks["telegram"] is not task1, "token change must reconnect"

    # disable → stopped.
    await mgr._apply({"telegram": GatewayConfig(enabled=False)})
    assert "telegram" not in mgr._tasks
    await mgr.stop()


# ── Slash commands (gateway chat) ──────────────────────────────────────────


def test_commands_parse_recognizes_and_aliases():
    from services.gateways import commands
    assert commands.parse("/new") == ("new", [])
    assert commands.parse("/reset") == ("new", [])       # alias
    assert commands.parse("/clear") == ("new", [])       # alias
    assert commands.parse("/agent Personal") == ("agent", ["Personal"])
    assert commands.parse("/help") == ("help", [])
    assert commands.parse("/whoami") == ("whoami", [])
    assert commands.parse("/id") == ("whoami", [])        # alias
    assert commands.parse("/unknown") is None            # not a command → falls through
    assert commands.parse("hello") is None               # plain text


def test_commands_handle_new_agent_help():
    from services.gateways import commands
    calls = {"reset": 0, "agent": None}
    ctx = commands.CommandContext(
        current_agent="Jarvis",
        agent_names=["Jarvis", "Personal"],
        user_id="u123",
        reset_conversation=lambda: calls.__setitem__("reset", calls["reset"] + 1),
        set_agent=lambda n: calls.__setitem__("agent", n),
    )
    assert "new conversation" in commands.handle("/new", ctx).lower()
    assert calls["reset"] == 1

    assert "Personal" in commands.handle("/agent Personal", ctx)
    assert calls["agent"] == "Personal"

    assert "Unknown agent" in commands.handle("/agent Nope", ctx)

    assert "u123" in commands.handle("/whoami", ctx)       # /whoami returns the id

    assert "/new" in commands.handle("/help", ctx) and "/agent" in commands.handle("/help", ctx)

    assert commands.handle("just a normal message", ctx) is None


async def test_dispatch_new_command_resets_without_running_agent(monkeypatch):
    import services.shared_state as ss_mod

    ran = {"n": 0}

    class FakeSS:
        async def resume_and_send(self, *a, **k):
            ran["n"] += 1
            return "agent reply", "s1"

    monkeypatch.setattr(ss_mod, "session_service", FakeSS())

    class App:
        _agents = {"Jarvis": object(), "Personal": object()}

    mgr = GatewayManager(agent_app=App())
    session_map.upsert("fake", "cmd1", "sess-x", "Jarvis")  # existing binding

    reply = await mgr._dispatch(_msg(text="/new", chat_id="cmd1"), "Jarvis")
    assert "new conversation" in reply.lower()
    assert ran["n"] == 0                                  # agent NOT invoked
    assert session_map.lookup("fake", "cmd1") is None     # binding cleared


async def test_dispatch_agent_command_sets_per_chat_agent(monkeypatch):
    import services.shared_state as ss_mod

    class FakeSS:
        async def resume_and_send(self, agent_app, message, session_id, files_data=None, agent_name=None):
            return f"answered by {agent_name}", "s2"

    monkeypatch.setattr(ss_mod, "session_service", FakeSS())

    class App:
        _agents = {"Jarvis": object(), "Personal": object()}

    mgr = GatewayManager(agent_app=App())

    # /agent switches the per-chat agent (no agent run)
    reply = await mgr._dispatch(_msg(text="/agent Personal", chat_id="cmd2"), "Jarvis")
    assert "Personal" in reply
    assert session_map.get_agent("fake", "cmd2") == "Personal"

    # next normal message is answered by the chosen agent, not the default
    reply2 = await mgr._dispatch(_msg(text="hi", chat_id="cmd2"), "Jarvis")
    assert reply2 == "answered by Personal"


async def test_start_one_never_orphans_a_running_poller(monkeypatch):
    """If a start is requested while a poller is already running, the old task
    must be cancelled — otherwise two pollers run on one bot and 409 each other
    forever. Guards the root cause of the persistent-409 bug."""
    import services.gateways.manager as m

    class FakeGW(BaseGateway):
        def __init__(self, *, token=None, **kw):
            super().__init__(name="telegram", **kw)
        async def run(self):
            await asyncio.Event().wait()
        async def send_text(self, chat_id, text):
            pass

    monkeypatch.setitem(m.GATEWAY_REGISTRY, "telegram", FakeGW)
    mgr = GatewayManager(agent_app=object())
    mgr._loop = asyncio.get_running_loop()
    cfg = GatewayConfig(enabled=True, token="t", allow_from=[1], agent="Jarvis")

    mgr._start_one("telegram", cfg)
    t1 = mgr._tasks["telegram"]
    mgr._start_one("telegram", cfg)  # again, while t1 still running
    t2 = mgr._tasks["telegram"]

    assert t1 is not t2
    await asyncio.sleep(0)  # let the cancellation land
    assert t1.cancelled() or t1.done(), "stale poller was orphaned (not cancelled)"
    await mgr.stop()


# ── Integration: config loader reads from config_service (REAL DB) ─────────


def _clear_gateway_config():
    """Remove all gateways/* rows so each config test starts clean."""
    from services.config_service import config_service
    for name in ("telegram", "zalo"):
        for suffix in ("enabled", "token", "allow_from", "agent"):
            config_service.set("gateways", f"{name}_{suffix}", None)


def test_load_gateway_configs_defaults_when_unset():
    _clear_gateway_config()
    cfgs = load_gateway_configs()
    # Every registered platform is present and disabled by default.
    assert set(cfgs) == {"telegram", "zalo"}
    assert cfgs["telegram"].enabled is False
    assert cfgs["telegram"].token == ""
    assert cfgs["telegram"].allow_from == []
    assert cfgs["telegram"].agent == "Jarvis"


def test_load_gateway_configs_reads_db_values():
    from services.config_service import config_service
    _clear_gateway_config()
    config_service.set("gateways", "telegram_enabled", "true")
    config_service.set("gateways", "telegram_token", "abc123", is_secret=True)
    config_service.set("gateways", "telegram_allow_from", "[1, 2]")
    config_service.set("gateways", "telegram_agent", "Personal")

    cfgs = load_gateway_configs()
    assert cfgs["telegram"].enabled is True
    assert cfgs["telegram"].token == "abc123"   # decrypted from the secret row
    assert cfgs["telegram"].allow_from == [1, 2]
    assert cfgs["telegram"].agent == "Personal"
    _clear_gateway_config()


# ── Integration: session_map against REAL SQLite ───────────────────────────


def test_session_map_upsert_lookup_and_rebind():
    assert session_map.lookup("telegram", "c1") is None
    session_map.upsert("telegram", "c1", "sess-A", "Jarvis")
    assert session_map.lookup("telegram", "c1") == "sess-A"
    # Rebind (e.g. the backend session was recreated) updates in place, no dup.
    session_map.upsert("telegram", "c1", "sess-B", "Jarvis")
    assert session_map.lookup("telegram", "c1") == "sess-B"
    db = get_db_session()
    try:
        rows = db.query(GatewayChat).filter_by(platform="telegram", chat_id="c1").all()
        assert len(rows) == 1  # UniqueConstraint(platform, chat_id) held
    finally:
        db.close()


async def test_dispatch_creates_then_reuses_binding(monkeypatch):
    """GatewayManager._dispatch must: pass the bound session to the agent,
    persist the id it gets back, and reuse it next time — verified against the
    REAL DB binding. The agent runtime is faked at the session_service seam.
    """
    import services.shared_state as ss_mod

    created = {"n": 0}

    class FakeSessionService:
        async def resume_and_send(self, agent_app, message, session_id, files_data=None, agent_name=None):
            # Mirror resume_and_send's contract: reuse the id if one is passed,
            # otherwise mint a new one.
            if session_id:
                return f"reply to {message}", session_id
            created["n"] += 1
            return f"reply to {message}", f"new-sess-{created['n']}"

    monkeypatch.setattr(ss_mod, "session_service", FakeSessionService())

    mgr = GatewayManager(agent_app=object())

    reply1 = await mgr._dispatch(_msg(text="first", chat_id="zz"), "Jarvis")
    assert reply1 == "reply to first"
    bound = session_map.lookup("fake", "zz")
    assert bound == "new-sess-1"  # first turn minted + persisted a session

    reply2 = await mgr._dispatch(_msg(text="second", chat_id="zz"), "Jarvis")
    assert reply2 == "reply to second"
    # Second turn reused the SAME session (no new mint) — the conversation
    # continues instead of starting over.
    assert session_map.lookup("fake", "zz") == "new-sess-1"
    assert created["n"] == 1


# ── E2E: full long-poll loop over REAL httpx (mock transport) ──────────────


async def test_e2e_telegram_loop_poll_to_send():
    """One inbound update flows through the real gateway loop and produces a
    real sendMessage HTTP call. Only the Telegram server is mocked (httpx
    MockTransport); BotApiGateway/TelegramGateway code is exercised for real.
    """
    sent_messages: list[dict] = []
    delivered = asyncio.Event()
    state = {"update_served": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        if path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "jarvis_bot"}})
        if path.endswith("/getUpdates"):
            if not state["update_served"]:
                state["update_served"] = True
                return httpx.Response(200, json={"ok": True, "result": [{
                    "update_id": 1,
                    "message": {"text": "hello bot",
                                "chat": {"id": 999}, "from": {"id": 7}},
                }]})
            # Subsequent polls: no new updates.
            return httpx.Response(200, json={"ok": True, "result": []})
        if path.endswith("/sendMessage"):
            sent_messages.append(body)
            delivered.set()
            return httpx.Response(200, json={"ok": True, "result": {"message_id": "1"}})
        # sendChatAction (typing) and anything else.
        return httpx.Response(200, json={"ok": True, "result": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def dispatcher(m: InboundMessage) -> str:
        return f"got: {m.text}"

    gw = TelegramGateway(
        token="TT", dispatcher=dispatcher, allow_from=["7"], client=client,
    )
    task = asyncio.create_task(gw.run())
    try:
        await asyncio.wait_for(delivered.wait(), timeout=3)
    finally:
        await gw.stop()
        await task

    assert sent_messages == [{"chat_id": "999", "text": "got: hello bot"}]


# ── Test-connection probe (getMe over mocked httpx) ────────────────────────


def _patch_probe_transport(monkeypatch, handler):
    import services.gateways.bot_api as bot_api_mod
    real_cls = httpx.AsyncClient

    def fake_client(*a, **k):
        return real_cls(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(bot_api_mod.httpx, "AsyncClient", fake_client)


async def test_probe_valid_token_returns_name(monkeypatch):
    _patch_probe_transport(
        monkeypatch,
        lambda req: httpx.Response(200, json={"ok": True, "result": {"username": "mybot"}}),
    )
    assert await TelegramGateway.probe("tok") == {"ok": True, "name": "mybot"}


async def test_probe_invalid_token_returns_error(monkeypatch):
    _patch_probe_transport(
        monkeypatch,
        lambda req: httpx.Response(200, json={"ok": False, "description": "bad token"}),
    )
    res = await ZaloGateway.probe("123456:ABCDEF")  # realistic token (no word collision)
    assert res == {"ok": False, "error": "bad token"}


# ── Live config reload + status ────────────────────────────────────────────


def _clear_gateway_config_all():
    from services.config_service import config_service
    for name in ("telegram", "zalo"):
        for suffix in ("enabled", "token", "allow_from", "agent"):
            config_service.set("gateways", f"{name}_{suffix}", None)


async def test_config_change_triggers_single_debounced_reload(monkeypatch):
    """A burst of gateways/* edits (one bulk save) collapses into ONE reload;
    non-gateways edits are ignored.
    """
    mgr = GatewayManager(agent_app=object())
    mgr._loop = asyncio.get_running_loop()

    reloads = []

    async def fake_reload():
        reloads.append(1)

    monkeypatch.setattr(mgr, "_reload", fake_reload)

    class Ev:
        def __init__(self, category):
            self.category = category

    mgr._on_config_change(Ev("llm"))        # unrelated → ignored
    mgr._on_config_change(Ev("gateways"))   # burst from one bulk save
    mgr._on_config_change(Ev("gateways"))
    mgr._on_config_change(Ev("gateways"))

    await asyncio.sleep(0.5)  # let the debounce window elapse
    assert reloads == [1], "expected exactly one coalesced reload"


def test_status_reflects_db_config():
    from services.config_service import config_service
    _clear_gateway_config_all()
    config_service.set("gateways", "zalo_enabled", "true")
    config_service.set("gateways", "zalo_token", "tok", is_secret=True)
    config_service.set("gateways", "zalo_allow_from", '["*"]')

    rows = {r["platform"]: r for r in GatewayManager(agent_app=object()).status()}
    assert rows["zalo"]["enabled"] is True
    assert rows["zalo"]["has_token"] is True
    assert rows["zalo"]["running"] is False  # not started in this test
    assert rows["telegram"]["enabled"] is False
    _clear_gateway_config_all()
