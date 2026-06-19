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
import { useLang } from '../composables/useLang'

const { t } = useLang()
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
    clientError.value = t('googleOauth.errBothRequired')
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
    connectError.value = t('googleOauth.errGoogle', { error })
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
      connectError.value = t('googleOauth.errPopupBlocked')
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
            connectError.value = t('googleOauth.errWindowClosed')
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
      title: t('googleOauth.disconnectConfirmTitle'),
      message: t('googleOauth.disconnectConfirmMessage'),
      confirmText: t('googleOauth.disconnect'),
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
    if (!raw) throw new Error(t('googleOauth.errPasteUrl'))
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
      throw new Error(t('googleOauth.errUrlMissing'))
    }
    if (pastedState && state !== pastedState) {
      throw new Error(t('googleOauth.errStateMismatch'))
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
      title: t('googleOauth.resetConfirmTitle'),
      message: t('googleOauth.resetConfirmMessage'),
      confirmText: t('googleOauth.reset'),
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
  if (diffMs <= 0) return t('googleOauth.expired')
  const mins = Math.round(diffMs / 60000)
  if (mins < 60) return t('googleOauth.renewsMinutes', { n: mins })
  return t('googleOauth.renewsHours', { n: Math.round(mins / 60) })
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
        <h3>{{ t('googleOauth.heading') }}</h3>
        <p class="muted">
          {{ t('googleOauth.intro') }}
        </p>
      </div>
      <span
        class="badge"
        :class="{ on: googleStatus.connected, off: !googleStatus.connected }"
        :data-testid="googleStatus.connected ? 'google-badge-connected' : 'google-badge-not-connected'"
      >{{ googleStatus.connected ? t('googleOauth.connected') : t('googleOauth.notConnected') }}</span>
    </header>

    <div v-if="loading" class="muted">{{ t('common.loading') }}</div>
    <div v-else-if="statusError" class="error-msg">{{ statusError }}</div>

    <!-- A) No credentials on file (or user clicked "Change credentials") -->
    <div
      v-if="!loading && (googleStatus.client_type === 'none' || showCredentialsForm)"
      class="client-form"
      data-testid="google-credentials-form"
    >
      <p class="muted">
        {{ t('googleOauth.pasteCredsPre') }}
        <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener">{{ t('googleOauth.cloudConsole') }}</a>.
        {{ t('googleOauth.pasteCredsPost') }}
      </p>
      <div class="client-type-row">
        <label class="radio-pill">
          <input type="radio" value="desktop" v-model="clientTypeChoice" />
          <span>
            <strong>{{ t('googleOauth.desktopApp') }}</strong>
            <em>{{ t('googleOauth.desktopAppHint') }}</em>
          </span>
        </label>
        <label class="radio-pill">
          <input type="radio" value="web" v-model="clientTypeChoice" />
          <span>
            <strong>{{ t('googleOauth.webApp') }}</strong>
            <em>{{ t('googleOauth.webAppHintPre') }} <code>{{ redirectUri() }}</code> {{ t('googleOauth.webAppHintPost') }}</em>
          </span>
        </label>
      </div>
      <input class="text-input" :placeholder="t('googleOauth.clientIdPlaceholder')" v-model="clientId" data-testid="google-client-id" />
      <input class="text-input" :placeholder="t('googleOauth.clientSecretPlaceholder')" type="password" v-model="clientSecret" data-testid="google-client-secret" />
      <div class="action-row">
        <button
          type="button"
          class="btn primary"
          :disabled="clientSaving"
          data-testid="google-save-credentials"
          @click="saveClient"
        >{{ clientSaving ? t('googleOauth.saving') : t('googleOauth.saveCredentials') }}</button>
        <button
          v-if="showCredentialsForm && googleStatus.client_type !== 'none'"
          type="button"
          class="btn"
          @click="showCredentialsForm = false"
        >{{ t('common.cancel') }}</button>
      </div>
      <div v-if="clientError" class="error-msg" data-testid="google-client-error">{{ clientError }}</div>
      <div v-if="clientSaved" class="success-msg">{{ t('googleOauth.credentialsSaved') }}</div>
    </div>

    <!-- B) Desktop client saved & not yet connected -->
    <div
      v-if="!loading && googleStatus.client_type === 'desktop' && !googleStatus.connected && !showCredentialsForm"
      class="bundled-flow"
    >
      <ol class="steps">
        <li>{{ t('googleOauth.step1Pre') }} <strong>{{ t('googleOauth.openConsent') }}</strong>. {{ t('googleOauth.step1Post') }}</li>
        <li>
          {{ t('googleOauth.step2Pre') }}
          <em>{{ t('googleOauth.step2Quote') }}</em> — <strong>{{ t('googleOauth.step2Expected') }}</strong>. {{ t('googleOauth.step2UrlPre') }}
          <code>{{ googleStatus.desktop_redirect_uri || 'http://localhost' }}/…</code>{{ t('googleOauth.step2UrlPost') }}
        </li>
        <li>{{ t('googleOauth.step3Pre') }} <strong>{{ t('googleOauth.complete') }}</strong>.</li>
      </ol>
      <div class="action-row">
        <button type="button" class="btn primary" @click="startDesktopConsent" data-testid="google-open-consent">
          {{ consentUrlOpen ? t('googleOauth.reopenConsent') : t('googleOauth.openConsent') }}
        </button>
      </div>
      <input
        class="text-input"
        :placeholder="t('googleOauth.pasteUrlPlaceholder')"
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
        >{{ pasteLoading ? t('googleOauth.completing') : t('googleOauth.complete') }}</button>
      </div>
      <div v-if="pasteError" class="error-msg">{{ pasteError }}</div>
      <div class="muted" style="font-size: 12px;">
        <a href="#" @click.prevent="showCredentialsForm = true">{{ t('googleOauth.changeCredentials') }}</a>
      </div>
    </div>

    <!-- C) Web client saved & not yet connected -->
    <div
      v-if="!loading && googleStatus.client_type === 'web' && !googleStatus.connected && !showCredentialsForm"
      class="connection-row"
    >
      <div class="meta muted">
        {{ t('googleOauth.webClientHint') }}
      </div>
      <div class="controls">
        <button type="button" class="btn" @click="showCredentialsForm = true">{{ t('googleOauth.changeCredentials') }}</button>
        <button
          type="button"
          class="btn primary"
          :disabled="connecting"
          data-testid="google-connect-btn"
          @click="startConnect"
        >{{ connecting ? t('googleOauth.waitingConsent') : t('googleOauth.connectGoogle') }}</button>
      </div>
    </div>

    <!-- D) Connected -->
    <div v-if="!loading && googleStatus.connected" class="connection-row">
      <div class="meta meta-info">
        <div>{{ t('googleOauth.scopesGranted', { n: googleStatus.scopes.length }) }}</div>
        <div v-if="expiresText" class="muted">{{ expiresText }}</div>
        <div v-if="!googleStatus.has_refresh_token" class="warn-msg">
          {{ t('googleOauth.noRefreshToken') }}
        </div>
        <div class="muted" style="font-size: 12px;">
          {{ t('googleOauth.clientTypeLabel') }} <strong>{{ googleStatus.client_type }}</strong>
          <a href="#" style="margin-left: 8px;" @click.prevent="resetCredentials">{{ t('googleOauth.resetCredentials') }}</a>
        </div>
      </div>
      <div class="controls">
        <button type="button" class="btn ghost-danger" @click="disconnect">{{ t('googleOauth.disconnect') }}</button>
      </div>
    </div>

    <!-- E) API enablement checklist -->
    <div
      v-if="!loading && googleStatus.client_type !== 'none' && googleStatus.required_apis && googleStatus.required_apis.length"
      class="api-enable-panel"
    >
      <div class="api-enable-header">
        <strong>{{ t('googleOauth.enableApisHeading') }}</strong>
        <span class="muted" style="font-size: 12px;">
          {{ t('googleOauth.enableApisHint') }}
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
        {{ t('googleOauth.noProjectNumber') }}
      </div>
    </div>

    <div v-if="connectError" class="error-msg" data-testid="google-connect-error">{{ connectError }}</div>
    <div v-if="connectSuccess" class="success-msg" data-testid="google-connect-success">{{ t('googleOauth.googleConnected') }}</div>
  </div>
