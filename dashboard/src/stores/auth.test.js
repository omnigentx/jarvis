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

// Now import the store (after globals are in place).
const { useAuthStore, STATUS, _resetAuthModuleForTests } = await import('./auth.js')

beforeEach(() => {
  setActivePinia(createPinia())
  resetBus()
  _resetAuthModuleForTests()
  FakeBroadcastChannel._channels.length = 0
  globalThis.document.cookie = ''
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
