<script setup>
/**
 * Shared Google OAuth credential + consent card.
 *
 * Renders the entire Google connection lifecycle so both Settings → Services
 * and Setup Wizard Step 3 can embed the SAME flow without duplicating
 * client-credential paste forms, popup logic, desktop paste-URL fallback,
 * disconnect, and the API-enable checklist.
 *
 * Branches (mutually exclusive, driven by /api/oauth/google/status):
 *   A) client_type === 'none' OR user clicked "Change credentials"
 *      → form to paste client_id + client_secret + radio (desktop/web)
 *   B) client_type === 'desktop' && !connected → "Open consent" + paste-URL
 *   C) client_type === 'web' && !connected → popup OAuth flow
 *   D) connected → details + Disconnect
 *   E) Whenever client_type !== 'none' → API enable checklist
 *
 * Auth: every fetch goes through apiFetch which attaches the bearer
 * token from local storage. The setup gate must allow /api/oauth (see
 * backend/middleware/setup_gate.py::_ALLOWED_PREFIXES) so this card
 * works even mid-wizard.
 */
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { apiFetch } from '../api'
import { useConfirm } from '../composables/useConfirm'

const { confirm } = useConfirm()

const googleStatus = ref({
  client_configured: false,
  client_type: 'none',
  desktop_redirect_uri: null,
  connected: false,
  scopes: [],
  expires_at: null,
  has_refresh_token: false,
  project_number: null,
  required_apis: [],
})
const loading = ref(true)
const statusError = ref('')
const showCredentialsForm = ref(false)

async function fetchGoogleStatus() {
  loading.value = true
  statusError.value = ''
  try {
    googleStatus.value = await apiFetch('/api/oauth/google/status')
  } catch (err) {
    statusError.value = err?.body?.detail || err?.message || String(err)
  } finally {
    loading.value = false
  }
}

// ── Client credentials editor ────────────────────────────────────────
const clientId = ref('')
const clientSecret = ref('')
const clientTypeChoice = ref('desktop')
const clientSaving = ref(false)
const clientSaved = ref(false)
const clientError = ref('')

async function saveClient() {
  if (!clientId.value.trim() || !clientSecret.value.trim()) {
    clientError.value = 'Both client_id and client_secret are required.'
    return
  }
  clientSaving.value = true
  clientError.value = ''
  clientSaved.value = false
  try {
    await apiFetch('/api/oauth/google/client', {
      method: 'PUT',
      body: JSON.stringify({
        client_id: clientId.value.trim(),
        client_secret: clientSecret.value.trim(),
        client_type: clientTypeChoice.value,
      }),
    })
    clientSaved.value = true
    clientId.value = ''
    clientSecret.value = ''
    showCredentialsForm.value = false
    await fetchGoogleStatus()
  } catch (err) {
    clientError.value = err?.body?.detail || err?.message || String(err)
  } finally {
    clientSaving.value = false
  }
}

// ── Web popup flow (client_type === 'web') ───────────────────────────
const connecting = ref(false)
const connectError = ref('')
const connectSuccess = ref(false)
let popupRef = null
let popupWatcher = null

function redirectUri() {
  return `${window.location.origin}/oauth/callback`
}

async function onMessage(event) {
  if (event.origin !== window.location.origin) return
  if (event?.data?.type !== 'jarvis:oauth:google') return
  const { code, state, error } = event.data
  if (popupRef) {
    try { popupRef.close() } catch (_) { /* may already be closed */ }
  }
  if (popupWatcher) clearInterval(popupWatcher)
  if (error) {
    connectError.value = `Google error: ${error}`
    connecting.value = false
    return
  }
  try {
    await apiFetch('/api/oauth/google/callback', {
      method: 'POST',
      body: JSON.stringify({ code, state }),
    })
    connectSuccess.value = true
    connectError.value = ''
    await fetchGoogleStatus()
  } catch (err) {
    connectError.value = err?.body?.detail || err?.message || String(err)
  } finally {
    connecting.value = false
  }
}

