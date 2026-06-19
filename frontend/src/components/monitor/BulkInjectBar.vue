<script setup>
/**
 * BulkInjectBar — bottom bar that injects a message to ALL selected
 * agents in one shot. Supports:
 *   - multiline text
 *   - ⌘V image paste
 *   - file attach button
 *   - singleton mic (shared with chat via useVoiceSession)
 *   - send button (POST /api/agents/{name}/inject in parallel)
 *
 * Parent passes:
 *   - `selectedNames`: agents to inject into
 *   - `onSubmit({ text, files })`: returns Promise per-agent (parent fans out)
 *
 * Voice mic singleton: we don't reach into /ws/voice ourselves here —
 * just call useVoiceSession() so the shared state stays consistent with
 * the chat view's mic button (only one mic active globally).
 */
import { ref, computed, watch } from 'vue'
import { useVoiceSession } from '../../composables/useVoiceSession.js'
import { useLang } from '../../composables/useLang'

const { t } = useLang()

const props = defineProps({
  selectedNames: { type: Array, default: () => [] },
  onSubmit: { type: Function, default: null },
})

const text = ref('')
const files = ref([])
const busy = ref(false)
const feedback = ref('')

const voice = useVoiceSession()

const micActive = computed(() => {
  const s = voice?.status?.value
  return s === 'listening' || s === 'thinking' || s === 'speaking'
})

// When voice produces a final transcript, append it to the text box so
// the user can review before pressing Send. This avoids the trap of
// "mic spoke -> immediately injected" without a confirmation step.
watch(
  () => voice?.lastFinalTranscript?.value,
  (transcript) => {
    if (transcript) {
      text.value = (text.value ? text.value + ' ' : '') + transcript
    }
  },
)

async function toggleMic() {
  if (!voice) return
  if (micActive.value) {
    await voice.stop?.()
  } else {
    try {
      await voice.start?.()
    } catch (e) {
      feedback.value = t('bulkInject.micError', { msg: e?.message || String(e) })
      setTimeout(() => (feedback.value = ''), 4000)
    }
  }
}

function chooseFile() {
  const input = document.createElement('input')
  input.type = 'file'
  input.multiple = true
  input.onchange = (e) => {
    files.value = [...files.value, ...Array.from(e.target.files)]
  }
  input.click()
}

function removeFile(i) {
  files.value = files.value.filter((_, idx) => idx !== i)
}

// ⌘V image paste — listen on the bar, not globally, to avoid stealing
// paste events from any other input in the page.
function onPaste(e) {
  const items = e.clipboardData?.items
  if (!items) return
  for (const item of items) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const file = item.getAsFile()
      if (file) files.value.push(file)
      e.preventDefault()
    }
  }
}

async function submit() {
  const t = text.value.trim()
  if (!t && !files.value.length) return
  if (!props.selectedNames.length) {
    feedback.value = t('bulkInject.noAgents')
    setTimeout(() => (feedback.value = ''), 3000)
    return
  }
  busy.value = true
  feedback.value = ''
  try {
    const payload = { text: t, files: files.value }
    // Parent decides how to fan out — we just pass the payload.
    const results = await props.onSubmit?.(payload)
    text.value = ''
    files.value = []
    const okCount = Array.isArray(results)
      ? results.filter(r => r?.status === 'fulfilled').length
      : props.selectedNames.length
    feedback.value = t('bulkInject.injectedTo', { ok: okCount, total: props.selectedNames.length })
    setTimeout(() => (feedback.value = ''), 4000)
  } catch (e) {
    feedback.value = t('bulkInject.error', { msg: e?.message || String(e) })
    setTimeout(() => (feedback.value = ''), 5000)
  } finally {
    busy.value = false
  }
}

