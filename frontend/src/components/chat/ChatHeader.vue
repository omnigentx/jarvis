<script setup>
import { ref, computed } from 'vue'
import { useChatStore } from '../../stores/chat'
import { useVoiceSession } from '../../composables/useVoiceSession.js'

/**
 * ChatHeader — top strip with current agent, voice readiness chip, and
 * agent switcher. TTS playback toggle is preserved.
 *
 * Voice readiness line ("VAD ACTIVE · STT READY · TTS EDGE") is derived
 * from useVoiceSession.status so it's live, not hard-coded. Composable
 * unchanged.
 */
const props = defineProps({
  agent: { type: Object, default: null },
  agents: { type: Array, default: () => [] },
  showHamburger: { type: Boolean, default: false },
})

const emit = defineEmits(['switch-agent', 'toggle-conversations'])
const chatStore = useChatStore()
const voice = useVoiceSession()
const showDropdown = ref(false)

const initials = computed(() => {
  if (!props.agent?.name) return '?'
  return props.agent.name
    .split(/[\s_-]+/)
    .map(w => w[0]?.toUpperCase() || '')
    .join('')
    .slice(0, 2)
})

// Voice readiness mono-strip — pulls live state from the singleton.
const voiceStrip = computed(() => {
  const s = voice.status.value
  if (s === 'idle') return 'voice off'
  if (s === 'loading_stt') return 'stt loading'
  if (s === 'connecting') return 'connecting'
  if (s === 'speaking')  return 'vad on · tts streaming'
  if (s === 'thinking')  return 'transcribed · awaiting reply'
  return 'vad active · stt ready'
})

const isVoiceOn = computed(() => voice.status.value !== 'idle' && voice.status.value !== 'error')

// WS chip drives off the UPSTREAM STT WebSocket state (wsStatus), not the
// frontend↔backend ws_voice state (isVoiceOn). Reason: pre-fix, frontend
// had a healthy WS to the backend while Soniox WS was dead — chip showed
// green but mic input silently dropped (2026-05-29 incident). Now green
// ⇔ Soniox really is connected and audio is reaching it.
//
// Wire format matches STTConnectionState in
// backend/services/stt_backends/types.py:
//   'idle' (mic off)       → grey "OFF"
//   'connecting'           → amber "WS…"
//   'connected'            → green "WS"
//   'reconnecting'         → amber pulse "RECONNECT"
//   'error'                → red "WS ERR"
const wsChip = computed(() => {
  const s = voice.wsStatus?.value || 'idle'
  if (s === 'connected')    return { label: 'WS',       cls: 'chip-success', pulse: false }
  if (s === 'connecting')   return { label: 'WS…',      cls: 'chip-warning', pulse: true }
  if (s === 'reconnecting') return { label: 'RECONNECT', cls: 'chip-warning', pulse: true }
  if (s === 'error')        return { label: 'WS ERR',   cls: 'chip-danger',  pulse: false }
  return                      { label: 'OFF',      cls: 'chip-muted',   pulse: false }
})

function selectAgent(name) {
  emit('switch-agent', name)
  showDropdown.value = false
}
</script>

<template>
  <div class="hd" :class="{ 'hd-mobile': showHamburger }">
    <button
      v-if="showHamburger"
      class="hd-hamburger"
      @click="emit('toggle-conversations')"
      aria-label="Open conversations"
    >
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <line x1="2" y1="4" x2="14" y2="4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="2" y1="8" x2="14" y2="8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="2" y1="12" x2="14" y2="12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
      </svg>
    </button>

    <div class="hd-ava">{{ initials }}</div>

    <div class="hd-info">
      <div class="hd-name-row">
        <span class="hd-name">{{ agent?.name || 'No Agent' }}</span>
        <span v-if="agent" class="hd-role">· orchestrator</span>
      </div>
      <div class="hd-status-row">
        <span class="hd-dot" :class="{ on: isVoiceOn }" />
        <span class="hd-status">{{ voiceStrip }}</span>
      </div>
    </div>

    <!-- WS chip reflects upstream STT WebSocket state (not frontend↔backend). -->
    <span class="hd-chip" :class="wsChip.cls" :title="`Soniox/STT WS state: ${voice.wsStatus?.value || 'idle'}`">
      <span class="hd-chip-dot" :class="{ pulse: wsChip.pulse }" /> {{ wsChip.label }}
    </span>

    <!-- Switch agent -->
    <div class="hd-switch">
      <button class="hd-switch-btn" @click="showDropdown = !showDropdown">
        <span>Switch</span>
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
          <path d="M3 5L6 8L9 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
      <div v-if="showDropdown" class="hd-dropdown">
        <div
          v-for="a in agents"
          :key="a.name"
          class="hd-dropdown-item"
          :class="{ active: a.name === agent?.name }"
          @click="selectAgent(a.name)"
        >
          <div class="hd-mini-ava">
            {{ a.name.split(/[\s_-]+/).map(w => w[0]?.toUpperCase()).join('').slice(0,2) }}
          </div>
          <span>{{ a.name }}</span>
        </div>
      </div>
    </div>

    <!-- TTS button -->
    <button
      class="hd-tts"
      :class="{ playing: chatStore.ttsPlaying, on: chatStore.ttsEnabled }"
      :title="chatStore.ttsPlaying ? 'Stop playback' : chatStore.ttsEnabled ? 'TTS On' : 'TTS Off'"
      @click="chatStore.ttsPlaying ? chatStore.stopTts() : chatStore.toggleTts()"
    >
      <svg v-if="chatStore.ttsPlaying" width="14" height="14" viewBox="0 0 15 15" fill="none">
        <rect x="3" y="3" width="9" height="9" rx="1.5" fill="var(--danger)"/>
      </svg>
      <svg v-else-if="chatStore.ttsEnabled" width="14" height="14" viewBox="0 0 15 15" fill="none">
        <path d="M2 5.5H4L7.5 2V13L4 9.5H2C1.5 9.5 1 9 1 8.5V6.5C1 6 1.5 5.5 2 5.5Z" fill="currentColor" stroke="currentColor" stroke-width="0.8"/>
        <path d="M10 4.5C11 5.5 11.5 6.5 11.5 7.5C11.5 8.5 11 9.5 10 10.5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
      </svg>
      <svg v-else width="14" height="14" viewBox="0 0 15 15" fill="none">
        <path d="M2 5.5H4L7.5 2V13L4 9.5H2C1.5 9.5 1 9 1 8.5V6.5C1 6 1.5 5.5 2 5.5Z" stroke="currentColor" stroke-width="1.2"/>
        <path d="M10 5.5L14 9.5M14 5.5L10 9.5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
      </svg>
    </button>

    <!-- click-outside helper -->
    <teleport to="body">
      <div v-if="showDropdown" class="hd-dropdown-mask" @click="showDropdown = false"></div>
    </teleport>
  </div>
