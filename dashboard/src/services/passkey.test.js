/**
 * Unit tests for ``services/passkey.js``.
 *
 * We test the wiring + error normalisation, not the SimpleWebAuthn
 * library itself (tested upstream) and not the real authenticator
 * ceremony (covered by E2E with the Playwright virtual authenticator).
 *
 * Stubbing strategy: the service exposes ``_deps`` — a mutable object
 * holding the three SimpleWebAuthn entry points it calls. Tests replace
 * those with spies; production never touches ``_deps``. This sidesteps
 * the fact that Node refuses to redefine ESM module namespace exports.
 */
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'

// ---- fetch + localStorage stubs (must be set BEFORE importing) -------------

let _fetchCalls = []
let _fetchImpl = async () => { throw new Error('fetch not set') }
globalThis.fetch = (url, init) => {
  _fetchCalls.push({ url, init })
  return _fetchImpl(url, init)
}

globalThis.localStorage = globalThis.localStorage || {
  _data: {},
  getItem(k) { return this._data[k] ?? null },
  setItem(k, v) { this._data[k] = String(v) },
  removeItem(k) { delete this._data[k] },
}

globalThis.document = globalThis.document || { cookie: '' }

const passkey = await import('./passkey.js')
const realDeps = { ...passkey._deps }

beforeEach(() => {
  _fetchCalls = []
  _fetchImpl = async () => { throw new Error('fetch not set') }
  localStorage._data = {}
  document.cookie = ''
  passkey._deps.browserSupportsWebAuthn = () => true
  passkey._deps.startRegistration = async () => { throw new Error('reg not set') }
  passkey._deps.startAuthentication = async () => { throw new Error('auth not set') }
})

function _scriptFetch(routes) {
  /** Map of ``"METHOD path"`` → Response (or function returning one).
   *  Unscripted calls throw so a forgotten stub fails loudly. */
  _fetchImpl = async (url, init) => {
    const method = (init?.method || 'GET').toUpperCase()
    const path = url.replace(/^https?:\/\/[^/]+/, '')
    const key = `${method} ${path}`
    if (!(key in routes)) {
      throw new Error(`unscripted fetch: ${key}`)
    }
    const handler = routes[key]
    return typeof handler === 'function' ? handler(init) : handler
  }
}

function _jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

// ---- isSupported / hasAnyPasskey -------------------------------------------

test('isSupported reflects browserSupportsWebAuthn', () => {
  passkey._deps.browserSupportsWebAuthn = () => true
  assert.equal(passkey.isSupported(), true)
  passkey._deps.browserSupportsWebAuthn = () => false
  assert.equal(passkey.isSupported(), false)
})

test('hasAnyPasskey returns true on {has_passkey:true}', async () => {
  _scriptFetch({
    'GET /api/auth/passkey/has-any': _jsonResponse(200, { has_passkey: true }),
  })
  assert.equal(await passkey.hasAnyPasskey(), true)
})

test('hasAnyPasskey returns false on backend 5xx (graceful)', async () => {
  _scriptFetch({
    'GET /api/auth/passkey/has-any': _jsonResponse(500, { error: 'boom' }),
  })
  assert.equal(await passkey.hasAnyPasskey(), false)
})

test('hasAnyPasskey returns false on network error (graceful)', async () => {
  _fetchImpl = async () => { throw new TypeError('Failed to fetch') }
  assert.equal(await passkey.hasAnyPasskey(), false)
})

// ---- registerPasskey -------------------------------------------------------

test('registerPasskey unsupported short-circuits without fetching', async () => {
  passkey._deps.browserSupportsWebAuthn = () => false
  const result = await passkey.registerPasskey({ label: 'Mac' })
  assert.deepEqual(result, { ok: false, code: 'unsupported' })
  assert.equal(_fetchCalls.length, 0)
})

test('registerPasskey happy path returns credential id', async () => {
  _scriptFetch({
    'POST /api/auth/passkey/register/begin': _jsonResponse(200, {
      ceremony_id: 'cer-1',
      options: { challenge: 'abc', rp: { id: 'localhost' } },
    }),
    'POST /api/auth/passkey/register/finish': _jsonResponse(200, {
      status: 'ok', credential_id: 'cred-xyz', replaced: false,
    }),
  })
  passkey._deps.startRegistration = async ({ optionsJSON }) => {
    assert.equal(optionsJSON.challenge, 'abc')
    return { id: 'cred-xyz', response: { transports: ['internal'] } }
  }
  const result = await passkey.registerPasskey({ label: 'My Mac' })
  assert.deepEqual(result, { ok: true, credentialId: 'cred-xyz', replaced: false })
})

test('registerPasskey surfaces 401 from begin as auth_failed', async () => {
  _scriptFetch({
    'POST /api/auth/passkey/register/begin': _jsonResponse(401, {
      detail: { error: 'unauthorized' },
    }),
  })
  const result = await passkey.registerPasskey({})
  assert.equal(result.ok, false)
  assert.equal(result.code, 'auth_failed')
})

test('registerPasskey maps NotAllowedError → cancelled', async () => {
  _scriptFetch({
    'POST /api/auth/passkey/register/begin': _jsonResponse(200, {
      ceremony_id: 'cer-1', options: {},
    }),
  })
  passkey._deps.startRegistration = async () => {
    const err = new Error('user cancelled')
    err.name = 'NotAllowedError'
    throw err
  }
  const result = await passkey.registerPasskey({})
  assert.equal(result.ok, false)
  assert.equal(result.code, 'cancelled')
})

