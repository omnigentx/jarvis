/**
 * Passkey (WebAuthn) client wrapper.
 *
 * Thin layer over ``@simplewebauthn/browser`` that calls the backend's
 * ``/api/auth/passkey/*`` ceremony endpoints. Two reasons it exists
 * rather than every caller wiring up the browser API directly:
 *
 *   1. **Begin / finish HTTP wiring** is identical for both flows;
 *      centralising it removes 60 LOC of boilerplate from AuthGate +
 *      SettingsAuth.
 *   2. **Error normalisation** — every call returns ``{ok, ...}``
 *      objects so callers branch on a small enum instead of
 *      ``try/catch`` over a heterogeneous mix of fetch failures,
 *      ``WebAuthnError`` subclasses, and backend 4xx bodies.
 *
 * Authentication-flow result codes
 * --------------------------------
 * ``unsupported``  — browser lacks WebAuthn (Safari < 14, headless).
 * ``no_passkey``   — backend says no credentials registered for this RP.
 * ``cancelled``    — user dismissed the platform dialog.
 * ``credential_unknown`` — assertion signed by a credential the server
 *                          doesn't know (browser keychain restored
 *                          across a wipe, or wrong RP).
 * ``network``      — couldn't reach the backend at all.
 * ``error``        — fallback bucket; ``.detail`` carries the raw text.
 */
import {
  browserSupportsWebAuthn,
  startAuthentication,
  startRegistration,
  WebAuthnError,
} from '@simplewebauthn/browser'

import { apiFetch, ApiError } from '../api.js'

/** Indirection layer so unit tests can swap the SimpleWebAuthn calls
 *  without monkey-patching ESM module namespaces (which Node rejects).
 *  Production code never touches ``_deps`` — only tests do. */
export const _deps = {
  browserSupportsWebAuthn,
  startRegistration,
  startAuthentication,
}

export function isSupported() {
  return _deps.browserSupportsWebAuthn()
}

/** Probe used by AuthGate at mount: should we show the passkey
 *  button? Returns false on any error so a backend hiccup never blocks
 *  the API-key fallback path. */
export async function hasAnyPasskey() {
  try {
    const resp = await fetch('/api/auth/passkey/has-any', {
      credentials: 'include',
    })
    if (!resp.ok) return false
    const body = await resp.json()
    return !!body.has_passkey
  } catch (_) {
    return false
  }
}

/**
 * Register a new passkey. Must be called from an authenticated session
 * (Bearer key OR cookie); the backend's ``/register/begin`` rejects
 * unauthenticated callers with 401 and we surface that as ``error``.
 *
 * @param {{label?: string}} options
 * @returns {Promise<{ok: true, credentialId: string} | {ok: false, code: string, detail?: string}>}
 */
export async function registerPasskey({ label } = {}) {
  if (!isSupported()) {
    return { ok: false, code: 'unsupported' }
  }

  let begin
  try {
    begin = await apiFetch('/api/auth/passkey/register/begin', {
      method: 'POST',
      body: JSON.stringify({}),
    })
  } catch (err) {
    return _mapBeginError(err)
  }

  let assertion
  try {
    // SimpleWebAuthn handles base64url ↔ ArrayBuffer + calls
    // ``navigator.credentials.create()`` under the hood. We pass the
    // server-shaped options through unchanged.
    assertion = await _deps.startRegistration({ optionsJSON: begin.options })
  } catch (err) {
    return _mapAuthenticatorError(err)
  }

  try {
    const finish = await apiFetch('/api/auth/passkey/register/finish', {
      method: 'POST',
      body: JSON.stringify({
        ceremony_id: begin.ceremony_id,
        credential: assertion,
        label: label || null,
      }),
    })
    return { ok: true, credentialId: finish.credential_id, replaced: !!finish.replaced }
  } catch (err) {
    return _mapFinishError(err)
  }
}

/**
 * Sign in with an existing passkey. Public (no auth required) — a
 * successful assertion IS the auth, and the backend's
 * ``/authenticate/finish`` route mints the same session cookie that
 * ``POST /api/auth/login`` does.
 *
 * @returns {Promise<{ok: true, csrfToken: string, expiresIn: number} | {ok: false, code: string, detail?: string}>}
 */
