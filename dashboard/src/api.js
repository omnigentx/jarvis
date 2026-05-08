/**
 * API helper — cookie-session auth + CSRF double-submit.
 *
 * Auth model
 * ----------
 *
 * The backend authenticates the dashboard via the ``jarvis_session``
 * httpOnly cookie set by ``POST /api/auth/login``. JS never sees that
 * value (httpOnly), which means an XSS that exfiltrates ``document.cookie``
 * cannot lift the session token.
 *
 * For CSRF defence we use the double-submit pattern: the backend
 * additionally sets a non-httpOnly ``jarvis_csrf`` cookie. On every
 * mutating request we copy that cookie value into the
 * ``X-CSRF-Token`` header. An attacker on a third-party origin can
 * cause the cookie to be sent (SameSite=Lax allows top-level form
 * POSTs in some flows) but cannot read it to populate the header,
 * so the backend's CsrfMiddleware rejects the request with 403.
 *
 * SSE behavior is unchanged from the caller's perspective — they still
 * call ``buildSSEUrl(path, params)`` — but the URL no longer carries
 * ``?api_key=`` because the browser auto-attaches the session cookie.
 *
 * Backwards-compat with localStorage (transition only)
 * ----------------------------------------------------
 *
 * ``getApiKey()`` / ``setApiKey()`` / ``clearApiKey()`` remain exported
 * so the Setup Wizard and SettingsGeneral can read/write the legacy
 * key (still needed: it's the value the user types in the AuthGate
 * modal — we POST it to ``/api/auth/login`` and the backend hands us a
 * session cookie back). Once Phase-2 lands across the codebase those
 * helpers can become DB-only (no localStorage).
 */

const API_BASE = '' // Vite proxy handles /api → backend

// ─── Auto-initialize API key from Vite env (dev convenience) ───
// Guarded against test runs (node:test has no import.meta.env) and any
// future SSR context where ``localStorage`` is unavailable.
const ENV_KEY =
  (typeof import.meta !== 'undefined' && import.meta.env && import.meta.env.VITE_JARVIS_API_KEY) || ''
if (
  ENV_KEY &&
  typeof localStorage !== 'undefined' &&
  !localStorage.getItem('jarvis_api_key')
) {
  localStorage.setItem('jarvis_api_key', ENV_KEY)
}

const CSRF_COOKIE_NAME = 'jarvis_csrf'

export function getApiKey() {
  return localStorage.getItem('jarvis_api_key') || ''
}

export function setApiKey(key) {
  localStorage.setItem('jarvis_api_key', key)
}

export function clearApiKey() {
  localStorage.removeItem('jarvis_api_key')
}

/** Read the CSRF cookie set by ``POST /api/auth/login``. Empty string
 *  if the cookie is absent (i.e. user not logged in yet). */
export function getCsrfToken() {
  if (typeof document === 'undefined') return ''
  const match = document.cookie
    .split('; ')
    .find((row) => row.startsWith(`${CSRF_COOKIE_NAME}=`))
  return match ? decodeURIComponent(match.split('=', 2)[1] ?? '') : ''
}

// ─── 503 setup-required handler ───
let _setupRequiredHandler = null
export function onSetupRequired(handler) {
  _setupRequiredHandler = handler
}
function _handleSetupRequired() {
  if (_setupRequiredHandler) {
    try {
      _setupRequiredHandler()
    } catch (e) {
      console.error('[api] setup-required handler threw', e)
    }
  }
}

// ─── 401 handler — installed by the auth store's plugin code so this
//     module stays free of a Pinia dependency cycle (api.js is
//     imported by the auth store itself).
let _unauthorizedHandler = null
export function onUnauthorized(handler) {
  _unauthorizedHandler = handler
}
function _handleUnauthorized(reason) {
  if (_unauthorizedHandler) {
    try {
      _unauthorizedHandler(reason)
    } catch (e) {
      console.error('[api] unauthorized handler threw', e)
    }
  }
}

export class ApiError extends Error {
  constructor(status, body, message) {
    super(message || `API ${status}: ${body || ''}`)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

const _MUTATING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

/**
 * Authenticated fetch wrapper.
 *
 * @param {string} path
 * @param {RequestInit & {
 *   skipAuth?: boolean,        // suppress the legacy Bearer fallback
 *   skipSetupRedirect?: boolean,
 *   skipUnauthorizedHandler?: boolean,  // probe / login should not loop
 * }} options
 */
export async function apiFetch(path, options = {}) {
  const {
    skipAuth,
    skipSetupRedirect,
    skipUnauthorizedHandler,
    ...fetchOptions
  } = options

  const method = (fetchOptions.method || 'GET').toUpperCase()
  const apiKey = getApiKey()
  const isFormData = fetchOptions.body instanceof FormData

  const headers = {
    ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
    // Legacy Bearer fallback: programmatic clients (Xiaozhi, scripts)
    // still rely on it; the dashboard sends both header and cookie
    // during the transition so a half-deployed mix-and-match works.
    //
    // FOLLOW-UP (tracking issue: drop the localStorage key entirely):
    // sending Bearer here means the API key remains exfiltrate-able
    // via XSS — partially defeating the cookie-auth XSS mitigation.
    // Once we've verified no first-party caller depends on the
    // legacy path (Setup Wizard reads it from localStorage; needs to
    // be re-plumbed to receive the key through a one-shot route),
    // delete this branch and the localStorage helpers.
    ...(!skipAuth && apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
    ...fetchOptions.headers,
  }
  if (isFormData) delete headers['Content-Type']

  // CSRF for state-changing methods. The header is mandatory whenever
  // a CSRF cookie exists; if the cookie is absent (not logged in) the
  // backend's middleware lets the request through and the route's auth
  // dependency takes care of the 401.
  if (_MUTATING_METHODS.has(method)) {
    const csrf = getCsrfToken()
    if (csrf && !headers['X-CSRF-Token']) {
      headers['X-CSRF-Token'] = csrf
    }
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...fetchOptions,
    method,
    credentials: 'include',  // send cookies on same-origin / proxied paths
    headers,
  })

  if (!response.ok) {
    const body = await response.text().catch(() => '')
    let parsed = body
    try { parsed = JSON.parse(body) } catch (_) { /* not JSON */ }

    if (
      response.status === 503 &&
      response.headers.get('X-Setup-Required') === 'true' &&
      !skipSetupRedirect
    ) {
      _handleSetupRequired()
    }

    if (response.status === 401 && !skipUnauthorizedHandler) {
      const reason =
        (parsed && parsed.detail && parsed.detail.reason) ||
        (parsed && parsed.detail) ||
        'unauthorized'
      _handleUnauthorized(reason)
    }

    throw new ApiError(
      response.status, parsed,
      `API ${response.status}: ${body || response.statusText}`,
    )
  }

  if (response.headers.get('content-type')?.includes('application/json')) {
    return response.json()
  }
  return response.text()
}

/**
 * Build SSE URL.  Cookie auth means the path no longer needs the
 * ``?api_key=`` query parameter — the browser auto-attaches the
 * httpOnly session cookie. We keep the `params` arg for callers that
 * still use it for non-auth filters (agent_name, etc).
 */
export function buildSSEUrl(path, params = {}) {
  const url = new URL(path, window.location.origin)
  Object.entries(params).forEach(([k, v]) => {
    if (v) url.searchParams.set(k, v)
  })
  return url.toString()
}
