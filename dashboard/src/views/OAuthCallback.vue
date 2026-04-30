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

const message = ref('Completing Google sign-in...')
const error = ref('')

onMounted(() => {
  const url = new URL(window.location.href)
  const code = url.searchParams.get('code')
  const state = url.searchParams.get('state')
  const errParam = url.searchParams.get('error')

  if (errParam) {
    error.value = `Google returned error: ${errParam}`
    if (window.opener) {
      window.opener.postMessage(
        { type: 'jarvis:oauth:google', error: errParam },
        window.location.origin,
      )
    }
    return
  }

  if (!code || !state) {
    error.value = 'Missing code/state in redirect. Please restart the flow.'
    return
  }

  if (!window.opener) {
    // User refreshed / navigated here manually — nothing to post to.
    error.value =
      'No opener window. Close this tab and restart the flow from Settings → Services.'
    return
  }

  window.opener.postMessage(
    { type: 'jarvis:oauth:google', code, state },
    window.location.origin,
  )
  message.value = 'You can close this window now.'
  // Small delay so the opener has time to receive the message before we close.
  setTimeout(() => window.close(), 400)
})
</script>

<template>
  <div class="cb-page">
    <div class="cb-card">
      <h1>Google OAuth</h1>
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
  color: var(--text-primary, #f0f2f5);
  font-family: inherit;
}
.cb-card {
  background: var(--bg-card, #111318);
  border: 1px solid var(--border, #1e2030);
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
.cb-card p { font-size: 14px; color: var(--text-nav, #8b8fa3); line-height: 1.5; }
.cb-card .error { color: #ef4444; }
</style>
