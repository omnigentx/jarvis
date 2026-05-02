<script setup>
/**
 * VoiceBar — hands-free toggle + live transcript strip.
 *
 * Sits between ChatHeader and ChatMessages. Tapping the mic opens
 * /ws/voice, captures the mic via AudioWorklet, and renders incoming
 * partial transcripts live. When the agent replies, audio plays back
 * through Web Audio API automatically. Tapping again (or "Stop") tears
 * everything down.
 *
 * The "Send last" button takes whatever final transcript came back from
 * STT and asks the server to TTS-stream a reply for it — useful for
 * quickly probing the speak() path without typing.
 */
import { computed } from 'vue'
import { useVoiceSession } from '../../composables/useVoiceSession.js'

const session = useVoiceSession()

const statusLabel = computed(() => ({
  idle: 'Off',
  connecting: 'Connecting…',
  listening: 'Listening',
  speaking: 'Speaking',
  error: 'Error',
}[session.status.value] || session.status.value))

const isOn = computed(() => session.status.value !== 'idle' && session.status.value !== 'error')

async function toggle() {
  if (isOn.value) await session.stop()
  else await session.start()
}
</script>

<template>
  <div class="voice-bar" :class="{ on: isOn, error: session.status.value === 'error' }">
    <button class="mic" type="button" @click="toggle" :title="isOn ? 'Stop hands-free' : 'Start hands-free'">
      <span class="dot" />
      <span class="label">{{ isOn ? 'Stop' : 'Mic' }}</span>
    </button>
    <span class="status">{{ statusLabel }}</span>
    <span class="transcript" :title="session.lastFinalTranscript.value">
      <template v-if="session.partialTranscript.value">{{ session.partialTranscript.value }}</template>
      <template v-else-if="session.lastFinalTranscript.value">"{{ session.lastFinalTranscript.value }}"</template>
      <template v-else><em>Speak after enabling the mic — partial transcripts appear here.</em></template>
    </span>
    <button v-if="isOn" class="action" type="button" :disabled="!session.lastFinalTranscript.value" @click="session.speak(session.lastFinalTranscript.value)">
      Re-speak
    </button>
    <button v-if="session.status.value === 'speaking'" class="action danger" type="button" @click="session.bargeIn()">
      Interrupt
    </button>
    <span v-if="session.error.value" class="err-msg">{{ session.error.value }}</span>
  </div>
</template>

<style scoped>
.voice-bar {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--border-sidebar, #1e2030);
  background: var(--bg-sidebar, #0e1020);
  font-size: 13px;
}
.voice-bar.on { background: rgba(59, 130, 246, 0.05); }
.voice-bar.error { background: rgba(239, 68, 68, 0.08); }
.mic {
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--bg-card, #161826); border: 1px solid var(--border-sidebar, #1e2030);
  color: var(--text-primary, #f0f2f5); padding: 6px 12px; border-radius: 999px; cursor: pointer;
  font: inherit;
}
.mic .dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--text-nav, #8b8fa3);
}
.voice-bar.on .mic .dot { background: var(--accent-red, #ef4444); animation: pulse 1.2s ease-in-out infinite; }
@keyframes pulse { 50% { transform: scale(1.4); opacity: 0.6; } }
.status {
  font-weight: 500; color: var(--text-secondary, #c4c8d4);
  min-width: 80px;
}
.transcript {
  flex: 1; color: var(--text-primary, #f0f2f5);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.transcript em { color: var(--text-nav, #8b8fa3); font-style: italic; }
.action {
  background: transparent; border: 1px solid var(--border-sidebar, #1e2030);
  color: var(--text-secondary, #c4c8d4); padding: 5px 10px; border-radius: 6px; cursor: pointer; font: inherit;
}
.action:disabled { opacity: 0.4; cursor: not-allowed; }
.action.danger { border-color: var(--accent-red, #ef4444); color: var(--accent-red, #ef4444); }
.err-msg { color: var(--accent-red, #ef4444); font-size: 12px; }
</style>