async function startConnect() {
  connectError.value = ''
  connectSuccess.value = false
  connecting.value = true
  try {
    const { url } = await apiFetch('/api/oauth/google/start', {
      method: 'POST',
      body: JSON.stringify({ redirect_uri: redirectUri() }),
    })
    const popup = window.open(url, 'jarvis-google-oauth', 'width=520,height=640')
    if (!popup) {
      connectError.value = 'Popup blocked — please allow popups for this site.'
      connecting.value = false
      return
    }
    popupRef = popup
    popupWatcher = setInterval(() => {
      if (popup.closed) {
        clearInterval(popupWatcher)
        if (connecting.value) {
          connecting.value = false
          if (!connectSuccess.value && !connectError.value) {
            connectError.value = 'OAuth window closed before completing.'
          }
        }
      }
    }, 500)
  } catch (err) {
    connectError.value = err?.body?.detail || err?.message || String(err)
    connecting.value = false
  }
}

async function disconnect() {
  if (
    !(await confirm({
      title: 'Disconnect Google',
      message: 'Gmail and Calendar tools will stop working until you reconnect.',
      confirmText: 'Disconnect',
      variant: 'danger',
    }))
  ) {
    return
  }
  try {
    await apiFetch('/api/oauth/google', { method: 'DELETE' })
    connectSuccess.value = false
    await fetchGoogleStatus()
  } catch (err) {
    connectError.value = err?.body?.detail || err?.message || String(err)
  }
}

// ── Desktop paste-URL flow (client_type === 'desktop') ───────────────
const consentUrlOpen = ref('')
const pastedUrl = ref('')
const pasteError = ref('')
const pasteLoading = ref(false)
let pastedState = ''

async function startDesktopConsent() {
  connectError.value = ''
  connectSuccess.value = false
  pasteError.value = ''
  consentUrlOpen.value = ''
  try {
    const resp = await apiFetch('/api/oauth/google/start', {
      method: 'POST',
      body: JSON.stringify({}),
    })
    consentUrlOpen.value = resp.url
    pastedState = resp.state
    window.open(resp.url, '_blank', 'noopener,noreferrer')
  } catch (err) {
    connectError.value = err?.body?.detail || err?.message || String(err)
  }
}

async function submitPastedUrl() {
  pasteError.value = ''
  pasteLoading.value = true
  try {
    const raw = pastedUrl.value.trim()
    if (!raw) throw new Error('Paste the URL from your browser address bar.')
    let params
    try {
      params = new URL(raw).searchParams
    } catch {
      const qs = raw.includes('?') ? raw.slice(raw.indexOf('?') + 1) : raw
      params = new URLSearchParams(qs)
    }
    const code = params.get('code')
    const state = params.get('state')
    if (!code || !state) {
      throw new Error('URL missing code or state — make sure you copied the full redirected URL.')
    }
    if (pastedState && state !== pastedState) {
      throw new Error('State mismatch — restart the flow and paste the URL from this session.')
    }
    await apiFetch('/api/oauth/google/callback', {
      method: 'POST',
      body: JSON.stringify({ code, state }),
    })
    connectSuccess.value = true
    pastedUrl.value = ''
    consentUrlOpen.value = ''
    pastedState = ''
    await fetchGoogleStatus()
  } catch (err) {
    pasteError.value = err?.body?.detail || err?.message || String(err)
  } finally {
    pasteLoading.value = false
  }
}

async function resetCredentials() {
  if (
    !(await confirm({
      title: 'Reset Google credentials',
      message:
        'Your saved Client ID & Secret (and any connected tokens) will be removed. You will need to paste new credentials and re-connect.',
      confirmText: 'Reset',
      variant: 'danger',
    }))
  ) {
    return
  }
  try {
    await apiFetch('/api/oauth/google', { method: 'DELETE' })
    await apiFetch('/api/oauth/google/client', { method: 'DELETE' })
    showCredentialsForm.value = false
    await fetchGoogleStatus()
  } catch (err) {
    statusError.value = err?.body?.detail || err?.message || String(err)
  }
}

const expiresText = computed(() => {
  const ts = googleStatus.value.expires_at
  if (!ts) return null
  const diffMs = ts * 1000 - Date.now()
  if (diffMs <= 0) return 'Expired — will refresh on next use.'
  const mins = Math.round(diffMs / 60000)
  if (mins < 60) return `Renews in ~${mins}m`
  return `Renews in ~${Math.round(mins / 60)}h`
})