function fileSize(f) {
  const b = f.size || 0
  if (b < 1024) return `${b} B`
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`
  return `${(b / 1024 / 1024).toFixed(1)} MB`
}

const canSubmit = computed(
  () => !busy.value && (text.value.trim() || files.value.length) && props.selectedNames.length,
)
</script>

<template>
  <div class="bulk-inject" :class="{ 'mic-active': micActive }" @paste="onPaste">
    <!-- Header line: target chip + voice status -->
    <div class="bi-header">
      <span class="bi-mono">{{ t('bulkInject.injectTo') }}</span>
      <span class="bi-target-pill">{{ t('bulkInject.agentCount', { n: selectedNames.length }) }}</span>
      <span v-if="micActive" class="bi-mic-status">
        <span class="bi-mic-dot" />
        {{ t('bulkInject.micShared') }}
      </span>
      <span v-if="feedback" class="bi-feedback">{{ feedback }}</span>
    </div>

    <!-- File chips (above the row) -->
    <div v-if="files.length" class="bi-chips">
      <span v-for="(f, i) in files" :key="i" class="bi-chip">
        <span class="bi-chip-thumb">
          {{ f.type?.startsWith('image/') ? '🖼' : f.type?.startsWith('audio/') ? '🎤' : '📄' }}
        </span>
        <span class="bi-chip-name">{{ f.name }}</span>
        <span class="bi-chip-size">{{ fileSize(f) }}</span>
        <button class="bi-chip-x" type="button" @click="removeFile(i)" :aria-label="t('bulkInject.removeFile')">×</button>
      </span>
    </div>

    <!-- Input row -->
    <div class="bi-row">
      <!-- Icons synced with ChatInput.vue: same outline SVGs, same 34×34
           rounded-square shell, same attach-then-mic order. -->
      <button
        class="bi-icon-btn"
        type="button"
        :title="t('bulkInject.attachFile')"
        @click="chooseFile"
      >
        <svg width="16" height="16" viewBox="0 0 16 14" fill="none">
          <rect x="1" y="1" width="14" height="12" rx="2" stroke="currentColor" stroke-width="1.3"/>
          <circle cx="5" cy="5" r="1.5" stroke="currentColor" stroke-width="1.2"/>
          <path d="M1 11L5 7L8 10L10 8L15 11" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>

      <button
        class="bi-icon-btn bi-mic-btn"
        :class="{ active: micActive }"
        type="button"
        :aria-pressed="micActive"
        :title="micActive ? t('bulkInject.stopVoice') : t('bulkInject.voiceInput')"
        @click="toggleMic"
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
          <rect x="5.5" y="1" width="5" height="9" rx="2.5" stroke="currentColor" stroke-width="1.3"/>
          <path d="M3 8C3 11 5.5 13 8 13C10.5 13 13 11 13 8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
          <line x1="8" y1="13" x2="8" y2="15" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
        </svg>
      </button>

      <textarea
        v-model="text"
        class="bi-textarea"
        rows="1"
        :placeholder="t('bulkInject.textareaPlaceholder')"
        :disabled="busy"
        @keydown.enter.exact.prevent="submit"
      />

      <button
        class="bi-send"
        type="button"
        :disabled="!canSubmit"
        :title="t('bulkInject.sendTitle', { n: selectedNames.length })"
        @click="submit"
      >
        <span v-if="busy">…</span>
        <span v-else>{{ t('common.send') }} <span class="bi-send-count">{{ selectedNames.length }}</span></span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.bulk-inject {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  transition: border-color 0.18s var(--ease-out), box-shadow 0.18s var(--ease-out);
}
.bulk-inject.mic-active {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent-bg), 0 0 24px var(--shadow-glow-cyan);
}

.bi-header {
  display: flex;
  align-items: center;
  gap: 10px;
  font-family: var(--font-mono);
  font-size: 11px;
}
.bi-mono {
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-muted);
}
.bi-target-pill {
  padding: 2px 8px;
  border-radius: var(--r-full);
  background: var(--primary-bg);
  color: var(--primary-hover);
  font-family: var(--font-mono);
  font-size: 11px;
  border: 1px solid var(--primary-bg-strong);
}
.bi-mic-status {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--accent);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.bi-mic-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 8px var(--accent);
  animation: bimicPulse 1.4s ease-in-out infinite;
}
@keyframes bimicPulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50%      { opacity: 0.4; transform: scale(1.3); }
}
.bi-feedback {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-dim);
  font-style: italic;
}

/* Attachment chips */
.bi-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.bi-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 6px 3px 4px;
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  font-size: 11px;
  color: var(--text);
}
.bi-chip-thumb {
  width: 22px; height: 22px;
  border-radius: 4px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-4);
  font-size: 12px;
}
.bi-chip-name {
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bi-chip-size { color: var(--text-muted); font-family: var(--font-mono); font-size: 10px; }
.bi-chip-x {
  background: transparent;
  border: 0;
  color: var(--text-muted);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
  padding: 0 2px;
}
.bi-chip-x:hover { color: var(--danger); }

/* Input row */
.bi-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
}
.mic-active .bi-row { border-color: var(--accent); }

/* Icon buttons — mirrors `.icon-btn` in ChatInput.vue: 34×34 rounded
   square, transparent shell, muted-text glyph, subtle bg-3 hover. Kept
   in sync so attach + mic look identical in both composers. */
.bi-icon-btn {
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--r-md);
  color: var(--text-muted);
  cursor: pointer;
  transition: all 0.15s var(--ease-out);
}
.bi-icon-btn:hover { background: var(--bg-3); color: var(--text-dim); }
.bi-mic-btn.active {
  background: var(--accent-bg);
  color: var(--accent);
  border-color: rgba(34, 211, 238, 0.30);
}

.bi-textarea {
  flex: 1;
  background: transparent;
  border: 0;
  outline: 0;
  color: var(--text);
  font-family: var(--font-body);
  font-size: 13px;
  resize: vertical;
  min-height: 24px;
  max-height: 120px;
  line-height: 1.5;
  padding: 4px 0;
}
.bi-textarea::placeholder { color: var(--text-subtle); }

.bi-send {
  height: 32px;
  padding: 0 14px;
  border-radius: var(--r-sm);
  background: linear-gradient(180deg, var(--primary-hover), var(--primary));
  color: white;
  border: 0;
  font-weight: 600;
  font-size: 12.5px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  transition: filter 0.15s var(--ease-out);
}
.bi-send:hover:not(:disabled) { filter: brightness(1.08); }
.bi-send:disabled {
  background: var(--bg-3);
  color: var(--text-subtle);
  cursor: not-allowed;
}
.bi-send-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: var(--r-full);
  background: rgba(255, 255, 255, 0.2);
  font-family: var(--font-mono);
  font-size: 10px;
}

@media (max-width: 767px) {
  .bulk-inject { padding: 10px; }
  .bi-row { padding: 4px 6px; gap: 6px; }
  /* WCAG/iOS 40px tap-target floor across all touch surfaces. */
  .bi-icon-btn { width: 40px; height: 40px; }
  .bi-send { padding: 0 12px; font-size: 12px; height: 40px; }
  .bi-textarea { font-size: 12.5px; }
}
@media (max-width: 480px) {
  /* When mic-active + feedback both push margin-left:auto they
     collide on narrow widths. Allow header to wrap so each can land
     on its own line if needed. */
  .bi-header { flex-wrap: wrap; }
  .bi-mic-status, .bi-feedback { margin-left: 0; }
  .bi-chip-x { width: 24px; height: 24px; }
}
@media (max-width: 380px) {
  /* The "Send N" count chip overflows when there are 3+ digits on
     iPhone Mini-width. Drop the chip and let the textual label carry
     the action affordance. */
  .bi-send-count { display: none; }
}
</style>
