/**
 * playbackDoneTracker — decides WHEN the client should tell the server it has
 * finished playing a TTS turn (the ``playback_done`` control frame).
 *
 * Why this exists (barge-in SSoT): the server keeps ``bot_speaking`` True from
 * ``tts_start`` until the client reports ``playback_done``. That window —
 * "the user is actually hearing the bot" — is what a barge-in checks against.
 * The naive signal (server synthesis finished) is wrong: the client buffers
 * several seconds of audio ahead, so synthesis ends long before the user stops
 * hearing the bot. Reporting playback_done only when BOTH (a) the server sent
 * ``tts_end`` (no more chunks coming) and (b) the local playback queue has
 * drained gives the server an accurate "bot is now silent" edge.
 *
 * Pure + side-effect-free except the injected ``send`` callback, so the timing
 * logic is unit-testable without shimming AudioContext / WebSocket. See
 * playbackDoneTracker.test.js and the wiring in useVoiceSession.js.
 *
 * @param {() => void} send  invoked exactly once per turn when playback_done
 *                           should be sent (the caller does the actual WS send).
 */
export function createPlaybackDoneTracker(send) {
  let productionEnded = false  // server emitted tts_end (no more chunks)
  let sent = false             // playback_done already sent this turn

  // Fire once, only when production ended AND nothing is left playing.
  function maybe(queueSize) {
    if (sent || !productionEnded || queueSize > 0) return
    sent = true
    send()
  }

  return {
    /** New TTS turn began (tts_start) — re-arm. */
    ttsStart() {
      productionEnded = false
      sent = false
    },
    /** Server finished synthesising (tts_end). Fires now if already drained. */
    ttsEnd(queueSize) {
      productionEnded = true
      maybe(queueSize)
    },
    /** A buffered chunk finished playing (src.onended). ``queueSize`` is the
     *  number of chunks STILL scheduled after this one was removed. */
    chunkEnded(queueSize) {
      maybe(queueSize)
    },
    /** Barge-in flushed the queue — the server already lowered bot_speaking on
     *  the cancel, so suppress the drain-triggered playback_done. */
    flush() {
      sent = true
    },
  }
}
