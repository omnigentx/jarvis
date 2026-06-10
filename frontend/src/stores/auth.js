import { defineStore } from 'pinia'

import { emit, EVENTS } from '../auth/bus.js'
import { getCsrfToken } from '../api.js'

/**
 * Auth store — single source of truth for the dashboard's authentication
 * state.
 *
 * State machine
 * -------------
 *
 *     unknown         (boot — haven't probed yet)
 *        |
 *        v  probe()
 *     authenticated   (cookie verified by /api/auth/whoami)
 *        |
 *        v  401 from REST OR SSE-probe
 *     challenged      (one-shot soft state; we re-probe before locking
 *                      the UI to avoid kicking the user off on a flaky
 *                      backend restart)
 *        |
 *        v  re-probe still fails OR explicit logout
 *     unauthenticated (modal opens, all SSE retries cancelled)
 *
 * Returns to ``authenticated`` after the modal's login succeeds and
 * the cookie is established.
 *
 * Cross-tab sync
 * --------------
 *
 * BroadcastChannel('jarvis-auth') multicasts state transitions to all
 * tabs of the same origin. When tab A's modal completes login, tab B
 * sees ``authenticated`` and re-opens its SSE streams. Falls back
 * silently in browsers without BroadcastChannel (Safari < 15.4 etc).
 */

const STATUS = Object.freeze({
  UNKNOWN: 'unknown',
  AUTHENTICATED: 'authenticated',
  CHALLENGED: 'challenged',
  UNAUTHENTICATED: 'unauthenticated',
})

const BC_NAME = 'jarvis-auth'

// Silent-refresh policy. Backend's session TTL is 1h with a 12h absolute
// ceiling (see backend/core/session.py). We refresh ahead of expiry so
// the user's working session is renewed transparently; only a 401
// (max_lifetime_exceeded / key_rotated / invalid_signature) locks the UI.
const REFRESH_LEAD_SECONDS = 5 * 60      // refresh 5 min before exp
const REFRESH_MIN_DELAY_SECONDS = 30     // never schedule sooner than 30s
const REFRESH_RETRY_SECONDS = 60         // retry after transient (5xx/network) failure

let _probeInflight = null  // dedup concurrent probes
let _channel = null
let _channelInitialized = false
let _refreshTimer = null   // setTimeout handle for the next silent refresh

function _clearRefreshTimer() {
  if (_refreshTimer !== null) {
    clearTimeout(_refreshTimer)
    _refreshTimer = null
  }
}

function _scheduleRefresh(store, expiresIn) {
  _clearRefreshTimer()
  if (!expiresIn || expiresIn <= 0) return
  const delaySec = Math.max(expiresIn - REFRESH_LEAD_SECONDS, REFRESH_MIN_DELAY_SECONDS)
  _refreshTimer = setTimeout(() => {
    _refreshTimer = null
    // Return the promise so tests that fire the timer manually can
    // await its full resolution. Production setTimeout ignores return
    // values; the ``.catch`` ensures any unexpected rejection here
    // can't surface as an unhandled promise rejection.
    return store.refresh().catch((err) => {
      console.warn('[auth/store] silent refresh threw:', err)
    })
  }, delaySec * 1000)
}

function _initChannel(store) {
  if (_channelInitialized) return
  _channelInitialized = true
  if (typeof BroadcastChannel === 'undefined') return
  try {
    _channel = new BroadcastChannel(BC_NAME)
    _channel.onmessage = (event) => {
      const msg = event.data || {}
      // Trust messages from same-origin tabs only (BroadcastChannel
      // already enforces same-origin, so we only need to ignore
      // accidental noise).
      if (msg.type === 'transition' && typeof msg.status === 'string') {
        // Apply silently — do NOT re-broadcast or we get a loop.
        store._applyRemoteTransition(msg.status, msg.csrfToken, msg.expiresIn)
      }
    }
  } catch (err) {
    console.warn('[auth/store] BroadcastChannel init failed', err)
  }
}