</template>

<style scoped>
.hd {
  display: flex;
  align-items: center;
  gap: 12px;
  height: 56px;
  padding: 0 20px;
  background: var(--bg-1);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.hd.hd-mobile { height: 52px; padding: 0 12px; gap: 10px; }

.hd-hamburger {
  width: 32px; height: 32px;
  display: flex; align-items: center; justify-content: center;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  color: var(--text-muted);
  cursor: pointer;
  flex-shrink: 0;
}

.hd-ava {
  width: 32px; height: 32px;
  flex-shrink: 0;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--primary), var(--accent));
  color: white;
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 600;
}

.hd-info { flex: 1; min-width: 0; }
.hd-name-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.hd-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}
.hd-role {
  font-size: 12px;
  color: var(--text-muted);
  font-weight: 400;
}

.hd-status-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 1px;
}
.hd-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--text-subtle);
  flex-shrink: 0;
}
.hd-dot.on {
  background: var(--accent);
  box-shadow: 0 0 6px var(--accent);
}
.hd-status {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.10em;
  text-transform: uppercase;
  color: var(--text-muted);
}

.hd-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 9px;
  border-radius: var(--r-full);
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.10em;
  text-transform: uppercase;
  border: 1px solid var(--border-strong);
}
.hd-chip.chip-success {
  background: var(--success-bg);
  color: var(--success);
  border-color: rgba(16, 185, 129, 0.25);
}
.hd-chip.chip-muted {
  background: var(--bg-3);
  color: var(--text-muted);
}
.hd-chip.chip-warning {
  background: var(--warning-bg);
  color: var(--warning);
  border-color: rgba(245, 158, 11, 0.30);
}
.hd-chip.chip-danger {
  background: var(--danger-bg);
  color: var(--danger);
  border-color: rgba(239, 68, 68, 0.30);
}
.hd-chip-dot {
  width: 5px; height: 5px; border-radius: 50%;
  background: currentColor;
  box-shadow: 0 0 6px currentColor;
}
.hd-chip-dot.pulse {
  animation: hd-chip-pulse 0.9s ease-in-out infinite;
}
@keyframes hd-chip-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50%      { opacity: 0.4; transform: scale(1.3); }
}

.hd-switch { position: relative; }
.hd-switch-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 5px 10px;
  background: transparent;
  border: 0;
  color: var(--text-muted);
  font-size: 11.5px;
  cursor: pointer;
  border-radius: var(--r-sm);
}
.hd-switch-btn:hover { color: var(--text); background: var(--bg-2); }
.hd-dropdown {
  position: absolute;
  top: 100%;
  right: 0;
  margin-top: 4px;
  width: 200px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  padding: 4px;
  box-shadow: var(--shadow-md);
  z-index: 50;
}
.hd-dropdown-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: var(--r-sm);
  cursor: pointer;
}
.hd-dropdown-item:hover { background: var(--bg-3); }
.hd-dropdown-item.active { background: var(--primary-bg); }
.hd-mini-ava {
  width: 22px; height: 22px;
  border-radius: 50%;
  background: var(--primary-bg-strong);
  color: var(--primary-hover);
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 600;
  display: flex; align-items: center; justify-content: center;
}
.hd-dropdown-item span {
  font-size: 12px;
  color: var(--text-dim);
}
.hd-dropdown-mask { position: fixed; inset: 0; z-index: 40; }

.hd-tts {
  width: 30px; height: 30px;
  display: flex; align-items: center; justify-content: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--r-sm);
  color: var(--text-muted);
  cursor: pointer;
  transition: all 0.15s var(--ease-out);
}
.hd-tts:hover { color: var(--text); background: var(--bg-2); }
.hd-tts.on {
  background: var(--success-bg);
  border-color: rgba(16, 185, 129, 0.25);
  color: var(--success);
}
.hd-tts.playing {
  background: var(--danger-bg);
  border-color: rgba(239, 68, 68, 0.30);
  color: var(--danger);
}

/* Mobile: shed non-essential header chrome so the agent name has
   room to breathe. Role label ("· orchestrator") and the WS/OFF chip
   are nice-to-have; they're redundant with the dot + Switch button
   that stay visible. */
@media (max-width: 480px) {
  .hd-role { display: none; }
  .hd-chip { display: none; }
  .hd-status { font-size: 9.5px; letter-spacing: 0.08em; }
}
@media (max-width: 380px) {
  /* At iPhone Mini width the status row also has to go — TTS button
     + Switch button + dot + name is already a tight row. */
  .hd-status-row { display: none; }
}
</style>
