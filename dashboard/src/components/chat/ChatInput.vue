<script setup>
import { ref, computed, nextTick } from 'vue'
import { useBreakpoint } from '../../composables/useBreakpoint'

const props = defineProps({
  isStreaming: { type: Boolean, default: false },
})

const emit = defineEmits(['send'])
const inputText = ref('')
const inputRef = ref(null)
const attachedFiles = ref([])
const isRecording = ref(false)
const mediaRecorder = ref(null)
const audioChunks = ref([])
const fileInputRef = ref(null)
const { isMobile } = useBreakpoint()

// File previews with object URLs
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

function handleSend() {
  const text = inputText.value.trim()
  const files = attachedFiles.value
  if ((!text && !files.length) || props.isStreaming) return
  emit('send', { text, files: files.length ? [...files] : null })
  inputText.value = ''
  attachedFiles.value = []
  // Reset textarea height
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

// --- Image picker ---
function triggerFilePicker() {
  fileInputRef.value?.click()
}

function handleFileSelected(e) {
  const files = Array.from(e.target.files || [])
  for (const file of files) {
    attachedFiles.value.push(file)
  }
  // Reset input so same file can be selected again
  e.target.value = ''
}

function removeFile(index) {
  const preview = filePreviews.value[index]
  if (preview?.url) URL.revokeObjectURL(preview.url)
  attachedFiles.value.splice(index, 1)
}

// --- Audio recorder ---
async function toggleRecording() {
  if (isRecording.value) {
    // Stop recording
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
      // Stop all tracks
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
  <div
    class="shrink-0"
    :style="{
      background: 'var(--bg-sidebar)',
      borderTop: '1px solid var(--border-sidebar)',
      padding: isMobile ? '0 12px' : '0 24px',
    }"
  >
    <!-- File previews -->
    <div
      v-if="filePreviews.length"
      class="flex items-center"
      :style="{
        gap: '8px',
        padding: '10px 0 4px 0',
        overflowX: 'auto',
      }"
    >
      <div
        v-for="(preview, idx) in filePreviews"
        :key="idx"
        class="relative shrink-0 flex items-center"
        :style="{
          background: '#111318',
          border: '1px solid var(--border-input)',
          borderRadius: '10px',
          padding: preview.isImage ? '4px' : '6px 12px',
          gap: '6px',
        }"
      >
        <!-- Image preview thumbnail -->
        <img
          v-if="preview.isImage && preview.url"
          :src="preview.url"
          :style="{
            width: '48px',
            height: '48px',
            borderRadius: '8px',
            objectFit: 'cover',
          }"
        />
        <!-- Audio file tag -->
        <div v-else-if="preview.isAudio" class="flex items-center" style="gap: 6px;">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
            <rect x="5.5" y="1" width="5" height="9" rx="2.5" stroke="#10b981" stroke-width="1.3"/>
            <path d="M3 8C3 11 5.5 13 8 13C10.5 13 13 11 13 8" stroke="#10b981" stroke-width="1.3" stroke-linecap="round"/>
          </svg>
          <span style="font-size: 11px; color: var(--text-secondary);">{{ preview.name }}</span>
        </div>
        <!-- Other file -->
        <span v-else style="font-size: 11px; color: var(--text-secondary);">{{ preview.name }}</span>
        <!-- Remove button -->
        <button
          class="flex items-center justify-center cursor-pointer"
          :style="{
            position: preview.isImage ? 'absolute' : 'relative',
            top: preview.isImage ? '-4px' : 'auto',
            right: preview.isImage ? '-4px' : 'auto',
            width: '16px',
            height: '16px',
            borderRadius: '50%',
            background: '#ef4444',
            border: 'none',
            padding: '0',
          }"
          @click="removeFile(idx)"
        >
          <svg width="8" height="8" viewBox="0 0 8 8" fill="none">
            <path d="M1 1L7 7M7 1L1 7" stroke="white" stroke-width="1.5" stroke-linecap="round"/>
          </svg>
        </button>
      </div>
    </div>

    <!-- Input row -->
    <div
      class="flex items-end"
      :style="{
        minHeight: '64px',
        padding: '12px 0',
        gap: '12px',
      }"
    >
      <!-- Hidden file input -->
      <input
        ref="fileInputRef"
        type="file"
        accept="image/*,audio/*"
        multiple
        style="display: none;"
        @change="handleFileSelected"
      />

      <!-- Action buttons column -->
      <div class="flex items-center shrink-0" style="gap: 8px; padding-bottom: 2px;">
        <!-- Image button -->
        <button
          class="flex items-center justify-center shrink-0 cursor-pointer transition-opacity"
          :style="{
            width: '36px',
            height: '36px',
            background: attachedFiles.some(f => f.type.startsWith('image/')) ? '#1e3a5f' : 'var(--bg-button)',
            borderRadius: '10px',
            border: 'none',
          }"
          title="Attach image"
          @click="triggerFilePicker"
        >
          <svg width="16" height="16" viewBox="0 0 16 14" fill="none">
            <rect x="1" y="1" width="14" height="12" rx="2" :stroke="attachedFiles.some(f => f.type.startsWith('image/')) ? '#3b82f6' : '#8b8fa3'" stroke-width="1.3"/>
            <circle cx="5" cy="5" r="1.5" :stroke="attachedFiles.some(f => f.type.startsWith('image/')) ? '#3b82f6' : '#8b8fa3'" stroke-width="1.2"/>
            <path d="M1 11L5 7L8 10L10 8L15 11" :stroke="attachedFiles.some(f => f.type.startsWith('image/')) ? '#3b82f6' : '#8b8fa3'" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </button>

        <!-- Audio button -->
        <button
          class="flex items-center justify-center shrink-0 cursor-pointer transition-opacity"
          :style="{
            width: '36px',
            height: '36px',
            background: isRecording ? '#3b1717' : 'var(--bg-button)',
            borderRadius: '10px',
            border: 'none',
            animation: isRecording ? 'pulse 1.5s ease-in-out infinite' : 'none',
          }"
          :title="isRecording ? 'Stop recording' : 'Voice input'"
          @click="toggleRecording"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <rect x="5.5" y="1" width="5" height="9" rx="2.5" :stroke="isRecording ? '#ef4444' : '#8b8fa3'" stroke-width="1.3"/>
            <path d="M3 8C3 11 5.5 13 8 13C10.5 13 13 11 13 8" :stroke="isRecording ? '#ef4444' : '#8b8fa3'" stroke-width="1.3" stroke-linecap="round"/>
            <line x1="8" y1="13" x2="8" y2="15" :stroke="isRecording ? '#ef4444' : '#8b8fa3'" stroke-width="1.3" stroke-linecap="round"/>
          </svg>
        </button>
      </div>

      <!-- Textarea input -->
      <div
        class="flex-1 flex items-end"
        :style="{
          background: 'var(--bg-input)',
          border: '1px solid var(--border-input)',
          borderRadius: '12px',
          padding: '10px 16px',
          minHeight: '40px',
        }"
      >
        <textarea
          ref="inputRef"
          v-model="inputText"
          placeholder="Type a message... (Shift+Enter for new line)"
          :disabled="isStreaming"
          rows="1"
          class="flex-1 bg-transparent outline-none chat-textarea"
          :style="{
            fontSize: '13px',
            fontWeight: '400',
            color: 'var(--text-primary)',
            fontFamily: 'Inter, sans-serif',
            resize: 'none',
            lineHeight: '1.5',
            maxHeight: '200px',
            height: '24px',
            overflow: 'auto',
          }"
          @keydown="handleKeydown"
          @input="autoGrow"
        />
      </div>

      <!-- Send button -->
      <button
        class="flex items-center justify-center shrink-0 transition-opacity cursor-pointer"
        :style="{
          width: '44px',
          height: '44px',
          background: isStreaming ? '#1e2233' : '#3b82f6',
          borderRadius: '12px',
          border: 'none',
          opacity: isStreaming ? 0.5 : 1,
        }"
        :disabled="isStreaming"
        aria-label="Send message"
        data-testid="chat-send"
        @click="handleSend"
      >
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
          <path d="M2 9L16 2L9 16L8 10L2 9Z" fill="white" stroke="white" stroke-width="1.2" stroke-linejoin="round"/>
        </svg>
      </button>
    </div>
  </div>
</template>

<style scoped>
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
</style>
