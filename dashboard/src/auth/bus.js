/**
 * Tiny pub/sub bus for auth-state events.
 *
 * Why not use Pinia store reactivity directly?
 *   SSE composables and other long-running side-effects need to *react*
 *   to state transitions (close socket on `expired`, re-connect on
 *   `restored`), not simply read state. A bus keeps the imperative
 *   side-effects out of the store and makes them testable in isolation.
 *
 * Why not import `mitt`?
 *   Adding a dep for ~30 lines is unjustified. This module is the bus.
 *
 * Cross-tab fan-out (BroadcastChannel) lives in the store, not here.
 * This bus only handles the in-page subscribers; the store is the
 * canonical authority.
 */

const _listeners = new Map() // event -> Set<fn>

/**
 * @param {string} event
 * @param {(payload: any) => void} fn
 * @returns {() => void} unsubscribe
 */
export function on(event, fn) {
  if (!_listeners.has(event)) _listeners.set(event, new Set())
  _listeners.get(event).add(fn)
  return () => _listeners.get(event)?.delete(fn)
}

/**
 * @param {string} event
 * @param {any} [payload]
 */
export function emit(event, payload) {
  const subs = _listeners.get(event)
  if (!subs || subs.size === 0) return
  // Snapshot the listener set so a handler unsubscribing or subscribing
  // mid-emit cannot break iteration.
  for (const fn of [...subs]) {
    try {
      fn(payload)
    } catch (err) {
      console.error(`[auth/bus] handler for "${event}" threw:`, err)
    }
  }
}

/** Test-only: drop all listeners between tests. */
export function _resetForTests() {
  _listeners.clear()
}

// Stable event name constants — typos here would silently break sync.
export const EVENTS = Object.freeze({
  EXPIRED: 'auth:expired',
  RESTORED: 'auth:restored',
  CHALLENGED: 'auth:challenged',
})
