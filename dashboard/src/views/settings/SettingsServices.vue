<script setup>
/**
 * Settings → Services.
 *
 * First-class support for Google OAuth (Gmail + Calendar).  The web flow
 * opens a popup to Google's consent screen; the popup bounces back to
 * /oauth/callback which posts code+state to us via postMessage.  We then
 * exchange the code server-side.
 */
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { apiFetch } from '../../api'
import { useConfirm } from '../../composables/useConfirm'

const { confirm } = useConfirm()

// ─── Google OAuth ──────────────────────────────────────────────────────

const googleStatus = ref({
  client_configured: false,
  client_type: 'none', // 'desktop' | 'web' | 'none'
  desktop_redirect_uri: null,
  connected: false,
  scopes: [],
  expires_at: null,
  has_refresh_token: false,
  project_number: null,
  // [{ name, api_id, enable_url }] — pre-filled with project_number when the
  // client_id has one. UI renders these as the "enable these APIs" checklist
  // that shows up after OAuth succeeds.
  required_apis: [],
})
const loading = ref(true)
const statusError = ref('')
// When credentials are on file but the user wants to rotate them, let them
// opt back into the form without having to disconnect tokens first.
const showCredentialsForm = ref(false)

async function fetchGoogleStatus() {
  loading.value = true
  statusError.value = ''
  try {
    googleStatus.value = await apiFetch('/api/oauth/google/status')
  } catch (err) {
    statusError.value = err?.message || String(err)
  } finally {
    loading.value = false
  }
}

// ── Client credentials editor ────────────────────────────────────────
const clientId = ref('')
const clientSecret = ref('')
// Default to "desktop" — it's the no-Cloud-Console-redirect-URI path,
// which is the simplest UX for a single-user self-hosted install.
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

// ── Consent flow ─────────────────────────────────────────────────────
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
    try { popupRef.close() } catch (_) { /* may be closed already */ }
  }
  clearInterval(popupWatcher)
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
    // If the user closes the popup without completing, stop spinning.
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

// ─── Desktop (paste-URL) flow ────────────────────────────────────────────
// Google redirects the browser to DESKTOP_LOOPBACK_REDIRECT_URI (a loopback
// address nothing is listening on) after consent. The browser shows "site
// can't be reached" but the URL in the address bar carries code+state. The
// user copies it here; we parse and POST to the same /callback endpoint the
// popup flow uses.
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
    // Accept either a full URL or a bare "?code=...&state=..." fragment.
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

// ─── Roborock (Vacuum) ───────────────────────────────────────────────────
// Stored under config_service category "service.roborock". The setup wizard
// persists both fields as secrets, so on load we only get a has_value flag
// per key — never the plaintext. Save goes through /api/settings/bulk so
// username + password land atomically; disconnect sends null values (which
// config_service treats as delete). runtime_config listens for those change
// events and re-exports the values into os.environ, which iot_server.py reads
// via os.getenv on each tool call.

const ROBOROCK_CATEGORY = 'service.roborock'
const ROBOROCK_USERNAME_KEY = 'ROBOROCK_USERNAME'
const ROBOROCK_PASSWORD_KEY = 'ROBOROCK_PASSWORD'

const roborockStatus = ref({
  username_set: false,
  password_set: false,
})
const roborockLoading = ref(true)
const roborockStatusError = ref('')
// "editing" opens the form even when credentials are on file, so the user
// can rotate without disconnecting first.
const roborockEditing = ref(false)
const roborockUsername = ref('')
const roborockPassword = ref('')
const roborockSaving = ref(false)
const roborockSaved = ref(false)
const roborockError = ref('')

const roborockConnected = computed(
  () => roborockStatus.value.username_set && roborockStatus.value.password_set,
)

async function fetchRoborockStatus() {
  roborockLoading.value = true
  roborockStatusError.value = ''
  try {
    const resp = await apiFetch(`/api/settings/${ROBOROCK_CATEGORY}`)
    const items = resp?.items || []
    const hasKey = (k) => items.some((i) => i.key === k && i.has_value)
    roborockStatus.value = {
      username_set: hasKey(ROBOROCK_USERNAME_KEY),
      password_set: hasKey(ROBOROCK_PASSWORD_KEY),
    }
  } catch (err) {
    roborockStatusError.value = err?.body?.detail || err?.message || String(err)
  } finally {
    roborockLoading.value = false
  }
}

