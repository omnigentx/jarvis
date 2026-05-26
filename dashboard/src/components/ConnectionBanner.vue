<script setup>
/**
 * ConnectionBanner — thin status bar at the top of the app showing the
 * SSE/WS realtime connection state and a one-click Reconnect.
 *
 * "Authentication" is no longer this component's concern: AuthGate
 * covers the entire app when the user is not signed in, and there is
 * nothing useful this banner could do that AuthGate isn't already
 * doing better. The previous "Set API Key" inline input + localStorage
 * write was removed alongside the cookie-only auth migration.
 */
import { useAuthStore } from '../stores/auth'

const props = defineProps({
  status: { type: String, default: 'disconnected' },
})

const emit = defineEmits(['reconnect'])

// Read authenticated state from the auth store; the Reconnect button
// only makes sense when the user IS signed in (otherwise AuthGate is
// covering them and clicking through here would just 401 again).
const auth = useAuthStore()

const bannerConfig = {
  connected: { text: '● Live', color: '#22c55e', bg: 'rgba(34,197,94,0.06)', show: true },
  connecting: { text: '● Connecting...', color: '#ffb547', bg: 'rgba(255,181,71,0.06)', show: true },
  disconnected: { text: '● Disconnected', color: '#ef4444', bg: 'rgba(239,68,68,0.06)', show: true },
  error: { text: '● Connection error — Retrying...', color: '#ef4444', bg: 'rgba(239,68,68,0.06)', show: true },
}
</script>

<template>
  <div
    :style="{
      display: 'flex',
      alignItems: 'center',
      gap: '8px',
      padding: '6px 36px',
      fontSize: '11px',
      fontWeight: '500',
      color: bannerConfig[status]?.color || '#ef4444',
      background: bannerConfig[status]?.bg || 'transparent',
      borderBottom: '1px solid #1e2030',
      flexShrink: 0,
    }"
  >
    <span
      :class="status === 'connecting' ? 'animate-pulse-dot' : ''"
      :style="{ color: bannerConfig[status]?.color }"
    >
      {{ bannerConfig[status]?.text || status }}
    </span>

    <button
      v-if="(status === 'disconnected' || status === 'error') && auth.isAuthenticated"
      @click="emit('reconnect')"
      style="margin-left: 8px; padding: 2px 10px; background: #3b82f6; border: none; border-radius: 4px; color: #fff; font-size: 11px; font-weight: 600; cursor: pointer;"
    >
      Reconnect
    </button>
  </div>
</template>
