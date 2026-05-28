<script setup>
import { ref, computed, nextTick } from 'vue'
import { useBreakpoint } from '../../composables/useBreakpoint'

/**
 * ChatInput — restyled compose row.
 *
 * Layout: text input + attach (image/audio) + mic toggle + send.
 * Tokens used: var(--bg-1) container, var(--bg-2) compose row,
 * var(--primary) send button. Mic recording state animates pulse.
 *
 * Audio capture logic unchanged — same MediaRecorder approach the user
 * already relies on; we just restyled the button states.
 */

const props = defineProps({
  isStreaming: { type: Boolean, default: false },
})

const emit = defineEmits(['send', 'stop'])
const inputText = ref('')
const inputRef = ref(null)
const attachedFiles = ref([])
const isRecording = ref(false)
const mediaRecorder = ref(null)
const audioChunks = ref([])
const fileInputRef = ref(null)
const { isMobile } = useBreakpoint()

const filePreviews = computed(() =>
  attachedFiles.value.map(file => ({
    file,
    name: file.name,
    type: file.type,
    isImage: file.type.startsWith('image/'),
    isAudio: file.type.startsWith('audio/'),
    url: file.type.startsWith('image/') ? URL.createObjectURL(file) : null,
  }))
)

function handleStop(mode) {
  emit('stop', { mode })
}

function handleSend() {
  const text = inputText.value.trim()
  const files = attachedFiles.value
  if ((!text && !files.length) || props.isStreaming) return
  emit('send', { text, files: files.length ? [...files] : null })
  inputText.value = ''
  attachedFiles.value = []
  nextTick(() => {
    if (inputRef.value) inputRef.value.style.height = '24px'
  })
  inputRef.value?.focus()
}

function autoGrow() {
  const el = inputRef.value
  if (!el) return
  el.style.height = '24px'
  el.style.height = Math.min(el.scrollHeight, 200) + 'px'
}

function handleKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    handleSend()
  }
}

function triggerFilePicker() { fileInputRef.value?.click() }

function handleFileSelected(e) {
  const files = Array.from(e.target.files || [])
  for (const file of files) attachedFiles.value.push(file)
  e.target.value = ''
}

function handlePaste(e) {
  // Only intercept when the clipboard carries a file (e.g. screenshot copy).
  // Plain-text paste must fall through so the textarea handles it natively.
  const items = e.clipboardData?.items
  if (!items || items.length === 0) return
  const pasted = []
  for (const item of items) {
    if (item.kind === 'file') {
      const file = item.getAsFile()
      if (file) pasted.push(file)
    }
  }
  if (pasted.length === 0) return
  e.preventDefault()
  for (const file of pasted) attachedFiles.value.push(file)
}

function removeFile(index) {
  const preview = filePreviews.value[index]
  if (preview?.url) URL.revokeObjectURL(preview.url)
  attachedFiles.value.splice(index, 1)
}

async function toggleRecording() {
  if (isRecording.value) {
    mediaRecorder.value?.stop()
    isRecording.value = false
    return
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    const recorder = new MediaRecorder(stream, { mimeType: 'audio/webm' })
    audioChunks.value = []
    recorder.ondataavailable = (e) => {
      if (e.data.size > 0) audioChunks.value.push(e.data)
    }
    recorder.onstop = () => {
      const blob = new Blob(audioChunks.value, { type: 'audio/webm' })
      const file = new File([blob], `recording_${Date.now()}.webm`, { type: 'audio/webm' })
      attachedFiles.value.push(file)
      stream.getTracks().forEach(t => t.stop())
    }
    recorder.start()
    mediaRecorder.value = recorder
    isRecording.value = true
  } catch (err) {
    console.error('[ChatInput] Microphone access denied:', err)
    alert('Microphone access denied. Please allow microphone access in your browser settings.')
  }
}
</script>

