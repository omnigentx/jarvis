/**
 * Auth store unit tests.
 *
 * We exercise the store via Pinia's vanilla-JS surface (no Vue runtime
 * required for state mutations / actions). globalThis.fetch,
 * document.cookie, and BroadcastChannel are stubbed so the test runs
 * in pure node.
 */
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'

import { createPinia, setActivePinia } from 'pinia'

import { _resetForTests as resetBus, on, EVENTS } from '../auth/bus.js'

// ---- Globals stubbed before importing the store ----

class FakeBroadcastChannel {
  constructor(name) {
    this.name = name
    this.onmessage = null
    FakeBroadcastChannel._channels.push(this)
  }
  postMessage(msg) {
    for (const ch of FakeBroadcastChannel._channels) {
      if (ch === this) continue
      ch.onmessage?.({ data: msg })
    }
  }
  close() { /* noop */ }
}
FakeBroadcastChannel._channels = []

globalThis.BroadcastChannel = FakeBroadcastChannel
globalThis.document = globalThis.document || { cookie: '' }
globalThis.window = globalThis.window || {}

// fetch stub: tests overwrite via _setFetch().
let _fetchImpl = async () => { throw new Error('fetch not set') }
globalThis.fetch = (...args) => _fetchImpl(...args)
function _setFetch(impl) { _fetchImpl = impl }

// Timer stubs. Two motivations:
//   1. The auth store schedules a silent-refresh timer ~55 min ahead on
//      every successful auth — without stubbing, the real Node timer
//      keeps the test process alive after the last test finishes.
//   2. We want to assert the schedule (delay, presence) and fire the
//      callback synchronously instead of waiting wall-clock time.
//
// Short delays (<1s) are used by other tests in this file to interleave
// promises (``setTimeout(r, 10)``); we pass those through to the real
// timer so those tests still work. The auth store's minimum scheduled
// delay is 30s, so the 1s cutoff is safely above test helper usage and
// safely below anything the auth store would ever schedule.
const _origSetTimeout = globalThis.setTimeout
const _origClearTimeout = globalThis.clearTimeout
let _scheduledTimers = []  // {id, fn, ms}
let _nextTimerId = 1_000_000  // distinct namespace from real timer ids
globalThis.setTimeout = (fn, ms) => {
  if (ms < 1000) return _origSetTimeout(fn, ms)
  const id = _nextTimerId++
  _scheduledTimers.push({ id, fn, ms })
  return id
}
globalThis.clearTimeout = (id) => {
  const idx = _scheduledTimers.findIndex((t) => t.id === id)
  if (idx >= 0) _scheduledTimers.splice(idx, 1)
  else _origClearTimeout(id)
}
function _pendingTimer() {
  // Latest scheduled timer (auth store only ever has one in-flight).
  return _scheduledTimers[_scheduledTimers.length - 1] || null
}
async function _fireTimer() {
  const t = _pendingTimer()
  if (!t) throw new Error('no timer pending')
  _scheduledTimers = _scheduledTimers.filter((x) => x.id !== t.id)
  await t.fn()
}

// Now import the store (after globals are in place).
const { useAuthStore, STATUS, _resetAuthModuleForTests } = await import('./auth.js')

beforeEach(() => {
  setActivePinia(createPinia())
  resetBus()
  _resetAuthModuleForTests()
  FakeBroadcastChannel._channels.length = 0
  globalThis.document.cookie = ''
  _scheduledTimers = []
  // Use a high baseline so our captured-timer IDs cannot collide with
  // real Node timer ids returned for the <1s passthrough timers used
  // by other tests in this file.
  _nextTimerId = 1_000_000
})


// ---- Initial state ----------------------------------------------------------

test('initial state is unknown / not authenticated', () => {
  const auth = useAuthStore()
  assert.equal(auth.status, STATUS.UNKNOWN)
  assert.equal(auth.isAuthenticated, false)
  assert.equal(auth.showAuthGate, false, 'unknown state must NOT open the gate prematurely')
})


// ---- probe() ----------------------------------------------------------------

