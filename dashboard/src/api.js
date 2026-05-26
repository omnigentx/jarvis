/**
 * API helper — cookie-session auth + CSRF double-submit.
 *
 * Auth model
 * ----------
 *
 * The backend authenticates the dashboard via the ``jarvis_session``
 * httpOnly cookie set by ``POST /api/auth/login`` (or by
 * ``POST /api/setup/auth`` during first-run). JS never sees that
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
 * SSE / WebSocket / ``<audio>`` callers do not call this module: the
 * session cookie is auto-attached by the browser for same-origin
 * requests (EventSource, WebSocket upgrade, ``audio.src``), so they
 * only need to drop ``?api_key=`` from their URLs.
 *
 * What this module deliberately does NOT do
 * -----------------------------------------
 *
 * No ``getApiKey()`` / ``setApiKey()`` / ``localStorage`` mirror. The
 * SPA used to keep ``JARVIS_API_KEY`` in localStorage and send it as a
 * Bearer header on every request — that path was removed because an
 * XSS could exfiltrate ``localStorage`` and lift the credential,
 * defeating the whole point of the httpOnly cookie. Machine-to-machine
 * callers (Xiaozhi voice device, scripts) still use Bearer against the
 * same endpoints; only the SPA is cookie-only.
 */

const API_BASE = '' // Vite proxy handles /api → backend

const CSRF_COOKIE_NAME = 'jarvis_csrf'

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
 *   skipAuth?: boolean,        // accepted for backwards compat with
 *                              //   callers that used to suppress the
 *                              //   legacy Bearer fallback. The Bearer
 *                              //   path is gone now; this flag is a
 *                              //   no-op kept to avoid churning every
 *                              //   call site in the same PR.
 *   skipSetupRedirect?: boolean,
 *   skipUnauthorizedHandler?: boolean,  // probe / login should not loop
 * }} options
 */
export async function apiFetch(path, options = {}) {
  const {
    skipAuth: _skipAuthUnused,
    skipSetupRedirect,
    skipUnauthorizedHandler,
    ...fetchOptions
  } = options

  const method = (fetchOptions.method || 'GET').toUpperCase()
  const isFormData = fetchOptions.body instanceof FormData

  const headers = {
    ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
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