</template>

<style scoped>
.google-oauth-card { display: flex; flex-direction: column; gap: 14px; }
.card-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 14px;
}
.title-block h3 { margin: 0; font-size: 15px; font-weight: 600; color: var(--text); }
.title-block p { margin: 4px 0 0; }

.muted { color: var(--text-dim); font-size: 13px; line-height: 1.5; }
.muted a { color: var(--accent); }

.badge {
  align-self: flex-start;
  padding: 4px 10px;
  border-radius: 999px;
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  border: 1px solid transparent;
  white-space: nowrap;
}
.badge.on  { background: var(--success-bg); color: var(--success); border-color: rgba(16, 185, 129, 0.35); }
.badge.off { background: var(--warning-bg); color: var(--warning); border-color: rgba(245, 158, 11, 0.30); }

.client-form, .bundled-flow, .connection-row, .api-enable-panel {
  display: flex; flex-direction: column; gap: 10px;
  padding: 14px;
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
}
.connection-row {
  flex-direction: row; justify-content: space-between; align-items: flex-start; gap: 16px;
}
.connection-row .meta { flex: 1; }
.connection-row .controls { display: flex; gap: 8px; flex-shrink: 0; }
.meta-info { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--text); }

.client-type-row { display: flex; gap: 10px; flex-wrap: wrap; }
.radio-pill {
  flex: 1; min-width: 220px;
  display: flex; gap: 8px; align-items: flex-start;
  padding: 10px 12px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.radio-pill:hover { border-color: var(--primary); }
.radio-pill input { margin-top: 2px; accent-color: var(--primary); }
.radio-pill strong { display: block; font-size: 13px; color: var(--text); }
.radio-pill em { font-style: normal; font-size: 12px; color: var(--text-muted); }
.radio-pill code { font-family: var(--font-mono); font-size: 11px; }

.text-input {
  background: var(--bg-4);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  padding: 9px 12px;
  color: var(--text);
  font-family: inherit;
  font-size: 13px;
}
.text-input:focus {
  outline: none;
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-bg-strong);
}