test('probe(): authenticated whoami → status=authenticated + RESTORED event', async () => {
  _setFetch(async (url) => {
    assert.match(url, /\/api\/auth\/whoami/)
    return new Response(JSON.stringify({ authenticated: true, expires_in: 3600 }), {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  })
  globalThis.document.cookie = 'jarvis_csrf=csrf-from-cookie; other=x'

  let restoredFired = 0
  on(EVENTS.RESTORED, () => { restoredFired++ })

  const auth = useAuthStore()
  await auth.probe()

  assert.equal(auth.status, STATUS.AUTHENTICATED)
  assert.equal(auth.csrfToken, 'csrf-from-cookie')
  assert.equal(auth.isAuthenticated, true)
  assert.equal(restoredFired, 1)
})

test('probe(): unauthenticated whoami → status=unauthenticated + EXPIRED event', async () => {
  _setFetch(async () => new Response(JSON.stringify({ authenticated: false }), {
    status: 200, headers: { 'content-type': 'application/json' },
  }))

  let expiredFired = 0
  on(EVENTS.EXPIRED, () => { expiredFired++ })

  const auth = useAuthStore()
  await auth.probe()

  assert.equal(auth.status, STATUS.UNAUTHENTICATED)
  assert.equal(auth.showAuthGate, true)
  assert.equal(expiredFired, 1)
})

test('probe(): network error keeps current status (no false lockout)', async () => {
  _setFetch(async () => { throw new TypeError('Failed to fetch') })

  const auth = useAuthStore()
  // Pretend we were authenticated already.
  auth.$patch({ status: STATUS.AUTHENTICATED, csrfToken: 'tok' })

  let expiredFired = 0
  on(EVENTS.EXPIRED, () => { expiredFired++ })

  await auth.probe()

  assert.equal(auth.status, STATUS.AUTHENTICATED, 'network error must not flip to unauthenticated')
  assert.equal(expiredFired, 0)
})

test('probe(): concurrent calls dedup to one HTTP request', async () => {
  let calls = 0
  _setFetch(async () => {
    calls++
    // Tiny async delay so all three probes overlap.
    await new Promise((r) => setTimeout(r, 10))
    return new Response(JSON.stringify({ authenticated: true, expires_in: 3600 }), {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  })

  const auth = useAuthStore()
  await Promise.all([auth.probe(), auth.probe(), auth.probe()])
  assert.equal(calls, 1, 'only one fetch should fire for concurrent probes')
})


// ---- login() ----------------------------------------------------------------

test('login() success → status=authenticated + RESTORED + broadcast', async () => {
  _setFetch(async (url, init) => {
    assert.match(url, /\/api\/auth\/login/)
    assert.equal(init.method, 'POST')
    assert.deepEqual(JSON.parse(init.body), { api_key: 'good-key' })
    return new Response(JSON.stringify({ status: 'ok', csrf_token: 'fresh-csrf', expires_in: 3600 }), {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  })

  // Set up a second tab that should receive the broadcast.
  const otherTabMsgs = []
  const otherTab = new FakeBroadcastChannel('jarvis-auth')
  otherTab.onmessage = (ev) => otherTabMsgs.push(ev.data)

  let restoredFired = 0
  on(EVENTS.RESTORED, () => { restoredFired++ })

  const auth = useAuthStore()
  const result = await auth.login('good-key')

  assert.equal(result.ok, true)
  assert.equal(auth.status, STATUS.AUTHENTICATED)
  assert.equal(auth.csrfToken, 'fresh-csrf')
  assert.equal(restoredFired, 1)
  // Broadcast must have arrived in the other tab.
  assert.equal(otherTabMsgs.length, 1)
  assert.equal(otherTabMsgs[0].type, 'transition')
  assert.equal(otherTabMsgs[0].status, STATUS.AUTHENTICATED)
})

test('login() 401 returns reason from backend, status stays unchanged', async () => {
  _setFetch(async () => new Response(JSON.stringify({
    detail: { error: 'unauthorized', reason: 'invalid_credentials' },
  }), { status: 401, headers: { 'content-type': 'application/json' } }))

  const auth = useAuthStore()
  // Start from challenged so we can prove login failure doesn't flip to authenticated.
  auth.$patch({ status: STATUS.CHALLENGED })

  const result = await auth.login('wrong')
  assert.equal(result.ok, false)
  assert.equal(result.status, 401)
  assert.equal(result.reason, 'invalid_credentials')
  assert.equal(auth.status, STATUS.CHALLENGED, 'failed login must NOT change status')
})

test('login() network error returns ok:false / status:0', async () => {
  _setFetch(async () => { throw new TypeError('Failed to fetch') })
  const auth = useAuthStore()
  const result = await auth.login('whatever')
  assert.equal(result.ok, false)
  assert.equal(result.status, 0)
})


// ---- on401() soft-fail logic -----------------------------------------------

test('on401() probes once before locking; probe-success keeps user authenticated', async () => {
  let probeCalls = 0
  _setFetch(async () => {
    probeCalls++
    return new Response(JSON.stringify({ authenticated: true, expires_in: 3600 }), {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  })

  let expiredFired = 0
  on(EVENTS.EXPIRED, () => { expiredFired++ })

  const auth = useAuthStore()
  auth.$patch({ status: STATUS.AUTHENTICATED, csrfToken: 'tok' })
  await auth.on401('rest_401')

  assert.equal(probeCalls, 1)
  assert.equal(auth.status, STATUS.AUTHENTICATED, 'transient 401 must not lock if probe still good')
  assert.equal(expiredFired, 0)
})

test('on401() locks UI when probe also says unauthenticated', async () => {
  _setFetch(async () => new Response(JSON.stringify({ authenticated: false }), {
    status: 200, headers: { 'content-type': 'application/json' },
  }))

  let expiredFired = 0
  on(EVENTS.EXPIRED, () => { expiredFired++ })

  const auth = useAuthStore()
  auth.$patch({ status: STATUS.AUTHENTICATED })
  await auth.on401('rest_401')

  assert.equal(auth.status, STATUS.UNAUTHENTICATED)
  // EXPIRED fires once for the final lock (the soft "challenged" hop emits CHALLENGED, not EXPIRED).
  assert.equal(expiredFired, 1)
})

test('on401() is a no-op when already unauthenticated', async () => {
  let calls = 0
  _setFetch(async () => {
    calls++
    return new Response('{}', { status: 200 })
  })
  const auth = useAuthStore()
  auth.$patch({ status: STATUS.UNAUTHENTICATED })
  await auth.on401('whatever')
  assert.equal(calls, 0, 'should not re-probe when already locked')
})


// ---- logout() --------------------------------------------------------------

test('logout() clears state + broadcasts even if backend errors', async () => {
  _setFetch(async () => { throw new Error('backend down') })

  let expiredFired = 0
  on(EVENTS.EXPIRED, () => { expiredFired++ })

  const auth = useAuthStore()
  auth.$patch({ status: STATUS.AUTHENTICATED, csrfToken: 'old' })
  await auth.logout()

  assert.equal(auth.status, STATUS.UNAUTHENTICATED)
  assert.equal(auth.csrfToken, '')
  assert.equal(expiredFired, 1)
})


// ---- Cross-tab BroadcastChannel ---------------------------------------------

test('cross-tab: receiving authenticated transition restores local state', async () => {
  // Set up the local store with no init() so its channel listener is wired
  // when login() broadcasts. Actually we need to trigger _initChannel —
  // simplest is to call init() after stubbing whoami to authenticated:false
  // (we'll override status manually below).
  _setFetch(async () => new Response(JSON.stringify({ authenticated: false }), {
    status: 200, headers: { 'content-type': 'application/json' },
  }))

  const auth = useAuthStore()
  await auth.init()
  assert.equal(auth.status, STATUS.UNAUTHENTICATED)

  let restoredFired = 0
  on(EVENTS.RESTORED, () => { restoredFired++ })

  // Simulate a sibling tab broadcasting "authenticated".
  const sibling = new FakeBroadcastChannel('jarvis-auth')
  sibling.postMessage({
    type: 'transition',
    status: STATUS.AUTHENTICATED,
    csrfToken: 'csrf-from-sibling',
  })

  // BroadcastChannel handler is sync in our stub.
  assert.equal(auth.status, STATUS.AUTHENTICATED)
  assert.equal(auth.csrfToken, 'csrf-from-sibling')
  assert.equal(restoredFired, 1)
})

test('cross-tab: receiving unauthenticated transition locks local state', async () => {
  _setFetch(async () => new Response(JSON.stringify({ authenticated: true, expires_in: 3600 }), {
    status: 200, headers: { 'content-type': 'application/json' },
  }))

  const auth = useAuthStore()
  await auth.init()
  assert.equal(auth.status, STATUS.AUTHENTICATED)

  let expiredFired = 0
  on(EVENTS.EXPIRED, () => { expiredFired++ })

  const sibling = new FakeBroadcastChannel('jarvis-auth')
  sibling.postMessage({
    type: 'transition',
    status: STATUS.UNAUTHENTICATED,
  })

  assert.equal(auth.status, STATUS.UNAUTHENTICATED)
  assert.equal(expiredFired, 1)
})


// ---- Silent refresh --------------------------------------------------------

test('login() schedules silent refresh ~5min before exp', async () => {
  _setFetch(async () => new Response(JSON.stringify({
    status: 'ok', csrf_token: 'tok', expires_in: 3600,
  }), { status: 200, headers: { 'content-type': 'application/json' } }))

  const auth = useAuthStore()
  await auth.login('good-key')

  const t = _pendingTimer()
  assert.ok(t, 'a timer must be scheduled after login')
  // 3600 - 300 (lead) = 3300s = 3,300,000 ms
  assert.equal(t.ms, 3300 * 1000)
})

test('probe() also schedules silent refresh when authenticated', async () => {
  _setFetch(async () => new Response(JSON.stringify({
    authenticated: true, expires_in: 3600,
  }), { status: 200, headers: { 'content-type': 'application/json' } }))

  const auth = useAuthStore()
  await auth.probe()

  const t = _pendingTimer()
  assert.ok(t, 'a timer must be scheduled after a successful probe')
  assert.equal(t.ms, 3300 * 1000)
})

test('schedule honours the 30s minimum delay for tiny expires_in', async () => {
  _setFetch(async () => new Response(JSON.stringify({
    status: 'ok', csrf_token: 'tok', expires_in: 60,  // 1 minute window
  }), { status: 200, headers: { 'content-type': 'application/json' } }))

  const auth = useAuthStore()
  await auth.login('good-key')

  // 60 - 300 = -240 → clamped to 30s floor
  assert.equal(_pendingTimer().ms, 30 * 1000)
})

test('refresh() success extends session, sends X-CSRF-Token header, and reschedules', async () => {
  let calls = 0
  let refreshHeaders = null
  _setFetch(async (url, init) => {
    calls++
    if (url.includes('/api/auth/login')) {
      // Simulate the cookie write so getCsrfToken() returns tok-1.
      globalThis.document.cookie = 'jarvis_csrf=tok-1'
      return new Response(JSON.stringify({ status: 'ok', csrf_token: 'tok-1', expires_in: 3600 }), {
        status: 200, headers: { 'content-type': 'application/json' },
      })
    }
    if (url.includes('/api/auth/refresh')) {
      refreshHeaders = init?.headers || {}
      globalThis.document.cookie = 'jarvis_csrf=tok-2'
      return new Response(JSON.stringify({ status: 'ok', csrf_token: 'tok-2', expires_in: 3600 }), {
        status: 200, headers: { 'content-type': 'application/json' },
      })
    }
    throw new Error(`unexpected url: ${url}`)
  })

  const auth = useAuthStore()
  await auth.login('good-key')
  assert.equal(auth.csrfToken, 'tok-1')

  // Fire the scheduled refresh.
  await _fireTimer()

  assert.equal(calls, 2, 'login + refresh')
  assert.equal(auth.status, STATUS.AUTHENTICATED, 'still authenticated after refresh')
  assert.equal(auth.csrfToken, 'tok-2', 'CSRF rotated on refresh')
  // CRITICAL: refresh must echo the CSRF cookie as X-CSRF-Token, otherwise
  // CsrfMiddleware 403s the call (the endpoint is not in _EXEMPT_PREFIXES).
  assert.equal(refreshHeaders['X-CSRF-Token'], 'tok-1',
    'refresh must send the current CSRF cookie value as the header')
  // A NEW timer should have been scheduled by _setAuthenticated.
  assert.ok(_pendingTimer(), 'next refresh must be re-scheduled')
})

test('refresh() falls back to probe() when response is missing expires_in', async () => {
  let probeCalls = 0
  _setFetch(async (url) => {
    if (url.includes('/api/auth/login')) {
      globalThis.document.cookie = 'jarvis_csrf=tok-1'
      return new Response(JSON.stringify({ status: 'ok', csrf_token: 'tok-1', expires_in: 3600 }), {
        status: 200, headers: { 'content-type': 'application/json' },
      })
    }
    if (url.includes('/api/auth/refresh')) {
      // Malformed payload — missing expires_in.
      return new Response(JSON.stringify({ status: 'ok', csrf_token: 'tok-2' }), {
        status: 200, headers: { 'content-type': 'application/json' },
      })
    }
    if (url.includes('/api/auth/whoami')) {
      probeCalls++
      return new Response(JSON.stringify({ authenticated: true, expires_in: 3600 }), {
        status: 200, headers: { 'content-type': 'application/json' },
      })
    }
    throw new Error(`unexpected url: ${url}`)
  })

  const auth = useAuthStore()
  await auth.login('good-key')
  await _fireTimer()

  assert.equal(probeCalls, 1, 'whoami fallback must run')
  assert.equal(auth.status, STATUS.AUTHENTICATED, 'fallback keeps user authenticated')
  assert.ok(_pendingTimer(), 'fallback re-schedules next refresh via _setAuthenticated')
})

test('refresh() 401 (max_lifetime_exceeded) locks the UI', async () => {
  _setFetch(async (url) => {
    if (url.includes('/api/auth/login')) {
      return new Response(JSON.stringify({ status: 'ok', csrf_token: 'tok', expires_in: 3600 }), {
        status: 200, headers: { 'content-type': 'application/json' },
      })
    }
    return new Response(JSON.stringify({
      detail: { error: 'unauthorized', reason: 'max_lifetime_exceeded' },
    }), { status: 401, headers: { 'content-type': 'application/json' } })
  })

  let expiredFired = 0
  let expiredReason = ''
  on(EVENTS.EXPIRED, (p) => { expiredFired++; expiredReason = p?.reason })

  const auth = useAuthStore()
  await auth.login('good-key')
  await _fireTimer()

  assert.equal(auth.status, STATUS.UNAUTHENTICATED)
  assert.equal(expiredFired, 1)
  assert.equal(expiredReason, 'max_lifetime_exceeded')
})

test('refresh() 5xx keeps user authenticated and reschedules a short retry', async () => {
  _setFetch(async (url) => {
    if (url.includes('/api/auth/login')) {
      return new Response(JSON.stringify({ status: 'ok', csrf_token: 'tok', expires_in: 3600 }), {
        status: 200, headers: { 'content-type': 'application/json' },
      })
    }
    return new Response('upstream error', { status: 502 })
  })

  const auth = useAuthStore()
  await auth.login('good-key')
  await _fireTimer()

  assert.equal(auth.status, STATUS.AUTHENTICATED, 'transient 5xx must not lock')
  // Retry: 60 + 300 = 360s window minus 300s lead = 60s delay. Floor is 30s,
  // so 60s should hold.
  assert.equal(_pendingTimer().ms, 60 * 1000)
})

test('refresh() network error keeps user authenticated and reschedules a short retry', async () => {
  let firstCall = true
  _setFetch(async (url) => {
    if (url.includes('/api/auth/login')) {
      return new Response(JSON.stringify({ status: 'ok', csrf_token: 'tok', expires_in: 3600 }), {
        status: 200, headers: { 'content-type': 'application/json' },
      })
    }
    if (firstCall) {
      firstCall = false
      throw new TypeError('Failed to fetch')
    }
    return new Response('{}', { status: 200 })
  })

  const auth = useAuthStore()
  await auth.login('good-key')
  await _fireTimer()

  assert.equal(auth.status, STATUS.AUTHENTICATED, 'network error must not lock')
  assert.ok(_pendingTimer(), 'retry timer must be scheduled')
})

test('logout() cancels the pending refresh timer', async () => {
  _setFetch(async (url) => {
    if (url.includes('/api/auth/login')) {
      return new Response(JSON.stringify({ status: 'ok', csrf_token: 'tok', expires_in: 3600 }), {
        status: 200, headers: { 'content-type': 'application/json' },
      })
    }
    return new Response('{}', { status: 200 })
  })

  const auth = useAuthStore()
  await auth.login('good-key')
  assert.ok(_pendingTimer(), 'login should have scheduled a timer')

  await auth.logout()

  assert.equal(_pendingTimer(), null, 'logout must cancel the refresh timer')
})

test('refresh() is a no-op when not authenticated', async () => {
  let calls = 0
  _setFetch(async () => {
    calls++
    return new Response('{}', { status: 200 })
  })
  const auth = useAuthStore()
  // status is UNKNOWN at boot
  await auth.refresh()
  assert.equal(calls, 0, 'must not hit the network when not authenticated')
})

test('cross-tab AUTHENTICATED with expiresIn schedules sibling refresh', async () => {
  _setFetch(async () => new Response(JSON.stringify({ authenticated: false }), {
    status: 200, headers: { 'content-type': 'application/json' },
  }))

  const auth = useAuthStore()
  await auth.init()
  assert.equal(auth.status, STATUS.UNAUTHENTICATED)
  assert.equal(_pendingTimer(), null)

  const sibling = new FakeBroadcastChannel('jarvis-auth')
  sibling.postMessage({
    type: 'transition',
    status: STATUS.AUTHENTICATED,
    csrfToken: 'sib-csrf',
    expiresIn: 3600,
  })

  assert.equal(auth.status, STATUS.AUTHENTICATED)
  const t = _pendingTimer()
  assert.ok(t, 'sibling-driven login must schedule a refresh')
  assert.equal(t.ms, 3300 * 1000)
})

test('cross-tab UNAUTHENTICATED clears the local refresh timer', async () => {
  _setFetch(async (url) => {
    if (url.includes('/api/auth/whoami')) {
      return new Response(JSON.stringify({ authenticated: true, expires_in: 3600 }), {
        status: 200, headers: { 'content-type': 'application/json' },
      })
    }
    return new Response('{}', { status: 200 })
  })

  const auth = useAuthStore()
  await auth.init()
  assert.equal(auth.status, STATUS.AUTHENTICATED)
  assert.ok(_pendingTimer(), 'init authenticated → timer scheduled')

  const sibling = new FakeBroadcastChannel('jarvis-auth')
  sibling.postMessage({ type: 'transition', status: STATUS.UNAUTHENTICATED })

  assert.equal(auth.status, STATUS.UNAUTHENTICATED)
  assert.equal(_pendingTimer(), null, 'remote logout must cancel local refresh timer')
})


// ---- Getters ---------------------------------------------------------------

test('showAuthGate is true ONLY when status is unauthenticated', () => {
  const auth = useAuthStore()
  for (const s of [STATUS.UNKNOWN, STATUS.AUTHENTICATED, STATUS.CHALLENGED]) {
    auth.$patch({ status: s })
    assert.equal(auth.showAuthGate, false, `should be false for ${s}`)
  }
  auth.$patch({ status: STATUS.UNAUTHENTICATED })
  assert.equal(auth.showAuthGate, true)
})