<template>
  <div class="compose-host" :class="{ 'is-mobile': isMobile }">
    <!-- File previews -->
    <div v-if="filePreviews.length" class="previews">
      <div
        v-for="(preview, idx) in filePreviews"
        :key="idx"
        class="preview-chip"
        :class="{ 'preview-image': preview.isImage }"
      >
        <img v-if="preview.isImage && preview.url" :src="preview.url" class="preview-img" />
        <div v-else-if="preview.isAudio" class="preview-audio">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
            <rect x="5.5" y="1" width="5" height="9" rx="2.5" stroke="var(--success)" stroke-width="1.3"/>
            <path d="M3 8C3 11 5.5 13 8 13C10.5 13 13 11 13 8" stroke="var(--success)" stroke-width="1.3" stroke-linecap="round"/>
          </svg>
          <span>{{ preview.name }}</span>
        </div>
        <span v-else class="preview-name">{{ preview.name }}</span>
        <button class="preview-remove" @click="removeFile(idx)" aria-label="Remove">
          <svg width="8" height="8" viewBox="0 0 8 8" fill="none">
            <path d="M1 1L7 7M7 1L1 7" stroke="white" stroke-width="1.5" stroke-linecap="round"/>
          </svg>
        </button>
      </div>
    </div>

    <!-- Compose row -->
    <div class="compose-row">
      <input
        ref="fileInputRef"
        type="file"
        accept="image/*,audio/*"
        multiple
        style="display: none;"
        @change="handleFileSelected"
      />

      <!-- Attach buttons -->
      <button
        class="icon-btn"
        :class="{ active: attachedFiles.some(f => f.type.startsWith('image/')) }"
        title="Attach image"
        @click="triggerFilePicker"
      >
        <svg width="16" height="16" viewBox="0 0 16 14" fill="none">
          <rect x="1" y="1" width="14" height="12" rx="2" stroke="currentColor" stroke-width="1.3"/>
          <circle cx="5" cy="5" r="1.5" stroke="currentColor" stroke-width="1.2"/>
          <path d="M1 11L5 7L8 10L10 8L15 11" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>

      <button
        class="icon-btn"
        :class="{ recording: isRecording }"
        :title="isRecording ? 'Stop recording' : 'Voice input (record)'"
        @click="toggleRecording"
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
          <rect x="5.5" y="1" width="5" height="9" rx="2.5" stroke="currentColor" stroke-width="1.3"/>
          <path d="M3 8C3 11 5.5 13 8 13C10.5 13 13 11 13 8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
          <line x1="8" y1="13" x2="8" y2="15" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
        </svg>
      </button>

      <!-- Text input -->
      <div class="textarea-wrap">
        <textarea
          ref="inputRef"
          v-model="inputText"
          placeholder="Type a message, paste image (⌘V), or hit mic — Shift+⏎ for new line"
          :disabled="isStreaming"
          rows="1"
          class="textarea"
          @keydown="handleKeydown"
          @input="autoGrow"
          @paste="handlePaste"
        />
      </div>

      <!-- Stop button replaces Send while streaming. Single click = soft
           interrupt (cancel LLM, leave subagents alone); shift-click =
           hard kill (SIGTERM subagents; side effects may have committed). -->
      <button
        v-if="isStreaming"
        class="send-btn stop-btn"
        aria-label="Stop generation"
        data-testid="chat-stop"
        title="Stop generation. Tools already called are NOT undone. Shift-click to also force-terminate running subagents."
        @click.exact="handleStop('soft')"
        @click.shift="handleStop('hard')"
      >
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
          <rect x="2" y="2" width="10" height="10" rx="1.5" fill="white"/>
        </svg>
      </button>
      <button
        v-else
        class="send-btn"
        aria-label="Send message"
        data-testid="chat-send"
        @click="handleSend"
      >
        <svg width="16" height="16" viewBox="0 0 18 18" fill="none">
          <path d="M2 9L16 2L9 16L8 10L2 9Z" fill="white" stroke="white" stroke-width="1.2" stroke-linejoin="round"/>
        </svg>
      </button>
    </div>
  </div>
</template>

<style scoped>
.compose-host {
  flex-shrink: 0;
  background: var(--bg-1);
  border-top: 1px solid var(--border);
  padding: 12px 24px 16px;
}
.compose-host.is-mobile { padding: 10px 12px 12px; }

