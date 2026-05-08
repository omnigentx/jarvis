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
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const apiKey = ref('')
const submitting = ref(false)
const errorMessage = ref('')
const inputEl = ref(null)
const modalEl = ref(null)

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

watch(visible, async (open) => {
  if (open) {
    errorMessage.value = ''
    apiKey.value = ''
    await nextTick()
    inputEl.value?.focus()
  }
})

onMounted(async () => {
  if (visible.value) {
    await nextTick()
    inputEl.value?.focus()
  }
})

async function handleSubmit() {
  const key = apiKey.value.trim()
  if (!key || submitting.value) return
  submitting.value = true
  errorMessage.value = ''
  try {
    const result = await auth.login(key)
    if (result.ok) {
      // Persist for legacy paths during the transition (Settings UI,
      // Setup Wizard still read this). Once those migrate this can go.
      try { localStorage.setItem('jarvis_api_key', key) } catch (_) { /* ignore */ }
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
      class="auth-gate-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="auth-gate-title"
      @keydown="onKeydown"
    >
      <div ref="modalEl" class="auth-gate-modal">
        <h2 id="auth-gate-title" class="auth-gate-title">
          Authentication required
        </h2>
        <p v-if="reasonHint" class="auth-gate-reason">{{ reasonHint }}</p>

        <form @submit.prevent="handleSubmit">
          <label for="auth-gate-key" class="auth-gate-label">API key</label>
          <input
            id="auth-gate-key"
            ref="inputEl"
            v-model="apiKey"
            type="password"
            autocomplete="current-password"
            placeholder="Paste JARVIS_API_KEY"
            class="auth-gate-input"
            :disabled="submitting"
          />
          <p v-if="errorMessage" class="auth-gate-error">{{ errorMessage }}</p>
          <div class="auth-gate-actions">
            <button
              type="submit"
              class="auth-gate-submit"
              :disabled="submitting || !apiKey.trim()"
            >
              {{ submitting ? 'Signing in…' : 'Sign in' }}
            </button>
          </div>
        </form>

        <button
          type="button"
          class="auth-gate-link"
          :disabled="submitting"
          @click="goToSetup"
        >
          Forgot your key? Re-run Setup Wizard
        </button>
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
  background: rgba(8, 10, 16, 0.85);
  backdrop-filter: blur(6px);
}
.auth-gate-modal {
  width: min(420px, 92vw);
  background: #0d1117;
  border: 1px solid #1e2030;
  border-radius: 10px;
  padding: 24px 28px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
}
.auth-gate-title {
  margin: 0 0 8px 0;
  font-size: 18px;
  font-weight: 600;
  color: #f3f6fc;
}
.auth-gate-reason {
  margin: 0 0 16px 0;
  font-size: 13px;
  color: #ffb547;
}
.auth-gate-label {
  display: block;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: #94a3b8;
  margin-bottom: 6px;
}
.auth-gate-input {
  width: 100%;
  height: 36px;
  padding: 0 12px;
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 6px;
  font-size: 13px;
  color: #f3f6fc;
  outline: none;
  font-family: 'SFMono-Regular', Menlo, monospace;
}
.auth-gate-input:focus {
  border-color: #3b82f6;
}
.auth-gate-error {
  margin: 8px 0 0 0;
  font-size: 12px;
  color: #ef4444;
}
.auth-gate-actions {
  display: flex;
  gap: 8px;
  margin-top: 16px;
}
.auth-gate-submit {
  flex: 1;
  height: 36px;
  background: #3b82f6;
  border: none;
  border-radius: 6px;
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}
.auth-gate-submit:disabled {
  background: #1e293b;
  cursor: not-allowed;
}
.auth-gate-link {
  display: block;
  width: 100%;
  margin-top: 14px;
  padding: 6px 0;
  background: transparent;
  border: none;
  color: #64748b;
  font-size: 11px;
  cursor: pointer;
  text-decoration: underline;
}
.auth-gate-link:hover {
  color: #94a3b8;
}
</style>