async function saveRoborock() {
  const username = roborockUsername.value.trim()
  const password = roborockPassword.value.trim()
  if (!username || !password) {
    roborockError.value = 'Both email and password are required.'
    return
  }
  roborockSaving.value = true
  roborockError.value = ''
  roborockSaved.value = false
  try {
    await apiFetch('/api/settings/bulk', {
      method: 'POST',
      body: JSON.stringify({
        items: [
          { category: ROBOROCK_CATEGORY, key: ROBOROCK_USERNAME_KEY, value: username, is_secret: true },
          { category: ROBOROCK_CATEGORY, key: ROBOROCK_PASSWORD_KEY, value: password, is_secret: true },
        ],
      }),
    })
    roborockSaved.value = true
    roborockUsername.value = ''
    roborockPassword.value = ''
    roborockEditing.value = false
    await fetchRoborockStatus()
  } catch (err) {
    roborockError.value = err?.body?.detail || err?.message || String(err)
  } finally {
    roborockSaving.value = false
  }
}

async function disconnectRoborock() {
  if (
    !(await confirm({
      title: 'Disconnect Roborock',
      message:
        'Vacuum control tools will stop working until you re-enter your Roborock credentials.',
      confirmText: 'Disconnect',
      variant: 'danger',
    }))
  ) {
    return
  }
  try {
    // Null values on /bulk delete the rows; runtime_config then unsets the
    // env vars so the next iot_server call fails cleanly with "must be set".
    await apiFetch('/api/settings/bulk', {
      method: 'POST',
      body: JSON.stringify({
        items: [
          { category: ROBOROCK_CATEGORY, key: ROBOROCK_USERNAME_KEY, value: null, is_secret: true },
          { category: ROBOROCK_CATEGORY, key: ROBOROCK_PASSWORD_KEY, value: null, is_secret: true },
        ],
      }),
    })
    roborockSaved.value = false
    roborockEditing.value = false
    await fetchRoborockStatus()
  } catch (err) {
    roborockError.value = err?.body?.detail || err?.message || String(err)
  }
}

function cancelRoborockEdit() {
  roborockEditing.value = false
  roborockUsername.value = ''
  roborockPassword.value = ''
  roborockError.value = ''
}

// ─── GitHub (dev agent git access) ──────────────────────────────────────
// Stored under config_service category "service.github". Unlike Roborock,
// the backend listener routes service.github.* changes to
// git_credential_sync which writes host-side files (git-credentials +
// gitconfig) bind-mounted into the container, and also keeps the github
// MCP section of fastagent.secrets.yaml in lockstep. The UI only sees
// has_value flags — the token is never echoed back.

const GITHUB_CATEGORY = 'service.github'
const GITHUB_TOKEN_KEY = 'personal_access_token'
const GITHUB_USER_NAME_KEY = 'user_name'
const GITHUB_USER_EMAIL_KEY = 'user_email'

const githubStatus = ref({
  token_set: false,
  user_name_set: false,
  user_email_set: false,
})
const githubLoading = ref(true)
const githubStatusError = ref('')
const githubEditing = ref(false)
const githubToken = ref('')
const githubUserName = ref('')
const githubUserEmail = ref('')
const githubSaving = ref(false)
const githubSaved = ref(false)
const githubError = ref('')

const githubConnected = computed(
  () =>
    githubStatus.value.token_set &&
    githubStatus.value.user_name_set &&
    githubStatus.value.user_email_set,
)

async function fetchGithubStatus() {
  githubLoading.value = true
  githubStatusError.value = ''
  try {
    const resp = await apiFetch(`/api/settings/${GITHUB_CATEGORY}`)
    const items = resp?.items || []
    const hasKey = (k) => items.some((i) => i.key === k && i.has_value)
    githubStatus.value = {
      token_set: hasKey(GITHUB_TOKEN_KEY),
      user_name_set: hasKey(GITHUB_USER_NAME_KEY),
      user_email_set: hasKey(GITHUB_USER_EMAIL_KEY),
    }
  } catch (err) {
    githubStatusError.value = err?.body?.detail || err?.message || String(err)
  } finally {
    githubLoading.value = false
  }
}