.previews {
  display: flex;
  gap: 8px;
  padding-bottom: 8px;
  overflow-x: auto;
}

.preview-chip {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  padding: 6px 12px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-dim);
  flex-shrink: 0;
}
.preview-chip.preview-image { padding: 4px; }
.preview-img { width: 48px; height: 48px; border-radius: var(--r-sm); object-fit: cover; }
.preview-audio { display: flex; align-items: center; gap: 6px; }
.preview-name {
  font-size: 11px;
  /* Cap long filenames so a 200-char path doesn't push the chip past
     the X-button. Truncation lets the close target stay reachable. */
  max-width: 140px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.preview-remove {
  width: 18px; height: 18px;
  border-radius: 50%;
  background: var(--danger);
  border: 0;
  padding: 0;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer;
  position: absolute;
  top: -6px;
  right: -6px;
}
/* Generous invisible tap hit-area around the 18px X button — keeps the
   visual chip small but lifts the touch target to ~32×32. */
.preview-remove::before {
  content: '';
  position: absolute;
  inset: -8px;
}
.preview-chip:not(.preview-image) .preview-remove {
  position: relative;
  top: auto; right: auto;
}

.compose-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  min-height: 48px;
}

.icon-btn {
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--r-md);
  color: var(--text-muted);
  cursor: pointer;
  transition: all 0.15s var(--ease-out);
}
.icon-btn:hover { background: var(--bg-3); color: var(--text-dim); }
.icon-btn.active {
  background: var(--primary-bg);
  color: var(--primary-hover);
  border-color: var(--primary-bg-strong);
}
.icon-btn.recording {
  background: var(--danger-bg);
  color: var(--danger);
  border-color: rgba(239, 68, 68, 0.30);
  animation: input-rec-pulse 1.5s ease-in-out infinite;
}
@keyframes input-rec-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }

.textarea-wrap {
  flex: 1;
  display: flex;
  align-items: center;
  padding: 0 6px;
}
.textarea {
  flex: 1;
  background: transparent;
  border: 0;
  outline: 0;
  resize: none;
  font: inherit;
  font-family: var(--font-body);
  font-size: 13.5px;
  color: var(--text);
  line-height: 1.5;
  max-height: 200px;
  height: 24px;
  overflow: auto;
  width: 100%;
}
.textarea::placeholder { color: var(--text-muted); }
.textarea:disabled { opacity: 0.5; }

.send-btn {
  width: 36px;
  height: 36px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(180deg, var(--primary-hover), var(--primary));
  border: 0;
  border-radius: var(--r-md);
  cursor: pointer;
  box-shadow: 0 1px 0 rgba(255,255,255,0.18) inset, 0 8px 24px -8px var(--primary-glow);
  transition: transform 0.15s var(--ease-out);
}
.send-btn:hover:not(:disabled) { transform: translateY(-1px); }
.send-btn:disabled { opacity: 0.4; cursor: not-allowed; box-shadow: none; }
/* Stop variant — danger-tinted gradient so it reads as "destructive" but
   stays in the same slot/size so the layout doesn't shift on send→stop. */
.stop-btn {
  background: linear-gradient(180deg, #F87171, var(--danger));
  box-shadow: 0 1px 0 rgba(255,255,255,0.18) inset, 0 8px 24px -8px rgba(239, 68, 68, 0.55);
  animation: stop-btn-pulse 1.4s ease-in-out infinite;
}
@keyframes stop-btn-pulse {
  0%, 100% { box-shadow: 0 1px 0 rgba(255,255,255,0.18) inset, 0 8px 24px -8px rgba(239, 68, 68, 0.55); }
  50%      { box-shadow: 0 1px 0 rgba(255,255,255,0.18) inset, 0 8px 32px -6px rgba(239, 68, 68, 0.75); }
}

/* Mobile: bump icon/send buttons to the WCAG/iOS 40px tap-target floor.
   Without this the composer was sub-44px across mic/attach/send and
   mis-taps were common on phones. */
@media (max-width: 767px) {
  .icon-btn { width: 40px; height: 40px; }
  .send-btn { width: 42px; height: 42px; }
  .compose-row { min-height: 52px; }
}
</style>