export const useAuthStore = defineStore('auth', {
  state: () => ({
    status: STATUS.UNKNOWN,
    csrfToken: '',
    /** Last probe failure ``reason`` from the backend (e.g. ``key_rotated``). */
    lastReason: '',
    /** UNIX seconds until session exp; null when unknown. */
    expiresAt: null,
  }),

  getters: {
    isAuthenticated: (state) => state.status === STATUS.AUTHENTICATED,
    isLockedOut: (state) => state.status === STATUS.UNAUTHENTICATED,
    /** UI should show the modal when status is unauthenticated. */
    showAuthGate: (state) => state.status === STATUS.UNAUTHENTICATED,
  },

  actions: {
    /**
     * Pre-flight probe via /api/auth/whoami. Idempotent and dedup-safe:
     * concurrent calls share one in-flight request, so a fan-out of
     * SSE streams hitting 401 simultaneously cannot trigger N probes.
     *
     * Never throws. Returns the resolved status.
     */
    async probe() {
      if (_probeInflight) return _probeInflight
      _probeInflight = (async () => {
        try {
          const resp = await fetch('/api/auth/whoami', {
            credentials: 'include',
          })
          if (!resp.ok) {
            // /whoami specifically should never 401 — it's a probe — but
            // if the backend hands us anything weird, treat as unknown
            // network failure (NOT unauthenticated, to avoid false
            // lockout on a transient backend restart).
            return this.status
          }
          const body = await resp.json()
          if (body.authenticated) {
            this.setAuthenticated(getCsrfToken(), body.expires_in)
          } else {
            // Whoami returned authenticated:false — cookie missing or
            // invalid (rotated key, expired, tampered). Lock UI.
            this._setUnauthenticated('whoami_unauthenticated')
          }
          return this.status
        } catch (err) {
          // Network failure — DON'T flip to unauthenticated. The user
          // is offline or the backend is down; leave them in current
          // state. The SSE retry loop will keep trying.
          console.warn('[auth/store] probe network error:', err)
          return this.status
        } finally {
          _probeInflight = null
        }
      })()
      return _probeInflight
    },

    /**
     * POST /api/auth/login with the supplied API key. On success the
     * backend sets the cookies; we update local state and broadcast.
     *
     * @param {string} apiKey
     * @returns {Promise<{ok: true} | {ok: false, status: number, reason?: string}>}
     */
    async login(apiKey) {
      try {
        const resp = await fetch('/api/auth/login', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ api_key: apiKey }),
        })
        if (!resp.ok) {
          let reason = 'unknown'
          try {
            const body = await resp.json()
            reason = body?.detail?.reason || body?.detail || reason
          } catch (_) { /* ignore */ }
          return { ok: false, status: resp.status, reason }
        }
        const body = await resp.json()
        this.setAuthenticated(body.csrf_token, body.expires_in)
        return { ok: true }
      } catch (err) {
        return { ok: false, status: 0, reason: 'network_error' }
      }
    },

    /**
     * Silently extend the session via /api/auth/refresh.
     *
     * Outcomes:
     *   - 200 ok           → ``setAuthenticated`` reschedules the next refresh
     *   - 401              → user must re-login (abs_exp hit, key rotated,
     *                        signature invalid). Locks the UI.
     *   - 5xx / network    → keep current state; reschedule a short retry.
     *                        We don't lock on transients because the cookie
     *                        is still valid; the user shouldn't be kicked
     *                        out for a flaky backend.
     */
    async refresh() {
      if (this.status !== STATUS.AUTHENTICATED) return
      // CSRF: /api/auth/refresh is NOT exempt from CsrfMiddleware
      // (only /login, /logout, /setup, /oauth callbacks are). We must
      // echo the jarvis_csrf cookie as X-CSRF-Token or the middleware
      // 403s us. Same envelope apiFetch uses for every other mutation.
      const csrf = getCsrfToken()
      const headers = csrf ? { 'X-CSRF-Token': csrf } : {}
      try {
        const resp = await fetch('/api/auth/refresh', {
          method: 'POST',
          credentials: 'include',
          headers,
        })
        if (resp.status === 401) {
          let reason = 'session_expired'
          try {
            const body = await resp.json()
            reason = body?.detail?.reason || body?.detail || reason
          } catch (_) { /* ignore */ }
          this._setUnauthenticated(reason)
          return
        }
        if (!resp.ok) {
          console.warn('[auth/store] refresh got status', resp.status)
          _scheduleRefresh(this, REFRESH_RETRY_SECONDS + REFRESH_LEAD_SECONDS)
          return
        }
        const body = await resp.json()
        if (!body.expires_in) {
          // Defensive: an unexpected response shape would otherwise
          // schedule no next refresh and silently expire the session
          // one cycle later. Fall back to whoami to recover an
          // expires_in.
          console.warn('[auth/store] /refresh response missing expires_in; falling back to whoami')
          await this.probe()
          return
        }
        // setAuthenticated reschedules the next silent refresh based on
        // the new expires_in window.
        this.setAuthenticated(body.csrf_token, body.expires_in)
      } catch (err) {
        console.warn('[auth/store] refresh network error:', err)
        _scheduleRefresh(this, REFRESH_RETRY_SECONDS + REFRESH_LEAD_SECONDS)
      }
    },

    /** Log out via the backend; clears cookies + local state. */
    async logout() {
      try {
        await fetch('/api/auth/logout', {
          method: 'POST',
          credentials: 'include',
        })
      } catch (_) { /* logout is best-effort */ }
      this._setUnauthenticated('logout')
    },

    /**
     * Called by the apiFetch wrapper on 401. Soft-fails: bumps to
     * "challenged", re-probes once, and only locks the UI if the
     * probe also fails. This avoids kicking the user out on a single
     * transient 401 (backend restart mid-flight, JWT_SECRET reload).
     */
    async on401(reason) {
      if (this.status === STATUS.UNAUTHENTICATED) return
      this.status = STATUS.CHALLENGED
      this.lastReason = reason || ''
      emit(EVENTS.CHALLENGED, { reason })
      // Re-probe ONCE. If still bad, lock — but only if probe didn't
      // already lock us (it would have emitted EXPIRED itself, so a
      // second _setUnauthenticated() here would double-fire).
      await this.probe()
      if (
        this.status !== STATUS.AUTHENTICATED &&
        this.status !== STATUS.UNAUTHENTICATED
      ) {
        this._setUnauthenticated(reason || 'rest_401')
      }
    },

    /**
     * Internal helper used by login() and probe() to converge on the
     * "authenticated" state and notify subscribers. Idempotent.
     */
    setAuthenticated(csrfToken, expiresIn) {
      // ``from`` rides on RESTORED so subscribers can distinguish a modal
      // login (from === 'unauthenticated' — page state is 401-poisoned and
      // needs a full replay) from the boot probe (from === 'unknown') or a
      // challenged-recovery, where in-page state is still good.
      const prevStatus = this.status
      const wasUnauth = prevStatus !== STATUS.AUTHENTICATED
      this.status = STATUS.AUTHENTICATED
      this.csrfToken = csrfToken || getCsrfToken()
      this.lastReason = ''
      this.expiresAt = expiresIn ? Math.floor(Date.now() / 1000) + expiresIn : null
      _scheduleRefresh(this, expiresIn)
      if (wasUnauth) {
        // Broadcast BEFORE the local emit: App.vue's RESTORED listener may
        // call window.location.reload(), and sibling-tab notification must
        // not depend on reload() letting the current task finish.
        this._broadcast(expiresIn)
        emit(EVENTS.RESTORED, { from: prevStatus })
      }
    },

    _setUnauthenticated(reason) {
      const wasAuth = this.status === STATUS.AUTHENTICATED
      this.status = STATUS.UNAUTHENTICATED
      this.csrfToken = ''
      this.lastReason = reason || ''
      this.expiresAt = null
      _clearRefreshTimer()
      // Always emit so SSE composables can stop their retry loops, even
      // if we were already in challenged/unknown — the contract is
      // "stop retrying when expired fires".
      emit(EVENTS.EXPIRED, { reason })
      if (wasAuth) this._broadcast()
    },

    /**
     * Apply a transition received from another tab. Don't re-broadcast
     * (would loop). Do emit local bus events so SSE composables react.
     */
    _applyRemoteTransition(status, csrfToken, expiresIn) {
      if (status === STATUS.AUTHENTICATED) {
        const prevStatus = this.status
        const wasUnauth = prevStatus !== STATUS.AUTHENTICATED
        this.status = STATUS.AUTHENTICATED
        this.csrfToken = csrfToken || getCsrfToken()
        this.lastReason = ''
        if (expiresIn) {
          this.expiresAt = Math.floor(Date.now() / 1000) + expiresIn
          _scheduleRefresh(this, expiresIn)
        }
        // ``from`` is THIS tab's previous status — a locked sibling tab
        // (modal showing) recovers exactly like the tab that logged in.
        if (wasUnauth) emit(EVENTS.RESTORED, { from: prevStatus })
      } else if (status === STATUS.UNAUTHENTICATED) {
        const wasAuth = this.status === STATUS.AUTHENTICATED
        this.status = STATUS.UNAUTHENTICATED
        this.csrfToken = ''
        this.expiresAt = null
        _clearRefreshTimer()
        if (wasAuth) emit(EVENTS.EXPIRED, { reason: 'cross_tab' })
      }
    },

    _broadcast(expiresIn) {
      _initChannel(this)
      if (!_channel) return
      try {
        // ``csrfToken`` is technically redundant here — sibling tabs
        // share the same origin and can read the ``jarvis_csrf``
        // cookie themselves. We include it so a sibling that
        // happens to be in the middle of cookie-write race (e.g.
        // about to send a mutation) gets the value immediately
        // instead of having to re-read document.cookie. Cheap
        // belt-and-suspenders; safe because cookies are not secret
        // anyway (the httpOnly session is the secret).
        //
        // ``expiresIn`` lets sibling tabs schedule their own silent
        // refresh from the same baseline; without it they'd have to
        // wait for /whoami or rely on stale local state.
        _channel.postMessage({
          type: 'transition',
          status: this.status,
          csrfToken: this.csrfToken,
          expiresIn: expiresIn || null,
        })
      } catch (err) {
        console.warn('[auth/store] broadcast failed', err)
      }
    },

    /**
     * Boot wiring. Call once from App.vue ``onMounted``.
     * - Initializes BroadcastChannel
     * - Runs initial probe
     */
    async init() {
      _initChannel(this)
      await this.probe()
    },
  },
})

/** Test-only: drop module-level singletons so each test starts clean.
 *  Production code never calls this. */
export function _resetAuthModuleForTests() {
  if (_channel) { try { _channel.close() } catch (_) { /* ignore */ } }
  _channel = null
  _channelInitialized = false
  _probeInflight = null
  _clearRefreshTimer()
}

export { STATUS }