async function saveGithub() {
  const token = githubToken.value.trim()
  const name = githubUserName.value.trim()
  const email = githubUserEmail.value.trim()
  if (!token || !name || !email) {
    githubError.value = 'Token, git user name, and email are all required.'
    return
  }
  githubSaving.value = true
  githubError.value = ''
  githubSaved.value = false
  try {
    await apiFetch('/api/settings/bulk', {
      method: 'POST',
      body: JSON.stringify({
        items: [
          { category: GITHUB_CATEGORY, key: GITHUB_TOKEN_KEY, value: token, is_secret: true },
          { category: GITHUB_CATEGORY, key: GITHUB_USER_NAME_KEY, value: name, is_secret: false },
          { category: GITHUB_CATEGORY, key: GITHUB_USER_EMAIL_KEY, value: email, is_secret: false },
        ],
      }),
    })
    githubSaved.value = true
    githubToken.value = ''
    githubUserName.value = ''
    githubUserEmail.value = ''
    githubEditing.value = false
    await fetchGithubStatus()
  } catch (err) {
    githubError.value = err?.body?.detail || err?.message || String(err)
  } finally {
    githubSaving.value = false
  }
}

async function disconnectGithub() {
  if (
    !(await confirm({
      title: 'Disconnect GitHub',
      message:
        'Dev agents will stop being able to clone/push/pull private repos until a new token is configured.',
      confirmText: 'Disconnect',
      variant: 'danger',
    }))
  ) {
    return
  }
  try {
    await apiFetch('/api/settings/bulk', {
      method: 'POST',
      body: JSON.stringify({
        items: [
          { category: GITHUB_CATEGORY, key: GITHUB_TOKEN_KEY, value: null, is_secret: true },
          { category: GITHUB_CATEGORY, key: GITHUB_USER_NAME_KEY, value: null, is_secret: false },
          { category: GITHUB_CATEGORY, key: GITHUB_USER_EMAIL_KEY, value: null, is_secret: false },
        ],
      }),
    })
    githubSaved.value = false
    githubEditing.value = false
    await fetchGithubStatus()
  } catch (err) {
    githubError.value = err?.body?.detail || err?.message || String(err)
  }
}

function cancelGithubEdit() {
  githubEditing.value = false
  githubToken.value = ''
  githubUserName.value = ''
  githubUserEmail.value = ''
  githubError.value = ''
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
  fetchRoborockStatus()
  fetchGithubStatus()
})

onBeforeUnmount(() => {
  window.removeEventListener('message', onMessage)
  if (popupWatcher) clearInterval(popupWatcher)
})
</script>