.action-row { display: flex; gap: 8px; flex-wrap: wrap; }
.btn {
  padding: 8px 14px;
  font-family: inherit;
  font-size: 13px;
  font-weight: 500;
  border-radius: var(--r-md);
  border: 1px solid var(--border-strong);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  transition: all 0.15s;
}
.btn:hover:not([disabled]) { color: var(--text); background: rgba(255,255,255,0.04); }
.btn.primary {
  background: var(--primary);
  color: #ffffff;
  border-color: var(--primary);
}
.btn.primary:hover:not([disabled]) { background: var(--primary-active); border-color: var(--primary-active); }
.btn.ghost-danger { color: var(--danger); border-color: rgba(239, 68, 68, 0.35); }
.btn.ghost-danger:hover:not([disabled]) { background: var(--danger-bg); }
.btn[disabled] { opacity: 0.5; cursor: not-allowed; }

.steps { padding-left: 18px; margin: 0; color: var(--text-dim); font-size: 13px; line-height: 1.6; }
.steps li + li { margin-top: 6px; }
.steps code { font-family: var(--font-mono); font-size: 12px; background: rgba(255,255,255,0.05); padding: 1px 4px; border-radius: 3px; color: var(--accent); }

.error-msg {
  padding: 8px 12px;
  background: var(--danger-bg);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: var(--r-md);
  color: var(--danger);
  font-size: 13px;
}
.success-msg {
  padding: 8px 12px;
  background: var(--success-bg);
  border: 1px solid rgba(16, 185, 129, 0.3);
  border-radius: var(--r-md);
  color: var(--success);
  font-size: 13px;
}
.warn-msg {
  padding: 6px 10px;
  background: var(--warning-bg);
  border: 1px solid rgba(245, 158, 11, 0.3);
  border-radius: var(--r-sm);
  color: var(--warning);
  font-size: 12px;
}

.api-enable-header { display: flex; flex-direction: column; gap: 4px; }
.api-enable-list {
  list-style: none; padding: 0; margin: 0;
  display: flex; flex-direction: column; gap: 6px;
}
.api-enable-list a {
  display: inline-flex; align-items: center; gap: 6px;
  color: var(--accent);
  text-decoration: none;
  padding: 4px 0;
}
.api-enable-list a:hover { text-decoration: underline; }
.ext-arrow { font-size: 11px; opacity: 0.7; }
</style>
