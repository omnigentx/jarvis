# Messaging gateways

Connect external chat platforms (Telegram, Zalo, …) to the Jarvis agent.
Inbound message → agent run → reply back in the same chat. Long-polling, so
**no public domain / webhook is required** — the bot holds an outbound
`getUpdates` request open and the platform returns the instant a message
arrives.

## Layers

```
BaseGateway (base.py)          orchestration: allow-list → typing → dispatch → reply → error
  └─ BotApiGateway (bot_api.py)  shared Telegram-style HTTP long-poll transport
       ├─ TelegramGateway        array updates + update_id offset, sendChatAction typing
       └─ ZaloGateway            single update + 408 timeout, no typing

GatewayManager (manager.py)    lifecycle + the ONE bridge to the agent runtime
session_map.py                 (platform, chat_id) → backend session_id  [SSoT, DB-backed]
config.py                      loads `gateways:` from fastagent.secrets.yaml
registry.py                    name → gateway class
```

## How a message flows

1. `BotApiGateway.run()` long-polls `getUpdates` and normalizes each update to
   an `InboundMessage`.
2. `BaseGateway.handle_inbound()` gates on `allow_from`, shows "typing…", then
   calls the manager's dispatcher.
3. `GatewayManager._dispatch()` looks up the chat's bound session, calls
   `session_service.resume_and_send(...)`, and **upserts the session id it gets
   back** — so the binding self-heals if the backend session was deleted.
4. The reply is chunked (≤3900 chars) and sent via `sendMessage`.

Agent runs are globally serialized by `session_service`'s lock, so concurrent
gateways + the web UI never interleave turns.

## Adding a platform

Telegram-style Bot API (most chat apps): subclass `BotApiGateway`, set
`api_base` / `typing_method`, implement `_poll()`. ~20 lines — see `zalo.py`.

Anything else (WebSocket, webhook, SDK): subclass `BaseGateway` directly and
implement `run()` + `send_text()`.

Then add one line to `GATEWAY_REGISTRY` in `registry.py` and a config block in
`fastagent.secrets.yaml`. Nothing else changes.

## Config

Stored in `config_service` (the DB, category `gateways`), edited from the UI:
**Settings → Messaging Gateways**. Keys per platform: `<p>_enabled`,
`<p>_token` (encrypted secret), `<p>_allow_from` (JSON array), `<p>_agent`.

Writes go through `/api/settings/bulk`, which emits a change event;
`GatewayManager` subscribes and **live-reloads** (stop → reload → start,
debounced) — no restart. `routes/gateways.py` adds `GET /api/gateways`
(status) and `POST /api/gateways/{platform}/test` (validate a token via
`getMe`). Disabled by default; empty `allow_from` ignores everyone until you
add user ids (`["*"]` to allow all).

## Inbound images

Photos are downloaded and passed to the agent as multimodal input. Telegram:
`getFile` → file URL → base64. Zalo: the photo arrives as a CDN URL in
`message.photo_url` (not `photo`), fetched directly; its `image/jpg` content
type is normalized to `image/jpeg`. The caption (if any) becomes the message
text. Stickers / other media are skipped (cursor still advances).

**Vision model required.** The image only reaches the LLM if the answering
model is vision-capable. fast-agent strips image content for models it doesn't
recognize (unknown models default to text-only → "Missing capability: vision").
For a custom/proxy model (e.g. a 9router combo) declare its capabilities with a
local overlay — gitignored, per-user — at
`backend/.fast-agent/model-overlays/<name>.yaml`:

```yaml
name: openai.coding-agent   # must equal the model string you use (default_model)
provider: openai
model: coding-agent          # wire name sent to the provider
metadata:
  tokenizes: [text/plain, image/jpeg, image/png, image/webp]
```

## Slash commands (typed in the chat)

Handled in `commands.py` before the agent runs (only for allow-listed users;
unknown `/...` falls through to the agent):

- `/new` (`/reset`, `/clear`) — start a fresh conversation.
- `/agent <name>` — switch the answering agent for *this* chat (stored in the
  binding); no arg shows the current agent + the list.
- `/whoami` (`/id`) — show your user id.
- `/help` — list commands.

`/stop` is intentionally absent: the poll loop is sequential and blocks on the
agent reply, so it cannot receive a message to interrupt mid-run.

## Secure onboarding (no `*` needed)

An **unauthorized** sender never reaches the agent — the bot replies with ONLY
their user id and a "not authorized" notice. So the owner keeps `allow_from`
empty (deny-all, the safe default), messages the bot, reads their id from the
reply, and adds it. You never have to open `["*"]` just to discover an id.
(Same idea as openclaw/hermes pairing.)

## Scope / roadmap

- One synchronous reply per message (no streaming edits). Streaming would hang
  off the activity stream — deferred to keep this simple and rate-limit-safe.
- Webhook transport is not implemented (long-polling covers local/NAT setups).
- Outbound media (bot → user images) not implemented; inbound only.