onMounted(() => {
  window.addEventListener('message', onMessage)
  fetchGoogleStatus()
})

onBeforeUnmount(() => {
  window.removeEventListener('message', onMessage)
  if (popupWatcher) clearInterval(popupWatcher)
})
</script>

<template>
  <div class="google-oauth-card" data-testid="google-oauth-card">
    <header class="card-head">
      <div class="title-block">
        <h3>Google (Gmail + Calendar)</h3>
        <p class="muted">
          Connect once. Your agents will use the stored refresh token — no
          browser pop-up needed for subsequent calls.
        </p>
      </div>
      <span
        class="badge"
        :class="{ on: googleStatus.connected, off: !googleStatus.connected }"
        :data-testid="googleStatus.connected ? 'google-badge-connected' : 'google-badge-not-connected'"
      >{{ googleStatus.connected ? 'Connected' : 'Not Connected' }}</span>
    </header>

    <div v-if="loading" class="muted">Loading…</div>
    <div v-else-if="statusError" class="error-msg">{{ statusError }}</div>

    <!-- A) No credentials on file (or user clicked "Change credentials") -->
    <div
      v-if="!loading && (googleStatus.client_type === 'none' || showCredentialsForm)"
      class="client-form"
      data-testid="google-credentials-form"
    >
      <p class="muted">
        Paste Google OAuth credentials from the
        <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener">Google Cloud Console</a>.
        Pick the client type that matches what you created there.
      </p>
      <div class="client-type-row">
        <label class="radio-pill">
          <input type="radio" value="desktop" v-model="clientTypeChoice" />
          <span>
            <strong>Desktop app</strong>
            <em>No redirect URI to configure — easiest for a self-hosted Jarvis.</em>
          </span>
        </label>
        <label class="radio-pill">
          <input type="radio" value="web" v-model="clientTypeChoice" />
          <span>
            <strong>Web application</strong>
            <em>Add <code>{{ redirectUri() }}</code> to the client's authorised redirect URIs.</em>
          </span>
        </label>
      </div>
      <input class="text-input" placeholder="Client ID" v-model="clientId" data-testid="google-client-id" />
      <input class="text-input" placeholder="Client Secret" type="password" v-model="clientSecret" data-testid="google-client-secret" />
      <div class="action-row">
        <button
          type="button"
          class="btn primary"
          :disabled="clientSaving"
          data-testid="google-save-credentials"
          @click="saveClient"
        >{{ clientSaving ? 'Saving…' : 'Save credentials' }}</button>
        <button
          v-if="showCredentialsForm && googleStatus.client_type !== 'none'"
          type="button"
          class="btn"
          @click="showCredentialsForm = false"
        >Cancel</button>
      </div>
      <div v-if="clientError" class="error-msg" data-testid="google-client-error">{{ clientError }}</div>
      <div v-if="clientSaved" class="success-msg">Credentials saved.</div>
    </div>

    <!-- B) Desktop client saved & not yet connected -->
    <div
      v-if="!loading && googleStatus.client_type === 'desktop' && !googleStatus.connected && !showCredentialsForm"
      class="bundled-flow"
    >
      <ol class="steps">
        <li>Click <strong>Open Google consent</strong>. A new tab opens on Google.</li>
        <li>
          Pick your account and allow access. Google sends you to a page that shows
          <em>"This site can't be reached"</em> — <strong>that's expected</strong>. The URL
          (starts with <code>{{ googleStatus.desktop_redirect_uri || 'http://localhost' }}/…</code>) is what we need.
        </li>
        <li>Copy that full URL, paste below, then press <strong>Complete</strong>.</li>
      </ol>
      <div class="action-row">
        <button type="button" class="btn primary" @click="startDesktopConsent" data-testid="google-open-consent">
          {{ consentUrlOpen ? 'Re-open consent tab' : 'Open Google consent' }}
        </button>
      </div>
      <input
        class="text-input"
        placeholder="Paste URL (http://localhost/?code=…&state=…)"
        v-model="pastedUrl"
        data-testid="google-paste-url"
        @keydown.enter="submitPastedUrl"
      />
      <div class="action-row">
        <button
          type="button"
          class="btn primary"
          :disabled="pasteLoading || !pastedUrl.trim()"
          data-testid="google-complete-paste"
          @click="submitPastedUrl"
        >{{ pasteLoading ? 'Completing…' : 'Complete' }}</button>
      </div>
      <div v-if="pasteError" class="error-msg">{{ pasteError }}</div>
      <div class="muted" style="font-size: 12px;">
        <a href="#" @click.prevent="showCredentialsForm = true">Change credentials</a>
      </div>
    </div>

    <!-- C) Web client saved & not yet connected -->
    <div
      v-if="!loading && googleStatus.client_type === 'web' && !googleStatus.connected && !showCredentialsForm"
      class="connection-row"
    >
      <div class="meta muted">
        Using your Web-application OAuth client. Click "Connect Google" to open the consent popup.
      </div>
      <div class="controls">
        <button type="button" class="btn" @click="showCredentialsForm = true">Change credentials</button>
        <button
          type="button"
          class="btn primary"
          :disabled="connecting"
          data-testid="google-connect-btn"
          @click="startConnect"
        >{{ connecting ? 'Waiting for consent…' : 'Connect Google' }}</button>
      </div>
    </div>

    <!-- D) Connected -->
    <div v-if="!loading && googleStatus.connected" class="connection-row">
      <div class="meta meta-info">
        <div>Scopes: {{ googleStatus.scopes.length }} granted</div>
        <div v-if="expiresText" class="muted">{{ expiresText }}</div>
        <div v-if="!googleStatus.has_refresh_token" class="warn-msg">
          No refresh token on file — you'll need to reconnect when the access token expires.
        </div>
        <div class="muted" style="font-size: 12px;">
          Client type: <strong>{{ googleStatus.client_type }}</strong>
          <a href="#" style="margin-left: 8px;" @click.prevent="resetCredentials">Reset credentials</a>
        </div>
      </div>
      <div class="controls">
        <button type="button" class="btn ghost-danger" @click="disconnect">Disconnect</button>
      </div>
    </div>

    <!-- E) API enablement checklist -->
    <div
      v-if="!loading && googleStatus.client_type !== 'none' && googleStatus.required_apis && googleStatus.required_apis.length"
      class="api-enable-panel"
    >
      <div class="api-enable-header">
        <strong>Enable these APIs in Google Cloud Console</strong>
        <span class="muted" style="font-size: 12px;">
          One-time per project. OAuth consent alone is not enough — Gmail/Calendar
          calls fail with 403 until the API is enabled.
        </span>
      </div>
      <ul class="api-enable-list">
        <li v-for="api in googleStatus.required_apis" :key="api.api_id">
          <a :href="api.enable_url" target="_blank" rel="noopener noreferrer">
            {{ api.name }}
            <span class="muted" style="font-size: 11px;">({{ api.api_id }})</span>
            <span class="ext-arrow">↗</span>
          </a>
        </li>
      </ul>
      <div v-if="!googleStatus.project_number" class="muted" style="font-size: 12px;">
        Couldn't parse a project number from your client_id — links open without a
        pre-selected project, so pick the right one on the Console page.
      </div>
    </div>

    <div v-if="connectError" class="error-msg" data-testid="google-connect-error">{{ connectError }}</div>
    <div v-if="connectSuccess" class="success-msg" data-testid="google-connect-success">Google connected.</div>
  </div>
