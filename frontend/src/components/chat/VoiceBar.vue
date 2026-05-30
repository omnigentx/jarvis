<script setup>
/**
 * VoiceBar — single-strip hands-free toggle.
 *
 * Sits between ChatHeader and ChatMessages. The bar shows mic toggle,
 * status pill, live transcript, and an Interrupt button while the agent
 * is talking. Conversation messages themselves are NOT rendered here —
 * they go straight into the normal chat message panel via chatStore
 * (addUserMessage / addAgentMessagePlaceholder / finalizeAgentMessage),
 * so voice and typed turns share one UI.
 */
import { computed } from 'vue'
import { useVoiceSession } from '../../composables/useVoiceSession.js'

const session = useVoiceSession()

// NOTE — no onBeforeUnmount stop() here. The voice session is a module-level
// singleton (see useVoiceSession.js) and the host route is kept alive across
// nav (AppLayout's <keep-alive>), so the only legitimate teardown triggers
// are: user stop, auth expiry, browser tab close. Auto-stopping on
// component unmount used to fire on every route change.

const STATUS_LABELS = {
  idle: 'Off',
  connecting: 'Connecting…',
  loading_stt: 'Loading STT model (first run ~30s)…',
  listening: 'VAD active',
  thinking: 'Transcribing…',
  speaking: 'Speaking',
  error: 'Error',
}
const statusLabel = computed(() => STATUS_LABELS[session.status.value] || session.status.value)
const isOn = computed(() => session.status.value !== 'idle' && session.status.value !== 'error')
const canInterrupt = computed(() =>
  session.status.value === 'thinking' || session.status.value === 'speaking',
)
const isInterrupting = computed(() => session.wasInterrupted.value)

async function toggle() {
  if (isOn.value) await session.stop()
  else await session.start()
}
</script>

<template>
  <div class="voice-bar" :class="{ on: isOn, error: session.status.value === 'error', interrupting: isInterrupting }">
    <button
      class="mic-btn"
      :class="{ active: isOn }"
      type="button"
      :title="isOn ? 'Stop hands-free' : 'Start hands-free'"
      @click="toggle"
    >
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <rect x="5.5" y="1.5" width="5" height="8.5" rx="2.5" stroke="currentColor" stroke-width="1.4"/>
        <path d="M3 8c0 2.8 2.2 5 5 5s5-2.2 5-5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
        <line x1="8" y1="13" x2="8" y2="15" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
      </svg>
      <span class="mic-label">{{ isOn ? 'Stop' : 'Mic' }}</span>
    </button>

    <span
      class="status-pill"
      :class="`pill-${session.status.value}`"
      :title="`Voice status: ${statusLabel}`"
    >
      <span v-if="canInterrupt" class="status-dot pulse" />
      <span v-else-if="isOn" class="status-dot" />
      {{ statusLabel }}
    </span>

    <span v-if="session.partialTranscript.value || session.lastFinalTranscript.value" class="transcript">
      <template v-if="session.partialTranscript.value">{{ session.partialTranscript.value }}</template>
      <template v-else>"{{ session.lastFinalTranscript.value }}"</template>
    </span>

    <button
      v-if="canInterrupt"
      type="button"
      class="interrupt-btn"
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
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-1);
  font-size: 12.5px;
  min-height: 56px;
}
.voice-bar.on {
  background: linear-gradient(to bottom, var(--primary-bg), var(--bg-1));
}
.voice-bar.error {
  background: var(--danger-bg);
}

.mic-btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  color: var(--text-dim);
  padding: 7px 14px;
  border-radius: var(--r-full);
  cursor: pointer;
  font: inherit;
  font-size: 12.5px;
  font-weight: 500;
  transition: all 0.18s var(--ease-out);
  flex-shrink: 0;
}
.mic-btn:hover {
  border-color: var(--border-bright);
  color: var(--text);
}
.mic-btn.active {
  background: var(--primary);
  border-color: transparent;
  color: white;
  box-shadow: 0 0 16px var(--primary-glow);
}
.mic-btn svg { flex-shrink: 0; }
.mic-label { font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; }

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: var(--r-full);
  font-family: var(--font-mono);
  font-size: 10.5px;
  letter-spacing: 0.10em;
  text-transform: uppercase;
  white-space: nowrap;
  border: 1px solid var(--border-strong);
  background: var(--bg-2);
  color: var(--text-muted);
  flex-shrink: 0;
}
.status-pill.pill-listening {
  background: var(--success-bg);
  color: var(--success);
  border-color: rgba(16, 185, 129, 0.25);
}
.status-pill.pill-thinking {
  background: var(--warning-bg);
  color: var(--warning);
  border-color: rgba(245, 158, 11, 0.30);
}
.status-pill.pill-speaking {
  background: var(--primary-bg);
  color: var(--primary-hover);
  border-color: rgba(99, 102, 241, 0.30);
}
.status-pill.pill-connecting,
.status-pill.pill-loading_stt {
  background: var(--bg-3);
  color: var(--text-dim);
}
.status-pill.pill-error {
  background: var(--danger-bg);
  color: var(--danger);
  border-color: rgba(239, 68, 68, 0.30);
}
.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}
.status-dot.pulse {
  animation: voicebar-pulse 0.9s ease-in-out infinite;
}
@keyframes voicebar-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50%      { opacity: 0.5; transform: scale(1.3); }
}

.transcript {
  flex: 1;
  min-width: 0;
  color: var(--text-dim);
  font-size: 12.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.interrupt-btn {
  background: transparent;
  border: 1px solid var(--danger);
  color: var(--danger);
  padding: 5px 12px;
  border-radius: var(--r-md);
  cursor: pointer;
  font: inherit;
  font-size: 11.5px;
  font-weight: 500;
  flex-shrink: 0;
  transition: all 0.18s var(--ease-out);
}
.interrupt-btn:hover {
  background: var(--danger-bg);
}

.err-msg {
  color: var(--danger);
  font-size: 11.5px;
  flex-shrink: 0;
}

@media (max-width: 640px) {
  .voice-bar { gap: 8px; padding: 8px 12px; flex-wrap: wrap; min-height: 0; }
  .transcript { flex: 1 1 100%; white-space: normal; order: 5; }
  .voice-bar:not(.on) { padding: 6px 12px; }
}
</style>