test('registerPasskey surfaces 400 verify failure with reason', async () => {
  _scriptFetch({
    'POST /api/auth/passkey/register/begin': _jsonResponse(200, {
      ceremony_id: 'cer-1', options: {},
    }),
    'POST /api/auth/passkey/register/finish': _jsonResponse(400, {
      detail: {
        error: 'passkey_register_failed',
        reason: 'attestation_invalid',
      },
    }),
  })
  passkey._deps.startRegistration = async () => ({ id: 'c', response: {} })
  const result = await passkey.registerPasskey({})
  assert.equal(result.ok, false)
  assert.equal(result.code, 'verify_failed')
  assert.equal(result.detail, 'attestation_invalid')
})

// ---- authenticateWithPasskey -----------------------------------------------

test('authenticateWithPasskey happy path returns csrf + expires', async () => {
  _scriptFetch({
    'POST /api/auth/passkey/authenticate/begin': _jsonResponse(200, {
      ceremony_id: 'auth-cer-1', options: { challenge: 'xyz' },
    }),
    'POST /api/auth/passkey/authenticate/finish': _jsonResponse(200, {
      status: 'ok', csrf_token: 'csrf-abc', expires_in: 3600,
    }),
  })
  passkey._deps.startAuthentication = async ({ optionsJSON }) => {
    assert.equal(optionsJSON.challenge, 'xyz')
    return { id: 'happy-cred', response: { signature: 'sig' } }
  }
  const result = await passkey.authenticateWithPasskey()
  assert.deepEqual(result, {
    ok: true, csrfToken: 'csrf-abc', expiresIn: 3600,
  })
})

test('authenticateWithPasskey maps 401 credential_unknown distinctly', async () => {
  _scriptFetch({
    'POST /api/auth/passkey/authenticate/begin': _jsonResponse(200, {
      ceremony_id: 'cer', options: {},
    }),
    'POST /api/auth/passkey/authenticate/finish': _jsonResponse(401, {
      detail: {
        error: 'passkey_auth_failed',
        reason: 'credential_unknown',
      },
    }),
  })
  passkey._deps.startAuthentication = async () => ({ id: 'unknown-cred', response: {} })
  const result = await passkey.authenticateWithPasskey()
  assert.deepEqual(result, { ok: false, code: 'credential_unknown' })
})

test('authenticateWithPasskey maps NotAllowedError → cancelled', async () => {
  _scriptFetch({
    'POST /api/auth/passkey/authenticate/begin': _jsonResponse(200, {
      ceremony_id: 'cer', options: {},
    }),
  })
  passkey._deps.startAuthentication = async () => {
    const err = new Error('cancelled')
    err.name = 'NotAllowedError'
    throw err
  }
  const result = await passkey.authenticateWithPasskey()
  assert.equal(result.code, 'cancelled')
})

test('authenticateWithPasskey maps 429 → rate_limited', async () => {
  _scriptFetch({
    'POST /api/auth/passkey/authenticate/begin': _jsonResponse(200, {
      ceremony_id: 'cer', options: {},
    }),
    'POST /api/auth/passkey/authenticate/finish': _jsonResponse(429, {
      detail: { error: 'rate_limited' },
    }),
  })
  passkey._deps.startAuthentication = async () => ({ id: 'c', response: {} })
  const result = await passkey.authenticateWithPasskey()
  assert.equal(result.code, 'rate_limited')
})

test('authenticateWithPasskey unsupported never reaches network', async () => {
  passkey._deps.browserSupportsWebAuthn = () => false
  const result = await passkey.authenticateWithPasskey()
  assert.deepEqual(result, { ok: false, code: 'unsupported' })
  assert.equal(_fetchCalls.length, 0)
})

// ---- listPasskeys / deletePasskey ------------------------------------------

test('listPasskeys returns rows on success', async () => {
  const rows = [
    { id: 'a', label: 'Mac', rp_id: 'localhost', created_at: 1, transports: ['internal'] },
  ]
  localStorage.setItem('jarvis_api_key', 'test-bearer')
  _scriptFetch({
    'GET /api/auth/passkey/list': _jsonResponse(200, rows),
  })
  const result = await passkey.listPasskeys()
  assert.deepEqual(result, { ok: true, rows })
})

test('deletePasskey returns not_found on 404', async () => {
  localStorage.setItem('jarvis_api_key', 'test-bearer')
  _scriptFetch({
    'DELETE /api/auth/passkey/missing-id': _jsonResponse(404, {
      detail: { error: 'passkey_not_found' },
    }),
  })
  const result = await passkey.deletePasskey('missing-id')
  assert.deepEqual(result, { ok: false, code: 'not_found' })
})

test('deletePasskey url-encodes credential id', async () => {
  localStorage.setItem('jarvis_api_key', 'test-bearer')
  let seenUrl = ''
  _fetchImpl = async (url, _init) => {
    seenUrl = url
    return _jsonResponse(200, { status: 'ok' })
  }
  await passkey.deletePasskey('credential/with/slashes')
  assert.ok(
    seenUrl.includes('credential%2Fwith%2Fslashes'),
    `Expected url-encoded id, got ${seenUrl}`,
  )
})

// Restore deps so other test files (if they import passkey) see the
// real SimpleWebAuthn entry points.
test('teardown — restore _deps', () => {
  Object.assign(passkey._deps, realDeps)
})