</template>

<style scoped>
.google-oauth-card { display: flex; flex-direction: column; gap: 14px; }
.card-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 14px;
}
.title-block h3 { margin: 0; font-size: 15px; font-weight: 600; color: var(--text-primary, #f0f2f5); }
.title-block p { margin: 4px 0 0; }

.muted { color: var(--text-nav, #8b8fa3); font-size: 13px; line-height: 1.5; }
.muted a { color: var(--accent-blue, #3b82f6); }

.badge {
  align-self: flex-start;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border: 1px solid transparent;
  white-space: nowrap;
}
.badge.on  { background: rgba(34, 197, 94, 0.12); color: #22c55e; border-color: rgba(34, 197, 94, 0.35); }
.badge.off { background: rgba(245, 158, 11, 0.10); color: #f59e0b; border-color: rgba(245, 158, 11, 0.30); }

.client-form, .bundled-flow, .connection-row, .api-enable-panel {
  display: flex; flex-direction: column; gap: 10px;
  padding: 14px;
  background: var(--bg-input, #0f172a);
  border: 1px solid var(--border-input, #1e2030);
  border-radius: 10px;
}
.connection-row {
  flex-direction: row; justify-content: space-between; align-items: flex-start; gap: 16px;
}
.connection-row .meta { flex: 1; }
.connection-row .controls { display: flex; gap: 8px; flex-shrink: 0; }
.meta-info { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--text-secondary, #c4c8d4); }

.client-type-row { display: flex; gap: 10px; flex-wrap: wrap; }
.radio-pill {
  flex: 1; min-width: 220px;
  display: flex; gap: 8px; align-items: flex-start;
  padding: 10px 12px;
  background: var(--bg-card, #111318);
  border: 1px solid var(--border-input, #1e2030);
  border-radius: 8px;
  cursor: pointer;
}
.radio-pill input { margin-top: 2px; accent-color: var(--accent-blue, #3b82f6); }
.radio-pill strong { display: block; font-size: 13px; color: var(--text-primary, #f0f2f5); }
.radio-pill em { font-style: normal; font-size: 12px; color: var(--text-nav, #8b8fa3); }
.radio-pill code { font-family: monospace; font-size: 11px; }

.text-input {
  background: var(--bg-card, #111318);
  border: 1px solid var(--border-input, #1e2030);
  border-radius: 8px;
  padding: 9px 12px;
  color: var(--text-primary, #f0f2f5);
  font-family: inherit;
  font-size: 13px;
}
.text-input:focus {
  outline: none;
  border-color: var(--accent-blue, #3b82f6);
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
}

.action-row { display: flex; gap: 8px; flex-wrap: wrap; }
.btn {
  padding: 8px 14px;
  font-family: inherit;
  font-size: 13px;
  font-weight: 600;
  border-radius: 8px;
  border: 1px solid var(--border-input, #2c3850);
  background: transparent;
  color: var(--text-secondary, #c4c8d4);
  cursor: pointer;
  transition: all 0.15s;
}
.btn:hover:not([disabled]) { color: var(--text-primary, #f0f2f5); background: rgba(255,255,255,0.04); }
.btn.primary {
  background: var(--accent-blue, #3b82f6);
  color: #ffffff;
  border-color: var(--accent-blue, #3b82f6);
}
.btn.primary:hover:not([disabled]) { background: #2f6cdc; border-color: #2f6cdc; }
.btn.ghost-danger { color: #ef4444; border-color: rgba(239, 68, 68, 0.35); }
.btn.ghost-danger:hover:not([disabled]) { background: rgba(239, 68, 68, 0.12); }
.btn[disabled] { opacity: 0.5; cursor: not-allowed; }

.steps { padding-left: 18px; margin: 0; color: var(--text-secondary, #c4c8d4); font-size: 13px; line-height: 1.6; }
.steps li + li { margin-top: 6px; }
.steps code { font-family: monospace; font-size: 12px; background: rgba(255,255,255,0.05); padding: 1px 4px; border-radius: 3px; }

.error-msg {
  padding: 8px 12px;
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: 8px;
  color: #ef4444;
  font-size: 13px;
}
.success-msg {
  padding: 8px 12px;
  background: rgba(34, 197, 94, 0.08);
  border: 1px solid rgba(34, 197, 94, 0.3);
  border-radius: 8px;
  color: #22c55e;
  font-size: 13px;
}
.warn-msg {
  padding: 6px 10px;
  background: rgba(245, 158, 11, 0.08);
  border: 1px solid rgba(245, 158, 11, 0.3);
  border-radius: 6px;
  color: #f59e0b;
  font-size: 12px;
}

.api-enable-header { display: flex; flex-direction: column; gap: 4px; }
.api-enable-list {
  list-style: none; padding: 0; margin: 0;
  display: flex; flex-direction: column; gap: 6px;
}
.api-enable-list a {
  display: inline-flex; align-items: center; gap: 6px;
  color: var(--accent-blue, #3b82f6);
  text-decoration: none;
  padding: 4px 0;
}
.api-enable-list a:hover { text-decoration: underline; }
.ext-arrow { font-size: 11px; opacity: 0.7; }
</style>
