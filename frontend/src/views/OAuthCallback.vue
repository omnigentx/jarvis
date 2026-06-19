<script setup>
/**
 * OAuth redirect landing page.
 *
 * Google bounces the user back here with ?code=...&state=... after consent.
 * We forward those via postMessage to the window that opened us (the
 * Settings → Services tab) and close.  That keeps the exchange out of the
 * URL bar of the main app — the opener is responsible for POSTing them to
 * /api/oauth/google/callback.
 */
import { onMounted, ref } from 'vue'
import { useLang } from '../composables/useLang'

const { t } = useLang()
const message = ref(t('oauth.completing'))
const error = ref('')

onMounted(() => {
  const url = new URL(window.location.href)
  const code = url.searchParams.get('code')
  const state = url.searchParams.get('state')
  const errParam = url.searchParams.get('error')

  if (errParam) {
    error.value = t('oauth.googleError', { err: errParam })
    if (window.opener) {
      window.opener.postMessage(
        { type: 'jarvis:oauth:google', error: errParam },
        window.location.origin,
      )
    }
    return
  }

  if (!code || !state) {
    error.value = t('oauth.missingCodeState')
    return
  }

  if (!window.opener) {
    // User refreshed / navigated here manually — nothing to post to.
    error.value = t('oauth.noOpener')
    return
  }

  window.opener.postMessage(
    { type: 'jarvis:oauth:google', code, state },
    window.location.origin,
  )
  message.value = t('oauth.canClose')
  // Small delay so the opener has time to receive the message before we close.
  setTimeout(() => window.close(), 400)
})
</script>

<template>
  <div class="cb-page">
    <div class="cb-card">
      <h1>{{ t('oauth.title') }}</h1>
      <p v-if="!error">{{ message }}</p>
      <p v-else class="error">{{ error }}</p>
    </div>
  </div>
</template>

<style scoped>
.cb-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  background: var(--bg-app, #0b0d13);
  color: var(--text-primary, var(--text));
  font-family: inherit;
}
.cb-card {
  background: var(--bg-card, var(--bg-2));
  border: 1px solid var(--border, var(--border-strong));
  border-radius: 12px;
  padding: 28px 32px;
  max-width: 420px;
  text-align: center;
}
.cb-card h1 {
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 8px;
}
.cb-card p { font-size: 14px; color: var(--text-nav, var(--text-muted)); line-height: 1.5; }
.cb-card .error { color: #ef4444; }
</style>
