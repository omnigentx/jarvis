<script setup>
import { ref, watch } from 'vue'
import { getApiKey, setApiKey } from '../api'

const props = defineProps({
  status: { type: String, default: 'disconnected' },
})

const emit = defineEmits(['reconnect'])
const apiKeyInput = ref('')
const showKeyInput = ref(false)

const hasKey = ref(!!getApiKey())

watch(() => props.status, (newStatus) => {
  // Auto-hide key input when connected
  if (newStatus === 'connected') showKeyInput.value = false
})

function connectWithKey() {
  const key = apiKeyInput.value.trim()
  if (!key) return
  setApiKey(key)
  hasKey.value = true
  showKeyInput.value = false
  apiKeyInput.value = ''
  emit('reconnect')
}

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
    <!-- Status text -->
    <span
      :class="status === 'connecting' ? 'animate-pulse-dot' : ''"
      :style="{ color: bannerConfig[status]?.color }"
    >
      {{ bannerConfig[status]?.text || status }}
    </span>

    <!-- Connect button when no key or disconnected -->
    <template v-if="status === 'disconnected' || status === 'error'">
      <button
        v-if="!showKeyInput && !hasKey"
        @click="showKeyInput = true"
        style="margin-left: 8px; padding: 2px 10px; background: #3b82f6; border: none; border-radius: 4px; color: #fff; font-size: 11px; font-weight: 600; cursor: pointer;"
      >
        Set API Key
      </button>
      <button
        v-if="hasKey"
        @click="emit('reconnect')"
        style="margin-left: 8px; padding: 2px 10px; background: #3b82f6; border: none; border-radius: 4px; color: #fff; font-size: 11px; font-weight: 600; cursor: pointer;"
      >
        Reconnect
      </button>

      <!-- Inline key input -->
      <div v-if="showKeyInput" style="display: flex; gap: 4px; margin-left: 8px;">
        <input
          v-model="apiKeyInput"
          type="password"
          placeholder="Paste API key..."
          @keyup.enter="connectWithKey"
          style="width: 220px; height: 22px; padding: 0 8px; background: #111318; border: 1px solid #1e2030; border-radius: 4px; font-size: 11px; color: #f3f6fc; outline: none;"
        />
        <button
          @click="connectWithKey"
          style="padding: 2px 10px; background: #22c55e; border: none; border-radius: 4px; color: #fff; font-size: 11px; font-weight: 600; cursor: pointer;"
        >
          Connect
        </button>
        <button
          @click="showKeyInput = false"
          style="padding: 2px 6px; background: transparent; border: 1px solid #1e2030; border-radius: 4px; color: #64748b; font-size: 11px; cursor: pointer;"
        >
          ✕
        </button>
      </div>
    </template>
  </div>
</template>
