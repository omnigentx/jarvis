<script setup>
/**
 * AuthGate — full-screen modal that blocks the dashboard when the
 * session is unauthenticated.
 *
 * UX rules
 * --------
 * * Modal is the ONLY UI visible until login succeeds — the user cannot
 *   Esc out of it. There's a "Reset & re-run setup" link for the
 *   "I forgot my key" path.
 * * Focus is trapped inside the modal (a11y).
 * * Last-failure reason is surfaced verbatim from the backend so we
 *   never have to translate magic strings to user-readable text twice.
 *
 * Visual restyle to the redesign auth modal — logic (passkey probe,
 * 401 re-probe, focus trap, reasonHint) is unchanged.
 */
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '../stores/auth'
import {
  authenticateWithPasskey,
  hasAnyPasskey,
  isSupported as isPasskeySupported,
} from '../services/passkey.js'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const apiKey = ref('')
const submitting = ref(false)
const errorMessage = ref('')
const inputEl = ref(null)
const modalEl = ref(null)

// Passkey availability is probed once per gate-open. Default to false
// (hide button) so a backend hiccup or unsupported browser never
// blocks the API-key fallback.
const passkeyOffered = ref(false)
const passkeyBusy = ref(false)
// "Use API key instead" reveal — closed by default when passkey is
// offered, open by default when it isn't.
const showApiKey = ref(false)

// AuthGate stays hidden on the bare setup layout: that flow has its
// own auth bootstrap (Step 1 of the wizard mints / confirms the key)
// and stacking the modal on top would block it. Only the main app
// chrome surfaces the modal. Watching ``route.meta`` instead of
// hard-coding paths lets new bare-layout routes opt-in via
// ``meta.layout: 'bare'`` without touching this file.
const visible = computed(
  () => auth.showAuthGate && route.meta?.layout !== 'bare',
)

const reasonHint = computed(() => {
  const r = auth.lastReason || ''
  switch (r) {
    case 'key_rotated':
      return 'Master key was rotated. Paste the new key to continue.'
    case 'expired':
    case 'whoami_unauthenticated':
    case 'rest_401':
      return 'Your session has expired. Please log in again.'
    case 'max_lifetime_exceeded':
      return 'Session reached its maximum lifetime. Please log in again.'
    case 'invalid_credentials':
      return 'Wrong API key. Try again, or reset via setup.'
    case 'cross_tab':
      return 'Logged out from another tab.'
    case 'logout':
      return 'Logged out.'
    case '':
      return ''
    default:
      return `Authentication required (${r}).`
  }
})

async function probePasskey() {
  if (!isPasskeySupported()) {
    passkeyOffered.value = false
    showApiKey.value = true
    return
  }
  const has = await hasAnyPasskey()
  passkeyOffered.value = has
  showApiKey.value = !has
}

async function _onGateOpen() {
  errorMessage.value = ''
  apiKey.value = ''
  await probePasskey()
  await nextTick()
  if (passkeyOffered.value) {
    modalEl.value?.querySelector('.auth-gate-passkey-btn')?.focus()
  } else {
    inputEl.value?.focus()
  }
}

watch(visible, async (open) => {
  if (open) await _onGateOpen()
}, { immediate: true })

async function handlePasskey() {
  if (passkeyBusy.value) return
  passkeyBusy.value = true
  errorMessage.value = ''
  try {
    const result = await authenticateWithPasskey()
    if (result.ok) {
      auth.setAuthenticated(result.csrfToken, result.expiresIn)
      return
    }
    switch (result.code) {
      case 'cancelled':
        errorMessage.value = ''
        break
      case 'credential_unknown':
        errorMessage.value =
          'This passkey is not recognised on this deployment. ' +
          'Sign in with your API key, then re-register the passkey.'
        showApiKey.value = true
        break
      case 'rate_limited':
        errorMessage.value =
          'Too many attempts. Wait a minute and try again.'
        break
      case 'unsupported':
        errorMessage.value = 'Your browser does not support passkeys.'
        passkeyOffered.value = false
        showApiKey.value = true
        break
      case 'network':
        errorMessage.value =
          'Network error. Check the backend is reachable.'
        break
      default:
        errorMessage.value =
          result.detail || 'Passkey sign-in failed.'
    }
  } finally {
    passkeyBusy.value = false
  }
}

async function revealApiKey() {
  showApiKey.value = true
  await nextTick()
  inputEl.value?.focus()
}

async function handleSubmit() {
  const key = apiKey.value.trim()
  if (!key || submitting.value) return
  submitting.value = true
  errorMessage.value = ''
  try {
    const result = await auth.login(key)
    if (result.ok) {
      apiKey.value = ''
    } else if (result.status === 401) {
      errorMessage.value = 'Wrong API key.'
    } else if (result.status === 429) {
      errorMessage.value = 'Too many attempts. Wait a minute and try again.'
    } else if (result.status === 503) {
      errorMessage.value = 'Backend not configured. Run setup first.'
    } else if (result.status === 0) {
      errorMessage.value = 'Network error. Check the backend is reachable.'
    } else {
      errorMessage.value = `Login failed (${result.status}).`
    }
  } finally {
    submitting.value = false
  }
}