<template>
  <div class="svc-sections">
    <!-- Google OAuth -->
    <section class="panel-card">
      <header>
        <div class="icon-circle">
          <svg width="18" height="18" viewBox="0 0 48 48">
            <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3c-1.6 4.6-6 8-11.3 8c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3l5.7-5.7C34.5 6.1 29.5 4 24 4C13 4 4 13 4 24s9 20 20 20s20-9 20-20c0-1.3-.1-2.3-.4-3.5"/>
            <path fill="#FF3D00" d="m6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3l5.7-5.7C34.5 6.1 29.5 4 24 4C16.3 4 9.6 8.4 6.3 14.7"/>
            <path fill="#4CAF50" d="M24 44c5.4 0 10.3-2.1 14-5.5l-6.5-5.3c-2 1.5-4.6 2.5-7.5 2.5c-5.3 0-9.7-3.4-11.3-8l-6.6 5.1C9.5 39.6 16.2 44 24 44"/>
            <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.3-2.3 4.3-4.3 5.7l6.5 5.3C41.4 35.5 44 30.1 44 24c0-1.3-.1-2.3-.4-3.5"/>
          </svg>
        </div>
        <div>
          <h2>Google (Gmail + Calendar)</h2>
          <p>Connect once. Your agents will use the stored refresh token — no browser pop-up needed for subsequent calls.</p>
        </div>
        <span
          class="badge"
          :class="{ on: googleStatus.connected, off: !googleStatus.connected }"
        >{{ googleStatus.connected ? 'Connected' : 'Not Connected' }}</span>
      </header>

      <div v-if="loading" class="muted">Loading…</div>
      <div v-else-if="statusError" class="error-msg">{{ statusError }}</div>

      <!-- A) No credentials on file (or user clicked "Change credentials") — show form. -->
      <div
        v-if="!loading && (googleStatus.client_type === 'none' || showCredentialsForm)"
        class="client-form"
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
        <input
          class="text-input"
          placeholder="Client ID"
          v-model="clientId"
        />
        <input
          class="text-input"
          placeholder="Client Secret"
          type="password"
          v-model="clientSecret"
        />
        <div class="action-row">
          <button
            type="button"
            class="btn primary"
            :disabled="clientSaving"
            @click="saveClient"
          >{{ clientSaving ? 'Saving…' : 'Save credentials' }}</button>
          <button
            v-if="showCredentialsForm && googleStatus.client_type !== 'none'"
            type="button"
            class="btn"
            @click="showCredentialsForm = false"
          >Cancel</button>
        </div>
        <div v-if="clientError" class="error-msg">{{ clientError }}</div>
        <div v-if="clientSaved" class="success-msg">Credentials saved.</div>
      </div>

      <!-- B) Desktop client saved & not yet connected — paste-URL flow. -->
      <div
        v-if="!loading && googleStatus.client_type === 'desktop' && !googleStatus.connected && !showCredentialsForm"
        class="bundled-flow"
      >
        <ol class="steps">
          <li>
            Click <strong>Open Google consent</strong>. A new tab opens on Google.
          </li>
          <li>
            Pick your account and allow access. Google will send you to a page that shows
            <em>"This site can't be reached"</em> — <strong>that's expected</strong>. The URL
            in the address bar (starts with <code>{{ googleStatus.desktop_redirect_uri || 'http://localhost' }}/…</code>) is what we need.
          </li>
          <li>
            Copy that full URL and paste it below, then press <strong>Complete</strong>.
          </li>
        </ol>
        <div class="action-row">
          <button
            type="button"
            class="btn primary"
            @click="startDesktopConsent"
          >{{ consentUrlOpen ? 'Re-open consent tab' : 'Open Google consent' }}</button>
        </div>
        <input
          class="text-input"
          placeholder="Paste URL (http://localhost/?code=…&state=…)"
          v-model="pastedUrl"
          @keydown.enter="submitPastedUrl"
        />
        <div class="action-row">
          <button
            type="button"
            class="btn primary"
            :disabled="pasteLoading || !pastedUrl.trim()"
            @click="submitPastedUrl"
          >{{ pasteLoading ? 'Completing…' : 'Complete' }}</button>
        </div>
        <div v-if="pasteError" class="error-msg">{{ pasteError }}</div>
        <div class="muted" style="font-size: 12px;">
          <a href="#" @click.prevent="showCredentialsForm = true">Change credentials</a>
        </div>
      </div>

      <!-- C) Web client saved & not yet connected — popup flow. -->
      <div
        v-if="!loading && googleStatus.client_type === 'web' && !googleStatus.connected && !showCredentialsForm"
        class="connection-row"
      >
        <div class="meta muted">
          Using your Web-application OAuth client. Click "Connect Google" to open the consent popup.
        </div>
        <div class="controls">
          <button
            type="button"
            class="btn"
            @click="showCredentialsForm = true"
          >Change credentials</button>
          <button
            type="button"
            class="btn primary"
            :disabled="connecting"
            @click="startConnect"
          >{{ connecting ? 'Waiting for consent…' : 'Connect Google' }}</button>
        </div>
      </div>

      <!-- D) Connected — show details + disconnect. -->
      <div
        v-if="!loading && googleStatus.connected"
        class="connection-row"
      >
        <div class="meta meta-info">
          <div>Scopes: {{ googleStatus.scopes.length }} granted</div>
          <div v-if="expiresText" class="muted">{{ expiresText }}</div>
          <div v-if="!googleStatus.has_refresh_token" class="warn-msg">
            No refresh token on file — you'll need to reconnect when the access token expires.
          </div>
          <div class="muted" style="font-size: 12px;">
            Client type: <strong>{{ googleStatus.client_type }}</strong>
            <a
              href="#"
              style="margin-left: 8px;"
              @click.prevent="resetCredentials"
            >Reset credentials</a>
          </div>
        </div>
        <div class="controls">
          <button type="button" class="btn ghost-danger" @click="disconnect">Disconnect</button>
        </div>
      </div>

      <!-- E) API enablement checklist (shown whenever a client is on file).
           OAuth consent alone doesn't activate Gmail/Calendar APIs on the
           Cloud project — Google needs a per-API "Enable" click. Surface
           the deep-links so the user doesn't hit 403 accessNotConfigured
           at runtime. Only renders once credentials are saved (otherwise
           there's nothing actionable to do). -->
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

      <div v-if="connectError" class="error-msg">{{ connectError }}</div>
      <div v-if="connectSuccess" class="success-msg">Google connected.</div>
    </section>

    <!-- Roborock (Vacuum) -->
    <section class="panel-card">
      <header>
        <div class="icon-circle">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="9" />
            <circle cx="12" cy="12" r="3" />
            <path d="M12 3v3" />
            <path d="M12 18v3" />
            <path d="M3 12h3" />
            <path d="M18 12h3" />
          </svg>
        </div>
        <div>
          <h2>Roborock (Vacuum)</h2>
          <p>Control Roborock vacuums through the Roborock cloud. Uses the same email + password as the Roborock mobile app.</p>
        </div>
        <span
          class="badge"
          :class="{ on: roborockConnected, off: !roborockConnected }"
        >{{ roborockConnected ? 'Configured' : 'Not Configured' }}</span>
      </header>

      <div v-if="roborockLoading" class="muted">Loading…</div>
      <div v-else-if="roborockStatusError" class="error-msg">{{ roborockStatusError }}</div>

      <!-- Form shows when nothing on file, or the user clicked "Update credentials". -->
      <div
        v-if="!roborockLoading && (!roborockConnected || roborockEditing)"
        class="client-form"
      >
        <p class="muted">
          Credentials are encrypted at rest with the master key. 2FA codes triggered by Roborock
          are handled automatically through the Gmail integration — no extra setup needed here.
        </p>
        <input
          class="text-input"
          placeholder="Account email"
          autocomplete="off"
          v-model="roborockUsername"
        />
        <input
          class="text-input"
          placeholder="Account password"
          type="password"
          autocomplete="new-password"
          v-model="roborockPassword"
        />
        <div class="action-row">
          <button
            type="button"
            class="btn primary"
            :disabled="roborockSaving"
            @click="saveRoborock"
          >{{ roborockSaving ? 'Saving…' : 'Save credentials' }}</button>
          <button
            v-if="roborockEditing"
            type="button"
            class="btn"
            :disabled="roborockSaving"
            @click="cancelRoborockEdit"
          >Cancel</button>
        </div>
        <div v-if="roborockError" class="error-msg">{{ roborockError }}</div>
        <div v-if="roborockSaved" class="success-msg">Roborock credentials saved.</div>
      </div>

      <!-- Connected — show state + rotate/disconnect controls. -->
      <div
        v-if="!roborockLoading && roborockConnected && !roborockEditing"
        class="connection-row"
      >
        <div class="meta meta-info">
          <div>Email + password on file.</div>
          <div class="muted" style="font-size: 12px;">
            Values are encrypted with the master key; the UI never echoes them back.
          </div>
        </div>
        <div class="controls">
          <button
            type="button"
            class="btn"
            @click="roborockEditing = true"
          >Update credentials</button>
          <button
            type="button"
            class="btn ghost-danger"
            @click="disconnectRoborock"
          >Disconnect</button>
        </div>
      </div>
    </section>

    <!-- GitHub (dev agent git access) -->
    <section class="panel-card">
      <header>
        <div class="icon-circle">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M12 .3a12 12 0 0 0-3.8 23.4c.6.1.8-.3.8-.6v-2c-3.3.7-4-1.6-4-1.6-.5-1.4-1.3-1.8-1.3-1.8-1.1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1.1 1.8 2.8 1.3 3.5 1 .1-.8.4-1.3.8-1.6-2.7-.3-5.5-1.3-5.5-6a4.7 4.7 0 0 1 1.3-3.2c-.2-.4-.6-1.6.1-3.2 0 0 1-.3 3.3 1.2a11.5 11.5 0 0 1 6 0C17.3 4.7 18.3 5 18.3 5c.7 1.6.2 2.8.1 3.2.8.9 1.3 2 1.3 3.2 0 4.6-2.8 5.7-5.5 6 .4.4.8 1.1.8 2.3v3.4c0 .3.2.7.8.6A12 12 0 0 0 12 .3"/>
          </svg>
        </div>
        <div>
          <h2>GitHub (Dev Agent Git Access)</h2>
          <p>Personal access token + git identity used by dev agents to clone/push/pull repos and by the GitHub MCP. The token is written to a bind-mounted credential file — never exposed via container env.</p>
        </div>
        <span
          class="badge"
          :class="{ on: githubConnected, off: !githubConnected }"
        >{{ githubConnected ? 'Configured' : 'Not Configured' }}</span>
      </header>

      <div v-if="githubLoading" class="muted">Loading…</div>
      <div v-else-if="githubStatusError" class="error-msg">{{ githubStatusError }}</div>

      <!-- Form shows when nothing on file, or the user clicked "Update credentials". -->
      <div
        v-if="!githubLoading && (!githubConnected || githubEditing)"
        class="client-form"
      >
        <p class="muted">
          Create a token at
          <a href="https://github.com/settings/tokens" target="_blank" rel="noopener">github.com/settings/tokens</a>
          with the <code>repo</code> scope. The token is encrypted at rest with the master key.
        </p>
        <input
          class="text-input"
          placeholder="Personal Access Token (ghp_…)"
          type="password"
          autocomplete="new-password"
          v-model="githubToken"
        />
        <input
          class="text-input"
          placeholder="Git user name (for commits)"
          autocomplete="off"
          v-model="githubUserName"
        />
        <input
          class="text-input"
          placeholder="Git user email (for commits)"
          autocomplete="off"
          v-model="githubUserEmail"
        />
        <div class="action-row">
          <button
            type="button"
            class="btn primary"
            :disabled="githubSaving"
            @click="saveGithub"
          >{{ githubSaving ? 'Saving…' : 'Save credentials' }}</button>
          <button
            v-if="githubEditing"
            type="button"
            class="btn"
            :disabled="githubSaving"
            @click="cancelGithubEdit"
          >Cancel</button>
        </div>
        <div v-if="githubError" class="error-msg">{{ githubError }}</div>
        <div v-if="githubSaved" class="success-msg">GitHub credentials saved.</div>
      </div>

      <!-- Configured — show state + rotate/disconnect controls. -->
      <div
        v-if="!githubLoading && githubConnected && !githubEditing"
        class="connection-row"
      >
        <div class="meta meta-info">
          <div>Token + git identity on file.</div>
          <div class="muted" style="font-size: 12px;">
            Token is bind-mounted to the container as a read-only credential file; the UI never echoes it back.
          </div>
        </div>
        <div class="controls">
          <button
            type="button"
            class="btn"
            @click="githubEditing = true"
          >Update credentials</button>
          <button
            type="button"
            class="btn ghost-danger"
            @click="disconnectGithub"
          >Disconnect</button>
        </div>
      </div>
    </section>

    <!-- Placeholder for future services -->
    <section class="panel-card muted-card">
      <p>
        <strong>More services</strong> (Firebase, Home Assistant…) will land here over time.
        For now they still live in the Services tab of the Setup Wizard.
      </p>
    </section>
  </div>
</template>

<style scoped>
.svc-sections { display: flex; flex-direction: column; gap: 20px; }
.panel-card {
  background: var(--bg-card, #111318);
  border: 1px solid var(--border, #1e2030);
  border-radius: 12px;
  padding: 28px;
}
.panel-card > header {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  margin-bottom: 20px;
}
.panel-card h2 { font-size: 16px; font-weight: 600; color: var(--text-primary, #f0f2f5); }
.panel-card header p {
  margin-top: 4px;
  font-size: 13px;
  color: var(--text-nav, #8b8fa3);
  line-height: 1.5;
  max-width: 520px;
}
.icon-circle {
  flex-shrink: 0;
  width: 40px; height: 40px;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.04);
  display: grid; place-items: center;
}
.badge {
  margin-left: auto;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.3px;
  text-transform: uppercase;
}
.badge.on { background: rgba(34, 197, 94, 0.12); color: #22c55e; }
.badge.off { background: rgba(139, 143, 163, 0.15); color: #8b8fa3; }

.client-form,
.bundled-flow {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-top: 12px;
  border-top: 1px solid var(--border, #1e2030);
}
.client-form .muted,
.bundled-flow .muted {
  font-size: 13px;
  color: var(--text-nav, #8b8fa3);
  line-height: 1.5;
}
.client-form code,
.bundled-flow code {
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 6px;
  border-radius: 4px;
  font-family: ui-monospace, monospace;
  font-size: 12px;
}
.bundled-flow .steps {
  margin: 0;
  padding-left: 20px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 13px;
  color: var(--text-primary, #f0f2f5);
  line-height: 1.5;
}
.bundled-flow .steps em {
  color: var(--text-nav, #8b8fa3);
  font-style: italic;
}
.client-type-row {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.radio-pill {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 14px;
  border: 1px solid var(--border-input, #1e2030);
  border-radius: 8px;
  cursor: pointer;
  background: var(--bg-input, #0f172a);
  transition: border-color 0.15s, background 0.15s;
}
.radio-pill:hover { border-color: rgba(59, 130, 246, 0.4); }
.radio-pill input[type="radio"] { margin-top: 2px; accent-color: var(--accent-blue, #3b82f6); }
.radio-pill span { display: flex; flex-direction: column; gap: 2px; font-size: 13px; color: var(--text-primary, #f0f2f5); }
.radio-pill em {
  font-style: normal;
  font-size: 12px;
  color: var(--text-nav, #8b8fa3);
  line-height: 1.4;
}

.text-input {
  width: 100%;
  background: var(--bg-input, #0f172a);
  border: 1px solid var(--border-input, #1e2030);
  border-radius: 8px;
  padding: 10px 14px;
  color: var(--text-primary, #f0f2f5);
  font-family: inherit;
  font-size: 13px;
}
.text-input:focus {
  outline: none;
  border-color: var(--accent-blue, #3b82f6);
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
}

.connection-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding-top: 16px;
  border-top: 1px solid var(--border, #1e2030);
  gap: 20px;
}
.connection-row .meta { flex: 1; font-size: 13px; color: var(--text-primary, #f0f2f5); }
.connection-row .controls { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.meta-info { display: flex; flex-direction: column; gap: 4px; }
.meta .muted { font-size: 12px; color: var(--text-sub, #555872); }

.action-row {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 6px;
}
.btn {
  padding: 9px 16px;
  font-family: inherit;
  font-size: 13px;
  font-weight: 600;
  border-radius: 8px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-nav, #8b8fa3);
  cursor: pointer;
  transition: all 0.15s;
}
.btn.primary {
  background: var(--accent-blue, #3b82f6);
  color: #ffffff;
  border-color: var(--accent-blue, #3b82f6);
}
.btn.primary:hover:not([disabled]) { background: #2f6cdc; }
.btn.ghost-danger { border-color: rgba(239, 68, 68, 0.3); color: #ef4444; }
.btn.ghost-danger:hover { background: rgba(239, 68, 68, 0.08); }
.btn[disabled] { opacity: 0.5; cursor: not-allowed; }

.error-msg {
  margin-top: 14px;
  padding: 10px 14px;
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: 8px;
  color: #ef4444;
  font-size: 13px;
}
.success-msg {
  margin-top: 14px;
  padding: 10px 14px;
  background: rgba(34, 197, 94, 0.08);
  border: 1px solid rgba(34, 197, 94, 0.3);
  border-radius: 8px;
  color: #22c55e;
  font-size: 13px;
}
.warn-msg {
  margin-top: 6px;
  color: #f59e0b;
  font-size: 12px;
}
.api-enable-panel {
  margin-top: 16px;
  padding: 14px 16px;
  background: rgba(245, 158, 11, 0.06);
  border: 1px solid rgba(245, 158, 11, 0.25);
  border-radius: 8px;
}
.api-enable-header { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; color: var(--text-primary, #f0f2f5); font-size: 13px; }
.api-enable-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; }
.api-enable-list li { font-size: 13px; }
.api-enable-list a {
  display: inline-flex; align-items: baseline; gap: 6px;
  color: var(--accent-blue, #3b82f6);
  text-decoration: none;
}
.api-enable-list a:hover { text-decoration: underline; }
.api-enable-list .ext-arrow { font-size: 11px; }
.muted { color: var(--text-nav, #8b8fa3); font-size: 13px; }
.muted-card { padding: 18px 22px; }
.muted-card p { font-size: 13px; color: var(--text-nav, #8b8fa3); line-height: 1.5; }
</style>
