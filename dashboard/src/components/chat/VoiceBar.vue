<script setup>
/**
 * VoiceBar — single-strip hands-free toggle.
 *
 * Sits between ChatHeader and ChatMessages. The bar shows mic toggle,
 * status (Off / Connecting… / Loading STT model… / Listening / Thinking /
 * Speaking), live partial transcript, and an Interrupt button while the
 * agent is talking. Conversation messages themselves are NOT rendered
 * here — they go straight into the normal chat message panel via
 * chatStore (addUserMessage / addAgentMessagePlaceholder /
 * finalizeAgentMessage), so voice and typed turns share one UI.
 */
import { computed, onBeforeUnmount } from 'vue'
import { useVoiceSession } from '../../composables/useVoiceSession.js'

const session = useVoiceSession()

// Tear down on real unmount so navigating away doesn't leak the mic
// / WS / AudioContext. Diagnostic-tagged so when the user sees an
// unexpected "Mic Off" we can tell from the console what triggered
// it (route nav vs HMR vs explicit stop button).
onBeforeUnmount(() => {
  console.debug('[voice] VoiceBar onBeforeUnmount → stop()', {
    status: session.status.value,
    hmr: !!import.meta.hot,
    href: location.href,
  })
  Promise.resolve(session.stop()).catch(() => {})
})

const STATUS_LABELS = {
  idle: 'Off',
  connecting: 'Connecting…',
  loading_stt: 'Loading STT model (first run ~30s)…',
  listening: 'Listening',
  thinking: 'Agent is thinking…',
  speaking: 'Speaking',
  error: 'Error',
}
const statusLabel = computed(() => STATUS_LABELS[session.status.value] || session.status.value)
const isOn = computed(() => session.status.value !== 'idle' && session.status.value !== 'error')
const canInterrupt = computed(() =>
  session.status.value === 'thinking' || session.status.value === 'speaking',
)

async function toggle() {
  if (isOn.value) await session.stop()
  else await session.start()
}
</script>

<template>
  <div class="voice-bar" :class="{ on: isOn, error: session.status.value === 'error' }">
    <button
      class="mic"
      type="button"
      :title="isOn ? 'Stop hands-free' : 'Start hands-free'"
      @click="toggle"
    >
      <span class="dot" />
      <span class="label">{{ isOn ? 'Stop' : 'Mic' }}</span>
    </button>
    <span
      class="status-pill"
      :class="`pill-${session.status.value}`"
      :title="`Voice status: ${statusLabel}`"
    >
      <span v-if="session.status.value === 'thinking' || session.status.value === 'speaking'" class="spinner" />
      {{ statusLabel }}
    </span>
    <span class="transcript" :title="session.lastFinalTranscript.value">
      <template v-if="session.partialTranscript.value">{{ session.partialTranscript.value }}</template>
      <template v-else-if="session.lastFinalTranscript.value">"{{ session.lastFinalTranscript.value }}"</template>
      <template v-else><em>Speak after enabling the mic — partial transcripts appear here.</em></template>
    </span>
    <button
      v-if="canInterrupt"
      type="button"
      class="action danger"
      @click="session.bargeIn()"
    >
      Interrupt
    </button>
    <span v-if="session.error.value" class="err-msg">{{ session.error.value }}</span>
  </div>
</template>

<style scoped>
.voice-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--border-sidebar, #1e2030);
  background: var(--bg-sidebar, #0e1020);
  font-size: 13px;
}
.voice-bar.on { background: rgba(59, 130, 246, 0.05); }
.voice-bar.error { background: rgba(239, 68, 68, 0.08); }

.mic {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: var(--bg-card, #161826);
  border: 1px solid var(--border-sidebar, #1e2030);
  color: var(--text-primary, #f0f2f5);
  padding: 6px 12px;
  border-radius: 999px;
  cursor: pointer;
  font: inherit;
}
.mic .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-nav, #8b8fa3);
}
.voice-bar.on .mic .dot {
  background: var(--accent-red, #ef4444);
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  50% { transform: scale(1.4); opacity: 0.6; }
}

/* status-pill: prominent so the user can tell at a glance whether the
   agent is just listening, processing their last utterance, or speaking
   back. Without this the only signal was a tiny grey label that blended
   into the bar — easy to miss. */
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 999px;
  font-weight: 600;
  font-size: 12px;
  white-space: nowrap;
  border: 1px solid transparent;
  background: var(--bg-card, #161826);
  color: var(--text-secondary, #c4c8d4);
}
.status-pill.pill-listening {
  background: rgba(34, 197, 94, 0.12);
  color: #22c55e;
  border-color: rgba(34, 197, 94, 0.3);
}
.status-pill.pill-thinking {
  background: rgba(245, 158, 11, 0.18);
  color: #fbbf24;
  border-color: rgba(245, 158, 11, 0.45);
  animation: pill-pulse 1.4s ease-in-out infinite;
}
.status-pill.pill-speaking {
  background: rgba(59, 130, 246, 0.18);
  color: #60a5fa;
  border-color: rgba(59, 130, 246, 0.45);
  animation: pill-pulse 1.4s ease-in-out infinite;
}
.status-pill.pill-loading_stt {
  background: rgba(148, 163, 184, 0.15);
  color: #cbd5e1;
  border-color: rgba(148, 163, 184, 0.3);
}
.status-pill.pill-error {
  background: rgba(239, 68, 68, 0.18);
  color: #ef4444;
  border-color: rgba(239, 68, 68, 0.45);
}
@keyframes pill-pulse {
  0%, 100% { box-shadow: 0 0 0 0 currentColor; opacity: 1; }
  50%      { box-shadow: 0 0 0 4px rgba(255, 255, 255, 0); opacity: 0.85; }
}
.spinner {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 2px solid currentColor;
  border-top-color: transparent;
  animation: spin 0.8s linear infinite;
  display: inline-block;
}
@keyframes spin { to { transform: rotate(360deg); } }
.transcript {
  flex: 1;
  color: var(--text-primary, #f0f2f5);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.transcript em {
  color: var(--text-nav, #8b8fa3);
  font-style: italic;
}

.action {
  background: transparent;
  border: 1px solid var(--border-sidebar, #1e2030);
  color: var(--text-secondary, #c4c8d4);
  padding: 5px 10px;
  border-radius: 6px;
  cursor: pointer;
  font: inherit;
}
.action:disabled { opacity: 0.4; cursor: not-allowed; }
.action.danger {
  border-color: var(--accent-red, #ef4444);
  color: var(--accent-red, #ef4444);
}

.err-msg {
  color: var(--accent-red, #ef4444);
  font-size: 12px;
}
</style>