function goToSetup() {
  router.push('/setup')
}

/**
 * Focus trap: keep Tab inside the modal so the user cannot tab into
 * the (interaction-disabled) background and lose visual focus.
 * Only intercepts when the modal is open.
 */
function onKeydown(event) {
  if (!visible.value) return
  if (event.key !== 'Tab') return
  const focusables = modalEl.value?.querySelectorAll(
    'input, button, [href], [tabindex]:not([tabindex="-1"])',
  )
  if (!focusables || focusables.length === 0) return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="auth-gate-overlay jv"
      role="dialog"
      aria-modal="true"
      aria-labelledby="auth-gate-title"
      @keydown="onKeydown"
    >
      <!-- Decorative grid backdrop -->
      <div class="auth-gate-grid grid-bg"></div>

      <!-- Brand crown -->
      <div class="auth-gate-brand">
        <div class="auth-gate-brand__mark">J</div>
        <div class="eyebrow auth-gate-brand__eyebrow">OMNIGENTX · SELF-HOSTED</div>
        <h1 class="auth-gate-brand__title">
          <span class="grad" style="font-style: italic">Jarvis</span>
        </h1>
      </div>

      <div ref="modalEl" class="auth-gate-modal hud">
        <span class="hud-br"></span>

        <div class="auth-gate-head">
          <span class="auth-gate-lockwrap">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" stroke-width="2"
              stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M6 11h12v9H6zM9 11V8a3 3 0 0 1 6 0v3" />
            </svg>
          </span>
          <h2 id="auth-gate-title" class="auth-gate-title">Authentication required</h2>
          <p v-if="reasonHint" class="auth-gate-reason">{{ reasonHint }}</p>
          <p v-else class="auth-gate-reason auth-gate-reason--neutral">
            Paste your JARVIS_API_KEY to access the dashboard.
          </p>
        </div>

        <button
          v-if="passkeyOffered"
          type="button"
          class="auth-gate-passkey-btn"
          :disabled="passkeyBusy || submitting"
          @click="handlePasskey"
        >
          <svg
            class="auth-gate-passkey-icon"
            width="18" height="18" viewBox="0 0 24 24"
            fill="none" stroke="currentColor" stroke-width="2"
            stroke-linecap="round" stroke-linejoin="round"
            aria-hidden="true"
          >
            <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" />
          </svg>
          {{ passkeyBusy ? 'Waiting for passkey…' : 'Sign in with passkey' }}
        </button>

        <p v-if="passkeyOffered && errorMessage" class="auth-gate-error">
          {{ errorMessage }}
        </p>

        <button
          v-if="passkeyOffered && !showApiKey"
          type="button"
          class="auth-gate-link auth-gate-link--secondary"
          :disabled="passkeyBusy"
          @click="revealApiKey"
        >
          Use API key instead
        </button>

        <form v-if="showApiKey" @submit.prevent="handleSubmit">
          <label for="auth-gate-key" class="auth-gate-label mono-label">API KEY</label>
          <input
            id="auth-gate-key"
            ref="inputEl"
            v-model="apiKey"
            type="password"
            autocomplete="current-password"
            placeholder="Paste JARVIS_API_KEY (from your .env)"
            class="auth-gate-input"
            :class="{ 'auth-gate-input--error': !passkeyOffered && errorMessage }"
            :disabled="submitting"
          />
          <p v-if="!passkeyOffered && errorMessage" class="auth-gate-error">
            {{ errorMessage }}
          </p>
          <button
            type="submit"
            class="auth-gate-submit"
            :disabled="submitting || !apiKey.trim()"
          >
            {{ submitting ? 'Authenticating…' : 'Continue' }}
            <svg v-if="!submitting" width="13" height="13" viewBox="0 0 24 24" fill="none">
              <path d="M5 12h14M13 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </button>
        </form>

        <div class="auth-gate-footer">
          <button
            type="button"
            class="auth-gate-link"
            :disabled="submitting || passkeyBusy"
            @click="goToSetup"
          >
            Forgot your key? Re-run Setup Wizard →
          </button>
          <div class="auth-gate-fineprint mono-label">
            KEY STORED IN HTTP-ONLY COOKIE · NO LOCAL TOKEN
          </div>
        </div>
      </div>

      <div class="auth-gate-tagline mono-label">
        <span>Just A Rather Very Intelligent System</span>
        <span class="auth-gate-tagline__sep">·</span>
        <span>MIT</span>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.auth-gate-overlay {
  position: fixed;
  inset: 0;
  z-index: 9999;
  display: flex;
  align-items: center;
  justify-content: center;
  background: radial-gradient(ellipse at center, var(--bg-1), var(--bg-0) 80%);
  font-family: var(--font-body);
  color: var(--text);
}

.auth-gate-grid {
  position: absolute;
  inset: 0;
  opacity: 0.18;
  -webkit-mask-image: radial-gradient(circle at center, black, transparent 70%);
  mask-image: radial-gradient(circle at center, black, transparent 70%);
  pointer-events: none;
}

.auth-gate-brand {
  position: absolute;
  top: 60px;
  left: 50%;
  transform: translateX(-50%);
  text-align: center;
}
.auth-gate-brand__mark {
  width: 64px;
  height: 64px;
  border-radius: 50%;
  background: radial-gradient(circle at 30% 30%, var(--primary-hover), var(--primary));
  box-shadow: 0 0 48px var(--primary-glow);
  color: #fff;
  font-family: var(--font-display);
  font-size: 24px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto 14px;
}
.auth-gate-brand__eyebrow { justify-content: center; margin-bottom: 4px; }
.auth-gate-brand__title {
  font-family: var(--font-display);
  font-size: 28px;
  margin: 0;
}

.auth-gate-modal {
  position: relative;
  z-index: 2;
  width: min(420px, 92vw);
  background: var(--bg-2);
  border: 1px solid var(--border-bright);
  border-radius: var(--r-xl);
  padding: 28px;
  box-shadow: var(--shadow-lg);
}

.auth-gate-head { text-align: center; margin-bottom: 18px; }
.auth-gate-lockwrap {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border-radius: 50%;
  background: var(--primary-bg);
  border: 1px solid var(--primary-bg-strong);
  color: var(--primary-hover);
  margin-bottom: 12px;
}
.auth-gate-title {
  margin: 0 0 4px 0;
  font-size: 18px;
  font-family: var(--font-display);
  font-weight: 600;
  color: var(--text);
}
.auth-gate-reason {
  margin: 0;
  font-size: 12.5px;
  color: var(--warning);
}
.auth-gate-reason--neutral { color: var(--text-dim); }

.auth-gate-label {
  display: block;
  font-size: 10px;
  margin-bottom: 6px;
}
.auth-gate-input {
  width: 100%;
  height: 40px;
  padding: 0 12px;
  background: var(--bg-4);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 13px;
  outline: none;
  transition: border-color 0.15s var(--ease-out);
}
.auth-gate-input:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-bg);
}
.auth-gate-input--error { border-color: var(--danger); }