export async function authenticateWithPasskey() {
  if (!isSupported()) {
    return { ok: false, code: 'unsupported' }
  }

  let begin
  try {
    const resp = await fetch('/api/auth/passkey/authenticate/begin', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })
    if (!resp.ok) return _mapHttpStatus(resp.status)
    begin = await resp.json()
  } catch (_) {
    return { ok: false, code: 'network' }
  }

  let assertion
  try {
    assertion = await _deps.startAuthentication({ optionsJSON: begin.options })
  } catch (err) {
    return _mapAuthenticatorError(err)
  }

  let finishResp
  try {
    finishResp = await fetch('/api/auth/passkey/authenticate/finish', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ceremony_id: begin.ceremony_id,
        credential: assertion,
      }),
    })
  } catch (_) {
    return { ok: false, code: 'network' }
  }

  if (!finishResp.ok) {
    let reason = ''
    try {
      const body = await finishResp.json()
      reason = body?.detail?.reason || ''
    } catch (_) { /* not JSON */ }
    if (finishResp.status === 401) {
      // ``credential_unknown`` happens when the browser keeps a
      // passkey the server forgot (DB wipe, cross-deployment) — show
      // it as a discrete state so the UI can suggest "register again
      // with API key".
      if (reason === 'credential_unknown') {
        return { ok: false, code: 'credential_unknown' }
      }
      return { ok: false, code: 'auth_failed', detail: reason }
    }
    return _mapHttpStatus(finishResp.status)
  }
  const body = await finishResp.json()
  return {
    ok: true,
    csrfToken: body.csrf_token,
    expiresIn: body.expires_in,
  }
}

/** List registered passkeys for the current RP. Auth required. */
export async function listPasskeys() {
  try {
    const rows = await apiFetch('/api/auth/passkey/list')
    return { ok: true, rows }
  } catch (err) {
    return _mapBeginError(err)
  }
}

/** Delete a passkey by credential id. Auth required. */
export async function deletePasskey(credentialId) {
  try {
    await apiFetch(
      `/api/auth/passkey/${encodeURIComponent(credentialId)}`,
      { method: 'DELETE' },
    )
    return { ok: true }
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      return { ok: false, code: 'not_found' }
    }
    return _mapBeginError(err)
  }
}

// ---- Error mapping ---------------------------------------------------------

function _mapHttpStatus(status) {
  if (status === 401) return { ok: false, code: 'auth_failed' }
  if (status === 404) return { ok: false, code: 'no_passkey' }
  if (status === 429) return { ok: false, code: 'rate_limited' }
  return { ok: false, code: 'error', detail: `HTTP ${status}` }
}

function _mapBeginError(err) {
  if (err instanceof ApiError) {
    return _mapHttpStatus(err.status)
  }
  return { ok: false, code: 'network' }
}

function _mapFinishError(err) {
  if (err instanceof ApiError) {
    const reason = err?.body?.detail?.reason || ''
    return {
      ok: false,
      code: err.status === 400 ? 'verify_failed' : 'auth_failed',
      detail: reason,
    }
  }
  return { ok: false, code: 'network' }
}

function _mapAuthenticatorError(err) {
  // SimpleWebAuthn surfaces a typed error with ``.name`` derived from
  // the underlying ``DOMException`` (NotAllowedError, AbortError, …).
  // The two we care about distinguishing are user-cancel vs
  // platform-unsupported.
  if (err instanceof WebAuthnError) {
    if (err.code === 'ERROR_PASSTHROUGH_SEE_CAUSE_PROPERTY') {
      const cause = err.cause
      if (cause?.name === 'NotAllowedError') {
        return { ok: false, code: 'cancelled' }
      }
      if (cause?.name === 'InvalidStateError') {
        // Trying to register a credential the authenticator already
        // holds for this RP. Surface as a distinct state so the UI
        // can say "already registered" rather than a generic error.
        return { ok: false, code: 'already_registered' }
      }
    }
    return { ok: false, code: 'error', detail: err.message }
  }
  if (err?.name === 'NotAllowedError') {
    return { ok: false, code: 'cancelled' }
  }
  return { ok: false, code: 'error', detail: String(err?.message || err) }
}
