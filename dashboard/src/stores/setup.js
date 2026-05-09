/**
 * Pinia store for the 5-step Setup Wizard.
 *
 * Wraps /api/setup/* and owns the canonical wizard state so every step
 * component can read `steps`, `overallComplete`, and `currentStep` without
 * refetching.  After any mutation the backend echoes the fresh SetupStatus
 * which we then commit — this keeps the UI and the server's setup-gate cache
 * perfectly consistent even when the user navigates away and back.
 */
import { defineStore } from 'pinia'
import { apiFetch, setApiKey, ApiError } from '../api'

const STEP_ORDER = ['auth', 'llm', 'services', 'yaml_config', 'verify']
const CRITICAL_STEPS = new Set(['auth', 'llm', 'verify'])

export const useSetupStore = defineStore('setup', {
  state: () => ({
    steps: [],
    overallComplete: false,
    currentStep: null,
    loading: false,
    error: null,
    lastSubmitError: null,
    // In-memory cache of what the user typed in each step so that navigating
    // Back/Forward inside the wizard doesn't force them to retype.  Not
    // persisted to localStorage — reloading the browser intentionally clears
    // secrets from RAM.  Shape: { services: { [serviceId]: { [fieldKey]: value } } }
    serviceDraft: {},
  }),
  getters: {
    stepByName: (state) => (name) =>
      state.steps.find((s) => s.name === name) || null,
    stepIndex: () => (name) => STEP_ORDER.indexOf(name),
    isCritical: () => (name) => CRITICAL_STEPS.has(name),
    stepOrder: () => STEP_ORDER,
  },
  actions: {
    _applyStatus(status) {
      if (!status) return
      this.steps = status.steps || []
      this.overallComplete = !!status.overall_complete
      this.currentStep = status.current_step || null
    },
    async fetchStatus() {
      this.loading = true
      this.error = null
      try {
        // /api/setup/status is behind verify_api_key, but the server also
        // leaves /api/setup/* outside the setup-gate so we can call it during
        // bootstrap.  If no key is stored yet (first ever visit), the call
        // will 401 — callers must treat that as "wizard step 1 pending".
        const res = await apiFetch('/api/setup/status', {
          skipSetupRedirect: true,
        })
        this._applyStatus(res)
        return res
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          // No key yet → step 1 is the current step by definition.
          this.steps = STEP_ORDER.map((name) => ({
            name,
            completed: false,
            skipped: false,
          }))
          this.currentStep = 'auth'
          this.overallComplete = false
          this.error = null
          return null
        }
        this.error = err.message || String(err)
        throw err
      } finally {
        this.loading = false
      }
    },
    async submitAuth({ apiKey }) {
      // Always send a key from the client so the UI knows it — the backend
      // would otherwise generate one server-side and we'd have no way to
      // authenticate the next request.  The caller is responsible for
      // generating via `generateApiKey()` when the user wants auto-pick.
      if (!apiKey || typeof apiKey !== 'string') {
        throw new Error('apiKey is required')
      }
      this.lastSubmitError = null
      try {
        const res = await apiFetch('/api/setup/auth', {
          method: 'POST',
          skipAuth: true,
          skipSetupRedirect: true,
          body: JSON.stringify({ api_key: apiKey }),
        })
        setApiKey(apiKey)
        this._applyStatus(res)

        // Mint a session cookie now so the dashboard's auth.probe() at
        // the next non-bare route resolves authenticated:true. Without
        // this, the user finishes the wizard, navigates to /agents,
        // and immediately hits the AuthGate modal with "session
        // expired" — confusing UX since they just typed the key.
        //
        // Best-effort: if /api/auth/login is unreachable here (older
        // backend, network blip), don't block the wizard. The
        // dashboard will fall back to the modal which now serves as a
        // proper recovery path. We import the auth store lazily to
        // avoid pulling Pinia into module-init order assumptions of
        // the setup store itself.
        try {
          const { useAuthStore } = await import('./auth.js')
          await useAuthStore().login(apiKey)
        } catch (_) { /* non-fatal */ }

        return res
      } catch (err) {
        this.lastSubmitError = _formatApiError(err)
        throw err
      }
    },
    async submitLLM(payload) {
      return this._submit('/api/setup/llm', payload)
    },
    async submitServices(payload) {
      // Cache the draft so the user can revisit Step 3 without retyping.
      if (payload && payload.services) {
        this.serviceDraft = { ...payload.services }
      }
      return this._submit('/api/setup/services', payload)
    },
    setServiceDraft(draft) {
      this.serviceDraft = draft && typeof draft === 'object' ? { ...draft } : {}
    },
    async submitYaml() {
      return this._submit('/api/setup/yaml_config', {})
    },
    async submitVerify(payload) {
      return this._submit('/api/setup/verify', payload)
    },
    async skipStep(name) {
      try {
        const res = await apiFetch(
          `/api/setup/step/${encodeURIComponent(name)}/skip`,
          { method: 'POST', skipSetupRedirect: true },
        )
        this._applyStatus(res)
        return res
      } catch (err) {
        this.lastSubmitError = _formatApiError(err)
        throw err
      }
    },
    async _submit(path, payload) {
      this.lastSubmitError = null
      try {
        const res = await apiFetch(path, {
          method: 'POST',
          skipSetupRedirect: true,
          body: JSON.stringify(payload || {}),
        })
        this._applyStatus(res)
        return res
      } catch (err) {
        this.lastSubmitError = _formatApiError(err)
        throw err
      }
    },
  },
})

export function generateApiKey() {
  // 32 random bytes → URL-safe base64, matching backend's py_secrets.token_urlsafe(32).
  const bytes = new Uint8Array(32)
  crypto.getRandomValues(bytes)
  let str = ''
  for (const b of bytes) str += String.fromCharCode(b)
  return btoa(str).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function _formatApiError(err) {
  if (err instanceof ApiError) {
    const body = err.body
    if (body && typeof body === 'object') {
      if (typeof body.detail === 'string') return body.detail
      if (body.detail && typeof body.detail === 'object') {
        const msg = body.detail.message || JSON.stringify(body.detail)
        const missing = Array.isArray(body.detail.missing) ? body.detail.missing : null
        if (missing && missing.length) {
          return `${msg} Missing: ${missing.join(', ')}`
        }
        return msg
      }
      return body.message || JSON.stringify(body)
    }
    return body || err.message
  }
  return err?.message || String(err)
}
