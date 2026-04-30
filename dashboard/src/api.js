/**
 * API configuration and helper for REST calls.
 * All fetch calls go through this to get consistent auth + error handling.
 */

const API_BASE = '' // Vite proxy handles /api → backend

// ─── Auto-initialize API key from Vite env (dev convenience) ───
const ENV_KEY = import.meta.env.VITE_JARVIS_API_KEY
if (ENV_KEY && !localStorage.getItem('jarvis_api_key')) {
  localStorage.setItem('jarvis_api_key', ENV_KEY)
}

export function getApiKey() {
  return localStorage.getItem('jarvis_api_key') || ''
}

export function setApiKey(key) {
  localStorage.setItem('jarvis_api_key', key)
}

export function clearApiKey() {
  localStorage.removeItem('jarvis_api_key')
}

// ─── 503 setup-required handler ───
// The backend's SetupGateMiddleware returns 503 + X-Setup-Required on every
// non-bootstrap endpoint until the wizard completes. We install one handler so
// every caller auto-redirects to /setup instead of surfacing a confusing error.
let _setupRequiredHandler = null
export function onSetupRequired(handler) {
  _setupRequiredHandler = handler
}
function _handleSetupRequired() {
  if (_setupRequiredHandler) {
    try {
      _setupRequiredHandler()
    } catch (e) {
      // Don't let handler errors mask the original 503
      console.error('[api] setup-required handler threw', e)
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

/**
 * Authenticated fetch wrapper.
 * @param {string} path - API path (e.g., '/api/agents')
 * @param {RequestInit & {skipAuth?: boolean, skipSetupRedirect?: boolean}} options
 * @returns {Promise<any>}
 */
export async function apiFetch(path, options = {}) {
  const { skipAuth, skipSetupRedirect, ...fetchOptions } = options
  const apiKey = getApiKey()
  const isFormData = fetchOptions.body instanceof FormData
  const headers = {
    // Don't set Content-Type for FormData — browser sets multipart boundary
    ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
    ...(!skipAuth && apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
    ...fetchOptions.headers,
  }
  // Remove Content-Type if explicitly omitted by caller for FormData
  if (isFormData) delete headers['Content-Type']

  const response = await fetch(`${API_BASE}${path}`, {
    ...fetchOptions,
    headers,
  })

  if (!response.ok) {
    const body = await response.text().catch(() => '')
    // Setup-gate: backend blocks API until wizard is complete. Redirect to the
    // wizard route unless the caller opted out (e.g. the wizard itself).
    if (
      response.status === 503 &&
      response.headers.get('X-Setup-Required') === 'true' &&
      !skipSetupRedirect
    ) {
      _handleSetupRequired()
    }
    let parsed = body
    try { parsed = JSON.parse(body) } catch (_) { /* not JSON */ }
    throw new ApiError(response.status, parsed, `API ${response.status}: ${body || response.statusText}`)
  }

  if (response.headers.get('content-type')?.includes('application/json')) {
    return response.json()
  }
  return response.text()
}

/**
 * Build SSE URL with query-param auth.
 * @param {string} path
 * @param {Record<string, string>} params
 * @returns {string}
 */
export function buildSSEUrl(path, params = {}) {
  const url = new URL(path, window.location.origin)
  const apiKey = getApiKey()
  if (apiKey) url.searchParams.set('api_key', apiKey)
  Object.entries(params).forEach(([k, v]) => {
    if (v) url.searchParams.set(k, v)
  })
  return url.toString()
}
