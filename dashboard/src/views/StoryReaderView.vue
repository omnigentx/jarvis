<script setup>
/**
 * StoryReaderView — Trang đọc text chương + audio sync.
 *
 * Features:
 *  - Fetch nội dung text chương
 *  - Font size controls (tăng/giảm/reset)
 *  - Audio play/pause inline
 *  - Chapter navigation (prev/next)
 */
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { apiFetch } from '../api'
import { useAudioPlayerStore } from '../stores/audioPlayer'
import { useToast } from '../composables/useToast'

const props = defineProps({
  storyId: { type: String, required: true },
  filename: { type: String, required: true },
})

const route = useRoute()
const router = useRouter()
const audioStore = useAudioPlayerStore()
const toast = useToast()

// ─── State ───
const content = ref('')
const isLoading = ref(false)
const fontSize = ref(_loadFontSize())
const chapters = ref([])

const FONT_SIZE_KEY = 'jarvis_reader_font_size'
const FONT_SIZES = [14, 16, 18, 20, 22, 24]

// ─── Computed ───
const currentFilename = computed(() => route.params.filename || props.filename)
const currentStoryId = computed(() => route.params.storyId || props.storyId)

const chapterNum = computed(() => {
  const match = currentFilename.value.match(/^(\d+)/)
  return match ? parseInt(match[1], 10) : 0
})

const chapterTitle = computed(() => {
  const name = currentFilename.value.replace('.txt', '')
  const parts = name.split('_')
  if (parts.length <= 1) return name
  return parts.slice(1).map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
})

const currentIndex = computed(() =>
  chapters.value.findIndex(c => c.file === currentFilename.value)
)
const canPrev = computed(() => currentIndex.value > 0)
const canNext = computed(() => currentIndex.value < chapters.value.length - 1)

const isThisChapterPlaying = computed(() =>
  audioStore.currentStoryId === currentStoryId.value
  && audioStore.currentChapterFile === currentFilename.value
  && audioStore.isPlaying
)

const paragraphs = computed(() => {
  if (!content.value) return []
  return content.value.split('\n').filter(p => p.trim().length > 0)
})

// ─── Actions ───
async function fetchContent() {
  isLoading.value = true
  try {
    const data = await apiFetch(
      `/api/stories/${encodeURIComponent(currentStoryId.value)}/chapters/${encodeURIComponent(currentFilename.value)}`
    )
    content.value = data.content || data.error || ''
  } catch (e) {
    content.value = ''
    toast.error('Không thể tải nội dung', { description: e.message })
  } finally {
    isLoading.value = false
  }
}

async function fetchChapters() {
  try {
    const data = await apiFetch(`/api/stories/${encodeURIComponent(currentStoryId.value)}/chapters`)
    chapters.value = data || []
  } catch (_) {}
}

async function handlePlayToggle() {
  if (isThisChapterPlaying.value) {
    audioStore.togglePlayPause()
    return
  }
  // Play this chapter
  try {
    const chapterFiles = chapters.value.map(c => c.file)
    await audioStore.playChapter(
      currentStoryId.value,
      currentStoryId.value, // title fallback
      currentFilename.value,
      chapterFiles,
    )
  } catch (e) {
    toast.error('Không thể phát audio', { description: e.message })
  }
}

function goChapter(direction) {
  const idx = currentIndex.value + direction
  if (idx < 0 || idx >= chapters.value.length) return
  const target = chapters.value[idx]
  router.replace({
    name: 'StoryReader',
    params: { storyId: currentStoryId.value, filename: target.file },
  })
}

function changeFontSize(delta) {
  const idx = FONT_SIZES.indexOf(fontSize.value)
  const newIdx = Math.max(0, Math.min(idx + delta, FONT_SIZES.length - 1))
  fontSize.value = FONT_SIZES[newIdx]
  localStorage.setItem(FONT_SIZE_KEY, String(fontSize.value))
}

function _loadFontSize() {
  const saved = localStorage.getItem(FONT_SIZE_KEY)
  return saved ? parseInt(saved, 10) : 18
}

function handleBack() {
  router.push({ name: 'StoryDetail', params: { storyId: currentStoryId.value } })
}

// ─── Watchers ───
watch(currentFilename, () => {
  fetchContent()
  window.scrollTo({ top: 0, behavior: 'smooth' })
})

// ─── Lifecycle ───
onMounted(() => {
  fetchContent()
  fetchChapters()
})
</script>

