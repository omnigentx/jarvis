/**
 * webrtcRecovery — decides WHEN to re-negotiate a dead RTCPeerConnection.
 *
 * Why this exists: on mobile networks (5G cell handover, CGNAT rebind, brief
 * radio loss) the ICE candidate pair dies mid-session as a matter of course.
 * The original handler treated the first ``failed`` as fatal — error banner,
 * dead mic — even though the /ws/voice control socket (which rides HTTPS/
 * cloudflared) usually survives the blip and can carry a fresh ``webrtc_offer``
 * within a second or two.
 *
 * Policy encoded here:
 *   * ``disconnected`` is usually transient — give the browser ``graceMs`` to
 *     recover on its own before forcing a re-offer (the timer is cancelled if
 *     the connection comes back).
 *   * ``failed`` re-offers immediately.
 *   * ``connected`` resets the retry budget — only *consecutive* failures
 *     count toward giving up.
 *   * After ``maxRetries`` consecutive attempts, give up via ``fail`` (the
 *     caller surfaces the existing error banner).
 *
 * Pure timing/decision logic — the actual teardown + re-offer lives in
 * useVoiceSession.js (``restart`` callback), so this is unit-testable without
 * shimming RTCPeerConnection. Mirrors the playbackDoneTracker.js pattern.
 *
 * @param {object} opts
 * @param {(attempt: number) => void} opts.restart  perform one re-negotiation
 * @param {(reason: string) => void}  opts.fail     give up — surface the error
 * @param {number} [opts.maxRetries]  consecutive attempts before giving up
 * @param {number} [opts.graceMs]     how long 'disconnected' may self-heal
 */
export function createWebRtcRecovery({ restart, fail, maxRetries = 2, graceMs = 4000 }) {
  let retries = 0
  let timer = null
  let stopped = false

  function clearTimer() {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
  }

  function attempt() {
    if (stopped) return
    if (retries >= maxRetries) {
      fail(`webrtc reconnect gave up after ${retries} attempts`)
      return
    }
    retries++
    restart(retries)
  }

  return {
    /** Feed every RTCPeerConnection ``connectionstatechange`` here. */
    onState(state) {
      if (stopped) return
      if (state === 'connected') {
        retries = 0
        clearTimer()
      } else if (state === 'disconnected') {
        if (timer === null) {
          timer = setTimeout(() => {
            timer = null
            attempt()
          }, graceMs)
        }
      } else if (state === 'failed') {
        clearTimer()
        attempt()
      }
    },
    /** A restart attempt itself blew up (offer send failed, ICE fetch error). */
    onRestartError() {
      if (stopped) return
      // Count as a failed pair: either retry (budget left) or give up.
      attempt()
    },
    /** Session torn down (user stop / WS closed) — never fire again. */
    stop() {
      stopped = true
      clearTimer()
    },
  }
}
