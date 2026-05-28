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
import { computed } from 'vue'
import { useAuthStore } from '../stores/auth'

const props = defineProps({
  status: { type: String, default: 'disconnected' },
})

const emit = defineEmits(['reconnect'])

const auth = useAuthStore()

// Map status → role colour token + label. ``connected`` is the
// happy-path and is hidden entirely so the banner doesn't burn
// 26px of vertical space during normal operation.
const bannerConfig = {
  connected:    { text: '● Live',                          role: 'success', show: false },
  connecting:   { text: '● Connecting…',                   role: 'warning', show: true },
  disconnected: { text: '● Disconnected',                  role: 'danger',  show: true },
  error:        { text: '● Connection error — retrying…',  role: 'danger',  show: true },
}

const current = computed(() => bannerConfig[props.status] || bannerConfig.disconnected)
const visible = computed(() => current.value.show !== false)
</script>

<template>
  <div v-if="visible" class="conn-banner" :class="`conn-banner--${current.role}`">
    <span class="conn-banner__text" :class="{ 'pulse-dot': status === 'connecting' }">
      {{ current.text }}
    </span>

    <button
      v-if="(status === 'disconnected' || status === 'error') && auth.isAuthenticated"
      class="conn-banner__btn"
      @click="emit('reconnect')"
    >
      Reconnect
    </button>
  </div>
</template>

<style scoped>
.conn-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 36px;
  font-size: 11px;
  font-weight: 500;
  font-family: var(--font-mono);
  letter-spacing: 0.02em;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  color: var(--text-dim);
  background: var(--bg-1);
}
.conn-banner--success { color: var(--success); background: var(--success-bg); }
.conn-banner--warning { color: var(--warning); background: var(--warning-bg); }
.conn-banner--danger  { color: var(--danger);  background: var(--danger-bg); }

.conn-banner__text { line-height: 1.4; }

.conn-banner__btn {
  margin-left: 8px;
  padding: 2px 10px;
  background: var(--primary);
  border: none;
  border-radius: var(--r-sm);
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  font-family: var(--font-body);
  cursor: pointer;
  transition: filter 0.15s var(--ease-out);
}
.conn-banner__btn:hover { filter: brightness(1.1); }
</style>
