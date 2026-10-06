# Story pregen completion acceptance (SCRUM-67)

## Root cause evidence

A diagnostic request used the configured NamMinh voice and +20% rate on the production network. No private source text or filenames are included here.

| Sample | Characters | First audio | At 30 s | Completion |
| --- | ---: | ---: | --- | --- |
| Affected chunk | 497 | 6.41 s | 164,160 audio bytes, 228 audio events; still progressing | 32.65 s, 188,640 bytes |
| Synthetic control | 444 | 5.06 s | Already complete | 23.09 s, 132,912 bytes |

The old `asyncio.wait_for(_synth_once(...), 30)` cancels sample A before successful protocol completion. Buffered audio is correctly discarded on failure, but the deadline causes a false failure and retries the same healthy chunk. This is a demonstrated application defect; it does not prove every upstream `NoAudioReceived` has the same cause.

The new provider completed a subsequent request for sample A with the same 188,640 bytes. Its 1.22 s duration is **not** a before/after performance benchmark: upstream latency varies.

Queue regression: two failed chapters under durable cooldown previously caused the third fresh chapter to be selected. The new failing-before-fix regression proves that speculation now pauses at a two-chapter completion frontier. After cooldown expires it resumes the earliest chapter, and advances the frontier only as chapters complete. A listening-position change immediately reprioritizes the frontier; empty source files do not hold it. This deliberately bounds speculative work rather than promising that Edge is always available.

## Implementation and bounds

- 30 s without **audio** remains a stall; metadata does not reset the deadline.
- 90 s total per attempt, three attempts maximum; reader deadlines cover that budget.
- Immediate asyncio cancellation, closed upstream streams, existing atomic publication and durable integrity-checked chunk checkpoints remain in force.
- Same SSE chapter lifecycle/progress for pregen and on-demand; reconnect snapshots restore ready, failed and active chapters. Database failures return a structured 503 and release the subscription.
- UI exposes chunk progress, incomplete/retry status, reconnect state and a waiting-for-recovery explanation. Only atomic completion is green/ready.
- Closing the player no longer turns the browser's intentional source-clearing error into a failure toast.
- No new status/file polling, voice substitution, paid TTS provider, or private content fixture.

## Real Edge + computer-use acceptance

Chrome was operated through the UI on the local app. Three synthetic chapters were seeded, including two deliberate failure records. They recovered from 0 to 3 ready; a fourth 13-chunk chapter was added, observed generating, then played while the same pregen producer continued. The UI reached 4 ready via SSE without a manual refresh. Playback clock advanced (chapter one 1:10 / 1:24; streamed chapter four 3:48). Closing the player was retested without a false error toast.

The actual audio was generated using **NamMinh +20%**. Byte counts, duration from ffprobe and SHA-256 are in [live-audio.json](live-audio.json). All four chapter database rows were `ready`.

### Test network limitation

Direct local NamMinh requests returned `NoAudioReceived` on edge-tts 7.2.8; a contemporaneous en-US-Guy control returned 53,136 bytes. NamMinh on the production network succeeded. Acceptance therefore used a test-only, loopback HTTP CONNECT relay through SSH, allowlisted to `speech.platform.bing.com:443`. The application, database, UI and generation/checkpoint/queue code ran locally; only upstream egress used the server. The relay/constructor injection is outside shipped code. This does **not** establish the cause of the local/provider voice-specific failure, an IP block, or universal upstream availability.

### Screenshots (synthetic data only)

- [Desktop incomplete/retry](retry-desktop.png)
- [Mobile incomplete/retry](retry-mobile.png)
- [Live chunk progress](progress-desktop.png)
- [Mobile recovered playback](ready-mobile.png)
- [Desktop recovered playback](ready-desktop.png)
- [Fourth chapter completion during streaming playback](streamed-ready-desktop.png)

Mobile viewport/browser-engine testing is not physical iOS/Android screen-lock certification; existing real-device background playback follow-up remains separate.

## Automated verification

TDD failures captured before implementation:

1. Live audio crossing the old 30-second total deadline was incorrectly discarded.
2. Two blocked chapters incorrectly selected a third fresh chapter.
3. Closing the player incorrectly created an error toast.
4. A failed SSE snapshot leaked its subscription and returned an unstructured exception.

Tests additionally cover metadata-only stalls, total cap despite continuous audio, cancellation, retry exhaustion, atomic publication, durable checkpoints/restart, no duplicate writers, scoped snapshots, shared on-demand progress, failure/ready reconnect rendering, and empty sources. Run:

```sh
cd backend
uv run --frozen pytest tests/
```

```sh
cd frontend
npm run test:unit
npm run build
PW_FULL_MATRIX=1 npm run test:e2e -- tests/e2e/flows/pregen-progress.spec.ts tests/e2e/flows/audio-player.spec.ts tests/e2e/flows/audio-autonext.spec.ts --workers=3
```

The matrix includes Chromium desktop/mobile and WebKit mobile. One CDP suspension test skips on WebKit because that transport is Chromium-specific; the remaining WebKit playback tests run. Final exact-head CI results are recorded on the PR rather than inferred from earlier local runs.

## Operational rollout

The production container application code is not hot-patched/deployed by these tests. Recovery uses the corrected provider in an isolated worker, existing content-addressed checkpoints and the existing OS writer lease; only complete audio is atomically published and marked ready. Worker output contains chapter ordinal and numeric progress, never source prose/titles. Main application code changes ship after review/merge through normal deployment. Upstream failure remains a bounded, visible retry condition rather than a silent gap or a fake ready state.