.auth-gate-error {
  margin: 8px 0 0 0;
  font-size: 12px;
  color: var(--danger);
}

.auth-gate-submit {
  width: 100%;
  height: 40px;
  margin-top: 14px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border-radius: var(--r-md);
  border: 1px solid transparent;
  font-weight: 500;
  font-size: 13.5px;
  font-family: var(--font-body);
  background: linear-gradient(180deg, var(--primary-hover), var(--primary));
  color: #fff;
  box-shadow: 0 1px 0 rgba(255,255,255,0.18) inset, 0 8px 24px -8px var(--primary-glow);
  cursor: pointer;
  transition: transform 0.18s var(--ease-out), filter 0.18s var(--ease-out);
}
.auth-gate-submit:hover:not(:disabled) { transform: translateY(-1px); }
.auth-gate-submit:disabled {
  background: var(--bg-3);
  color: var(--text-muted);
  box-shadow: none;
  cursor: not-allowed;
}

.auth-gate-passkey-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  height: 40px;
  margin-top: 4px;
  border-radius: var(--r-md);
  border: 1px solid transparent;
  font-size: 13.5px;
  font-weight: 500;
  font-family: var(--font-body);
  background: linear-gradient(180deg, var(--primary-hover), var(--primary));
  color: #fff;
  box-shadow: 0 1px 0 rgba(255,255,255,0.18) inset, 0 8px 24px -8px var(--primary-glow);
  cursor: pointer;
  transition: transform 0.18s var(--ease-out), filter 0.18s var(--ease-out);
}
.auth-gate-passkey-btn:hover:not(:disabled) { transform: translateY(-1px); }
.auth-gate-passkey-btn:disabled {
  background: var(--bg-3);
  color: var(--text-muted);
  box-shadow: none;
  cursor: not-allowed;
}
.auth-gate-passkey-icon { flex-shrink: 0; }

.auth-gate-footer {
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.auth-gate-fineprint {
  font-size: 9.5px;
  color: var(--text-subtle);
}

.auth-gate-link {
  display: block;
  width: 100%;
  padding: 6px 0;
  background: transparent;
  border: none;
  color: var(--accent);
  font-size: 12px;
  text-align: center;
  cursor: pointer;
}
.auth-gate-link:hover:not(:disabled) { color: var(--primary-hover); }
.auth-gate-link:disabled { opacity: 0.5; cursor: not-allowed; }
.auth-gate-link--secondary {
  margin-top: 12px;
  margin-bottom: 4px;
  color: var(--text-dim);
  font-size: 12.5px;
  text-decoration: underline;
}

.auth-gate-tagline {
  position: absolute;
  bottom: 24px;
  left: 0;
  right: 0;
  text-align: center;
  font-size: 11px;
  color: var(--text-muted);
}
.auth-gate-tagline__sep {
  margin: 0 8px;
  color: var(--text-subtle);
}
</style>