<template>
  <div class="reader-view">
    <!-- Toolbar -->
    <div class="reader-view__toolbar">
      <button class="reader-view__btn" @click="handleBack" title="Quay lại">
        <svg viewBox="0 0 24 24" fill="none" width="18" height="18">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>

      <div class="reader-view__header-info">
        <span class="reader-view__chapter-num">Ch.{{ chapterNum }}</span>
        <span class="reader-view__chapter-title">{{ chapterTitle }}</span>
      </div>

      <div class="reader-view__toolbar-actions">
        <!-- Font size controls -->
        <button class="reader-view__btn" @click="changeFontSize(-1)" title="Giảm cỡ chữ">
          <span style="font-size: 12px; font-weight: 700;">A-</span>
        </button>
        <span class="reader-view__font-label">{{ fontSize }}px</span>
        <button class="reader-view__btn" @click="changeFontSize(1)" title="Tăng cỡ chữ">
          <span style="font-size: 16px; font-weight: 700;">A+</span>
        </button>

        <!-- Play/pause button -->
        <button
          class="reader-view__btn reader-view__btn--audio"
          @click="handlePlayToggle"
          :title="isThisChapterPlaying ? 'Tạm dừng' : 'Phát audio'"
        >
          <svg v-if="!isThisChapterPlaying" viewBox="0 0 24 24" fill="none" width="18" height="18">
            <polygon points="6,3 20,12 6,21" fill="currentColor"/>
          </svg>
          <svg v-else viewBox="0 0 24 24" fill="none" width="18" height="18">
            <rect x="6" y="4" width="4" height="16" rx="1" fill="currentColor"/>
            <rect x="14" y="4" width="4" height="16" rx="1" fill="currentColor"/>
          </svg>
        </button>
      </div>
    </div>

    <!-- Loading -->
    <div v-if="isLoading" class="reader-view__loading">
      <div class="reader-view__loading-spinner"></div>
      <p>Đang tải nội dung...</p>
    </div>

    <!-- Content -->
    <div v-else class="reader-view__content" :style="{ fontSize: fontSize + 'px' }">
      <p
        v-for="(para, idx) in paragraphs"
        :key="idx"
        class="reader-view__paragraph"
      >
        {{ para }}
      </p>
    </div>

    <!-- Chapter navigation footer -->
    <div class="reader-view__nav">
      <button
        class="reader-view__nav-btn"
        :disabled="!canPrev"
        @click="goChapter(-1)"
      >
        ← Chương trước
      </button>
      <span class="reader-view__nav-progress">
        {{ currentIndex + 1 }} / {{ chapters.length }}
      </span>
      <button
        class="reader-view__nav-btn"
        :disabled="!canNext"
        @click="goChapter(1)"
      >
        Chương sau →
      </button>
    </div>
  </div>
</template>

<style scoped>
.reader-view {
  display: flex;
  flex-direction: column;
  max-width: 780px;
  margin: 0 auto;
}

/* Toolbar */
.reader-view__toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
  margin-bottom: 20px;
  border-bottom: 1px solid var(--border-primary, #1a1d2e);
  position: sticky;
  top: 0;
  background: var(--bg-base, #0a0d14);
  z-index: 10;
}

.reader-view__header-info {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.reader-view__chapter-num {
  font-size: 14px;
  font-weight: 700;
  color: var(--accent-blue, #3b82f6);
  white-space: nowrap;
}
.reader-view__chapter-title {
  font-size: 14px;
  color: var(--text-secondary, #c4c8d4);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.reader-view__toolbar-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.reader-view__btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: none;
  background: transparent;
  color: var(--text-secondary, #c4c8d4);
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.15s;
}
.reader-view__btn:hover {
  background: rgba(255,255,255,0.06);
  color: var(--text-heading, #f0f2f5);
}
.reader-view__btn--audio {
  background: rgba(59,130,246,0.12);
  color: var(--accent-blue, #3b82f6);
}
.reader-view__btn--audio:hover {
  background: rgba(59,130,246,0.2);
}
.reader-view__font-label {
  font-size: 11px;
  color: var(--text-muted, #8b8fa3);
  font-variant-numeric: tabular-nums;
  min-width: 32px;
  text-align: center;
}

/* Content */
.reader-view__content {
  line-height: 1.85;
  color: var(--text-secondary, #c4c8d4);
  padding-bottom: 40px;
}
.reader-view__paragraph {
  margin: 0 0 1em;
  text-indent: 2em;
}

/* Loading */
.reader-view__loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 60px;
  color: var(--text-muted, #8b8fa3);
  gap: 12px;
}
.reader-view__loading-spinner {
  width: 24px;
  height: 24px;
  border: 2px solid rgba(59,130,246,0.2);
  border-top-color: var(--accent-blue, #3b82f6);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* Navigation footer */
.reader-view__nav {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 0;
  border-top: 1px solid var(--border-primary, #1a1d2e);
  margin-top: 8px;
}
.reader-view__nav-btn {
  padding: 8px 16px;
  font-size: 13px;
  font-weight: 500;
  color: var(--accent-blue, #3b82f6);
  background: rgba(59,130,246,0.08);
  border: 1px solid rgba(59,130,246,0.15);
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.15s;
}
.reader-view__nav-btn:hover:not(:disabled) {
  background: rgba(59,130,246,0.15);
}
.reader-view__nav-btn:disabled {
  opacity: 0.3;
  cursor: default;
}
.reader-view__nav-progress {
  font-size: 12px;
  color: var(--text-muted, #8b8fa3);
}

/* Mobile */
@media (max-width: 768px) {
  .reader-view {
    max-width: 100%;
  }
  .reader-view__content {
    padding: 0 4px;
  }
}
</style>
