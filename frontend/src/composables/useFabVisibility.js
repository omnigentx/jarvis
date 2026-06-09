/**
 * useFabVisibility — shared show/hide state for the floating action buttons
 * (chat dock + voice mic) so they cover content as little as possible on mobile.
 *
 * Two independent inputs, combined as ``visible = !manualHidden && !scrollHidden``:
 *
 *   • manualHidden — the user tapped the grip to hide. STICKY + persisted.
 *     Auto-scroll must NOT override it: once manually hidden, only tapping the
 *     grip to show brings the FABs back (scrolling up does nothing). This is
 *     also the ONLY way to clear the FABs on a screen with no scroll.
 *
 *   • scrollHidden — transient: hide while scrolling DOWN (reading), reveal on
 *     scroll UP or after the scroll settles. Never persisted.
 *
 * Module-level singleton so the dock, the mic, and the grip share one source of
 * truth. The scroll listener is wired once (capture phase, so it catches scroll
 * from any inner scroll container, not just window).
 */
import { ref, computed } from 'vue'

const STORAGE_KEY = 'jarvis.fabsManualHidden'

function _loadManual() {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1'
  } catch (_) {
    return false
  }
}

const manualHidden = ref(_loadManual())
const scrollHidden = ref(false)

const visible = computed(() => !manualHidden.value && !scrollHidden.value)

function toggleManual() {
  manualHidden.value = !manualHidden.value
  try {
    localStorage.setItem(STORAGE_KEY, manualHidden.value ? '1' : '0')
  } catch (_) { /* private mode — fall back to in-memory only */ }
  // Showing manually should feel instant: clear any pending scroll-hide so the
  // FAB doesn't immediately vanish again from a stale scroll-down state.
  if (!manualHidden.value) scrollHidden.value = false
}

// ── one-time scroll wiring ────────────────────────────────────────────
let _wired = false
let _lastY = 0
let _idleTimer = null
const _SCROLL_DELTA = 6      // ignore sub-pixel / momentum jitter
const _REVEAL_IDLE_MS = 1200 // show again once scrolling settles

function _onScroll(e) {
  const t = e.target
  const cur =
    t && typeof t.scrollTop === 'number' && t.scrollTop >= 0
      ? t.scrollTop
      : (window.scrollY || 0)
  const dy = cur - _lastY
  _lastY = cur
  if (Math.abs(dy) < _SCROLL_DELTA) return

  // Down → hide; up → show. (manualHidden still wins via `visible`.)
  scrollHidden.value = dy > 0

  // Safety net: after the user stops scrolling, reveal — so a single
  // downward flick can't strand the FABs off-screen.
  if (_idleTimer) clearTimeout(_idleTimer)
  _idleTimer = setTimeout(() => { scrollHidden.value = false }, _REVEAL_IDLE_MS)
}

function _wire() {
  if (_wired || typeof window === 'undefined') return
  _wired = true
  // capture:true → scroll events from nested scroll containers reach us too.
  window.addEventListener('scroll', _onScroll, { capture: true, passive: true })
}

export function useFabVisibility() {
  _wire()
  return { visible, manualHidden, toggleManual }
}
