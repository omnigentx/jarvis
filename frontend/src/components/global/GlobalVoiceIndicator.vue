<script setup>
/**
 * GlobalVoiceIndicator — floating pill that shows hands-free status on
 * every route EXCEPT /chat (where VoiceBar already renders inline).
 *
 * Lets the user know mic is still live while they navigate to /monitor,
 * /agents, etc., and gives them one-click access to:
 *   • interrupt / barge-in while the agent is speaking,
 *   • stop the session entirely,
 *   • jump back to /chat to see the conversation.
 *
 * The voice session is a module-level singleton (see
 * composables/useVoiceSession.js) so this component reads the same
 * status refs as VoiceBar — they always agree.
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useVoiceSession } from '../../composables/useVoiceSession.js'
import { useLang } from '../../composables/useLang'

const { t } = useLang()
const session = useVoiceSession()
const route = useRoute()
const router = useRouter()

// Hide on /chat: VoiceBar is already on screen there, two indicators
// would be redundant noise. Also hide when the session is genuinely off.
const visible = computed(() => {
  if (route.name === 'Chat') return false
  const s = session.status.value
  return s !== 'idle'
})

const statusInfo = computed(() => {
  switch (session.status.value) {
    case 'connecting':   return { label: t('voiceIndicator.connecting'), color: '#f59e0b', dot: true }
    case 'loading_stt':  return { label: t('voiceIndicator.loadingStt'), color: '#f59e0b', dot: true }
    case 'listening':    return { label: t('voiceIndicator.listening'),  color: '#10b981', dot: true }
    case 'thinking':     return { label: t('voiceIndicator.thinking'),   color: '#3b82f6', dot: true, spin: true }
    case 'speaking':     return { label: t('voiceIndicator.speaking'),   color: '#8b5cf6', dot: true, spin: true }
    case 'error':        return { label: t('voiceIndicator.error'),      color: '#ef4444', dot: true }
    default:             return { label: t('voiceIndicator.off'),        color: '#555872', dot: false }
  }
})

const canInterrupt = computed(() =>
  session.status.value === 'thinking' || session.status.value === 'speaking',
)

function goToChat() {
  router.push({ name: 'Chat' })
}

async function handleStop() {
  try { await session.stop() } catch { /* already torn */ }
}

function handleBargeIn() {
  session.bargeIn()
}
</script>

<template>
  <transition name="indicator-fade">
    <div v-if="visible" class="voice-indicator" :class="`status-${session.status.value}`">
      <button
        class="indicator-body"
        type="button"
        :title="t('voiceIndicator.openChat')"
        @click="goToChat"
      >
        <span class="dot" :class="{ pulse: statusInfo.dot, spinning: statusInfo.spin }" :style="{ background: statusInfo.color }" />
        <span class="status-text">🎙️ {{ statusInfo.label }}</span>
        <span v-if="session.partialTranscript.value" class="partial">
          "{{ session.partialTranscript.value.slice(0, 40) }}{{ session.partialTranscript.value.length > 40 ? '…' : '' }}"
        </span>
      </button>

      <button
        v-if="canInterrupt"
        type="button"
        class="indicator-action interrupt"
        :title="t('voiceIndicator.interrupt')"
        @click="handleBargeIn"
      >
        ⏸
      </button>

      <button
        type="button"
        class="indicator-action stop"
        :title="t('voiceIndicator.stop')"
        @click="handleStop"
      >
        ✕
      </button>
    </div>
  </transition>
</template>

<style scoped>
.voice-indicator {
  position: fixed;
  bottom: 18px;
  right: 18px;
  z-index: 150;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 6px 6px 14px;
  background: rgba(12, 14, 21, 0.92);
  border: 1px solid #1a1d2e;
  border-radius: 24px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  backdrop-filter: blur(8px);
  font-size: 12px;
  color: #f0f2f5;
  min-width: 0;
  max-width: 380px;
}

.voice-indicator.status-listening {
  border-color: rgba(16, 185, 129, 0.45);
  box-shadow: 0 0 14px rgba(16, 185, 129, 0.18), 0 8px 24px rgba(0,0,0,0.4);
}

.voice-indicator.status-thinking,
.voice-indicator.status-speaking {
  border-color: rgba(139, 92, 246, 0.45);
  box-shadow: 0 0 14px rgba(139, 92, 246, 0.20), 0 8px 24px rgba(0,0,0,0.4);
}

.voice-indicator.status-error {
  border-color: rgba(239, 68, 68, 0.55);
}

.indicator-body {
  display: flex;
  align-items: center;
  gap: 8px;
  background: transparent;
  border: none;
  color: inherit;
  padding: 4px 8px;
  cursor: pointer;
  font: inherit;
  border-radius: 18px;
  flex: 1;
  min-width: 0;
}

.indicator-body:hover {
  background: rgba(255,255,255,0.04);
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.dot.pulse {
  animation: dot-pulse 1.4s ease-in-out infinite;
}

.dot.spinning {
  animation: dot-pulse 0.9s ease-in-out infinite;
}

@keyframes dot-pulse {
  0%, 100% { box-shadow: 0 0 0 0 currentColor; opacity: 1; }
  50%      { box-shadow: 0 0 0 5px transparent; opacity: 0.55; }
}

.status-text {
  font-weight: 600;
  white-space: nowrap;
  flex-shrink: 0;
}

.partial {
  font-style: italic;
  font-weight: 400;
  color: #8b8fa3;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.indicator-action {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #111318;
  border: 1px solid #1a1d2e;
  color: #c4c8d4;
  border-radius: 50%;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.15s;
  flex-shrink: 0;
}

.indicator-action:hover {
  background: #1e2233;
  color: #f0f2f5;
}

.indicator-action.interrupt:hover {
  background: rgba(245, 158, 11, 0.20);
  color: #f59e0b;
  border-color: rgba(245, 158, 11, 0.45);
}

.indicator-action.stop:hover {
  background: rgba(239, 68, 68, 0.20);
  color: #ef4444;
  border-color: rgba(239, 68, 68, 0.45);
}

/* Mobile: smaller, hide partial transcript */
@media (max-width: 767px) {
  .voice-indicator {
    /* Clear the bottom tab bar + safe-area + mini player (when visible),
       matching FloatingChatDock — a hardcoded 78px missed the safe-area on
       notched phones and the mini player, leaving the mic over the nav. */
    bottom: calc(var(--mobile-tabbar-h, 64px) + var(--safe-bottom, 0px) + var(--mini-player-h, 0px) + 12px);
    right: 12px;
    max-width: calc(100vw - 24px);
  }

  .partial {
    display: none;
  }
}

.indicator-fade-enter-active,
.indicator-fade-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}

.indicator-fade-enter-from,
.indicator-fade-leave-to {
  opacity: 0;
  transform: translateY(8px);
}
</style>
