/**
 * Generic auth-aware SSE composable.
 *
 * Replaces the 9 hand-rolled EventSource-with-retry blocks scattered
 * across the dashboard. Solves three classes of bug at once:
 *
 *   1. **401-loop after key rotation.** EventSource has no status code
 *      on errors, so previous code blindly retried with exponential
 *      backoff forever. We integrate with the auth store: a connection
 *      that errors *before* its first onopen runs auth.probe(); if the
 *      probe says unauthenticated, we emit EXPIRED (which closes ALL
 *      streams) and stop retrying instead of spamming logs.
 *   2. **Reconnect storms when locked out.** When the auth store
 *      transitions to unauthenticated, every active SSE listens for
 *      the EXPIRED event and tears itself down. When auth restores
 *      (RESTORED event), they reconnect automatically.
 *   3. **Tab-visibility blind reconnect.** Reconnect on visibility
 *      change is gated on auth.isAuthenticated.
 *
 * The composable owns lifecycle: connect / disconnect on mount / unmount,
 * auto-cleanup of bus subscriptions, retry timers, and visibility
 * listeners. Callers just supply a URL and event handlers.
 */
import { onMounted, onUnmounted, ref } from 'vue'

import { EVENTS, on } from '../auth/bus.js'
import { useAuthStore } from '../stores/auth.js'

const MAX_RETRY_DELAY = 30000

/**
 * @param {string | (() => string | null)} urlOrFactory
 *   Either a static URL or a factory called at connect-time. The
 *   factory may return ``null`` to skip connecting (e.g. when a
 *   required parameter like meetingId hasn't been set yet).
 * @param {Object} options
 * @param {(ev: MessageEvent) => void} [options.onMessage]
 *   Called for every ``message``-type event.
 * @param {(es: EventSource) => void} [options.onConnected]
 *   Called once after ``onopen`` fires successfully.
 * @param {Record<string, (ev: MessageEvent) => void>} [options.namedEvents]
 *   Mapping of named server-sent event types to handlers. Each is
 *   wired via ``addEventListener`` so the server can multiplex.
 * @param {boolean} [options.reconnectOnVisible=true]
 *   Whether to reconnect when the tab becomes visible again.
 * @param {boolean} [options.autoConnect=true]
 *   When false, the caller drives connect()/disconnect() manually.
 */
export function useSSEConnection(urlOrFactory, options = {}) {
  const {
    onMessage,
    onConnected,
    namedEvents = {},
    reconnectOnVisible = true,
    autoConnect = true,
  } = options

  const auth = useAuthStore()
  const status = ref('idle') // 'idle' | 'connecting' | 'connected' | 'disconnected'
  const retryCount = ref(0)
  let eventSource = null
  let retryTimer = null
  let destroyed = false
  let openedOnce = false  // tracks whether onopen ever fired for this attempt

  const _resolveUrl = () =>
    typeof urlOrFactory === 'function' ? urlOrFactory() : urlOrFactory

  function connect() {
    if (destroyed) return
    // Don't try to open a stream when we're not authenticated — the
    // backend would 401, EventSource would silently retry, and the
    // network panel would fill with 401s while the user is staring
    // at the AuthGate modal.
    if (!auth.isAuthenticated) {
      status.value = 'disconnected'
      return
    }

    const url = _resolveUrl()
    if (!url) {
      // Caller hasn't supplied required URL params yet (e.g. meetingId
      // not picked). Stay idle.
      status.value = 'idle'
      return
    }

    // Tear down any prior connection before opening a new one.
    if (eventSource) {
      try { eventSource.close() } catch (_) { /* ignore */ }
      eventSource = null
    }

    status.value = 'connecting'
    openedOnce = false
    eventSource = new EventSource(url)

    eventSource.onopen = () => {
      status.value = 'connected'
      retryCount.value = 0
      openedOnce = true
      try { onConnected?.(eventSource) } catch (err) {
        console.error('[SSE] onConnected handler threw', err)
      }
    }

    if (onMessage) {
      eventSource.onmessage = (ev) => {
        try { onMessage(ev) } catch (err) {
          console.warn('[SSE] onMessage handler threw', err)
        }
      }
    }

    for (const [name, handler] of Object.entries(namedEvents)) {
      eventSource.addEventListener(name, (ev) => {
        try { handler(ev) } catch (err) {
          console.warn(`[SSE] named-event "${name}" handler threw`, err)
        }
      })
    }

    eventSource.onerror = async () => {
      // Snapshot the "did we ever open" flag before close() resets it.
      const wasOpened = openedOnce
      try { eventSource?.close() } catch (_) { /* ignore */ }
      eventSource = null
      if (destroyed) return
      status.value = 'disconnected'

      if (!wasOpened) {
        // Errored before any successful open — could be auth fail,
        // could be cold backend. Probe the auth store: if it concludes
        // we're unauthenticated, the EXPIRED event will fire and our
        // listener tears retries down. We avoid an explicit double-
        // probe by checking the store's status afterwards.
        await auth.probe()
        if (!auth.isAuthenticated) {
          // Lock state — don't re-schedule. The EXPIRED bus event has
          // already fired (probe → _setUnauthenticated) and our own
          // listener will have called disconnect(); be idempotent.
          return
        }
      }

      _scheduleReconnect()
    }
  }

  function _scheduleReconnect() {
    if (destroyed || !auth.isAuthenticated) return
    const delay = Math.min(1000 * Math.pow(2, retryCount.value), MAX_RETRY_DELAY)
    retryCount.value++
    retryTimer = setTimeout(connect, delay)
  }

  function disconnect() {
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null }
    if (eventSource) {
      try { eventSource.close() } catch (_) { /* ignore */ }
      eventSource = null
    }
    status.value = 'disconnected'
  }

  /** Public reconnect — kills any in-flight retry timer + cycles connection. */
  function reconnect() {
    retryCount.value = 0
    disconnect()
    connect()
  }

  // ---- Auth bus integration -----------------------------------------------

  const offExpired = on(EVENTS.EXPIRED, () => {
    // Tear down NOW — don't wait for the next retry tick.
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null }
    if (eventSource) {
      try { eventSource.close() } catch (_) { /* ignore */ }
      eventSource = null
    }
    status.value = 'disconnected'
  })
  const offRestored = on(EVENTS.RESTORED, () => {
    if (destroyed) return
    retryCount.value = 0
    connect()
  })

  // ---- Visibility ---------------------------------------------------------

  function onVisibilityChange() {
    if (document.visibilityState !== 'visible') return
    if (!auth.isAuthenticated) return
    if (status.value !== 'connected') reconnect()
  }
  if (reconnectOnVisible && typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisibilityChange)
  }

  // ---- Lifecycle ----------------------------------------------------------

  onMounted(() => {
    if (autoConnect) connect()
  })
  onUnmounted(() => {
    destroyed = true
    disconnect()
    offExpired()
    offRestored()
    if (reconnectOnVisible && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  })

  return { status, retryCount, connect, disconnect, reconnect }
}
