# SCRUM-62 — story playback cache race

## Root cause and reproduction

Production logs (2026-10-05 23:57:38 UTC and 2026-10-06 00:01:12 UTC) contain:

```text
routes.tts - WARNING - Truncated file detected: 18000B < min 36729B. Removing.
```

The old `backend/routes/tts.py::tts_endpoint` validated/deleted the growing
`.mp3` despite an active generation lock. It keyed workers by request ID even
though the cache is keyed by content. A retry/Range request could unlink the
writer's inode; another request ID could start a second writer and truncate it.
The pre-gen writer used `.tmp` + atomic publish, but the HTTP route did not
actually attach to that writer. The browser could then wait on deleted or
inconsistent output. Frontend recovery also polled HEAD every 500ms.

Before editing, two deterministic tests against the real handler/files failed:

- second GET deleted the active 18KB audio;
- different request IDs for identical content started two providers.

These regressions now pass, including exact equality of both complete streams.
Private story names, chapter text, source directory and raw production logs are
excluded from this repository and PR.

## Changes

- One producer per content cache, synchronously claimed before awaiting;
  OS advisory lock prevents a different worker from overwriting it.
- On-demand and pre-gen both write `.tmp`, flush and notify readers, then
  atomically publish complete `.mp3`. Readers retain a stable inode.
- Event-driven byte delivery; no file/status polling loop. Shared generation
  survives a reader disconnect. Failed/cancelled work removes its temp/lock;
  a later claim recovers a crashed producer's stale temp file.
- Stall cap of 100 seconds without progress; explicit 502/503/504 errors.
- Completed files use HTTP Range semantics, including suffix ranges and 416.
  Growing responses do not advertise a static length or byte ranges.
- Underrun recovery uses a status SSE snapshot plus pushed updates and bounded
  exponential reconnect. Changing playback aborts the previous subscription.
- Audio failure stops the spinner and allows a deliberate retry. Status-probe
  failure cannot silently advance a chapter. Reset old time/duration when
  changing chapter, preserve saved seek on resume, reload playlist on refresh.
- TTS GET/HEAD now enforce existing cookie/Bearer auth; the previous optional
  authentication result was ignored. Status SSE is authenticated too.

## Actual local acceptance

Full local backend, pinned submodules, `uv sync --frozen`, then
`uv run --frozen uvicorn server:app --host 127.0.0.1 --port 8051`.
UI uses Vite on 3041. All acceptance content is synthetic.

- Real Edge TTS long chapter: live UI clock advanced 0:21 → 0:50 → 1:23,
  without spinner or console errors. `desktop-live.jpg` captures live playback.
- Two simultaneous real HTTP downloads during that generation returned
  identical **3,948,624 bytes**. SHA-256:
  `38667322e9308a38b44619fa44fba07b48cba583c298a9b88d4428de41b0e137`.
- On final backend: refresh/resume mobile at 390×844 retained position, both
  chapter navigation controls enabled, full duration **10:58**. Seek +30s
  advanced 1:29 → 2:05 while playing. `mobile-seek.jpg` captures this.
- Document width equals viewport width: 390px; no horizontal overflow.

## Automated verification

- Focused backend: **37 passed**, covering concurrent aliases, repeat GET,
  pre-gen handover, disconnect, first/mid-stream failure, timeout, foreign
  worker lock, HEAD, SSE completion/error, Range/suffix/416 and authentication.
- Frontend unit: **257 passed**.
- Focused browser E2E: **8 passed** (Stories/import, failure clears spinner,
  retry makes a fresh audio request, restored playlist supports next chapter).
- Production frontend build passed. Full CI results are linked on the PR.

Automated browser E2E uses scripted HTTP responses; actual speech decoding
and time advancement were separately verified above using computer use and
real Edge output. A 390px Chromium viewport is not physical iOS Safari proof.

## Remaining observation

The existing `sendBeacon('/api/library/progress', …)` sends no CSRF header;
local server logs show 403 on page unload. Regular progress saves use apiFetch.
This separate close-page persistence issue is tracked as SCRUM-63; it is not
part of the TTS writer race.
