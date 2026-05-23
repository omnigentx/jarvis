<script setup>
/**
 * ChapterList — Chapter list + preload status.
 *
 * Props:
 *  - storyId: string
 *  - storyTitle: string
 *
 * Features:
 *  - Fetch chapters from API
 *  - SSE stream for pregen status (realtime, no polling)
 *  - Display preload status (ready/generating/queued/none)
 *  - Highlight currently playing chapter
 *  - Scroll to playing chapter
 */
import { ref, computed, watch, onMounted, nextTick, toRef } from 'vue'
import { useRouter } from 'vue-router'
import { apiFetch } from '../../api'
import { useAudioPlayerStore } from '../../stores/audioPlayer'
import { useToast } from '../../composables/useToast'
import { usePregenStream } from '../../composables/usePregenStream'
import ChapterRow from './ChapterRow.vue'

const props = defineProps({
  storyId: { type: String, required: true },
  storyTitle: { type: String, default: '' },
})

const router = useRouter()
const audioStore = useAudioPlayerStore()
const toast = useToast()

// SSE stream for pregen status
const storyIdRef = toRef(props, 'storyId')
const { queue, getQueuePosition, getEffectiveStatus } = usePregenStream(storyIdRef)

const chapters = ref([])
const isLoading = ref(false)
const error = ref(null)

const chapterFiles = computed(() => chapters.value.map(c => c.file))

async function fetchChapters() {
  isLoading.value = true
  error.value = null
  try {
    const data = await apiFetch(`/api/stories/${encodeURIComponent(props.storyId)}/chapters`)
    chapters.value = data || []
    // Update batch generation status in store
    audioStore.updateBatchChapterStatus(chapters.value)
  } catch (e) {
    error.value = e.message
  } finally {
    isLoading.value = false
  }
}

async function handlePlay(filename) {
  try {
    await audioStore.playChapter(
      props.storyId,
      props.storyTitle,
      filename,
      chapterFiles.value,
    )
  } catch (e) {
    toast.error('Unable to play audio', { description: e.message })
  }
}

function handleRead(filename) {
  router.push({
    name: 'StoryReader',
    params: { storyId: props.storyId, filename },
  })
}

function isChapterPlaying(filename) {
  return audioStore.currentStoryId === props.storyId
    && audioStore.currentChapterFile === filename
    && (audioStore.isPlaying || audioStore.isPaused)
}

/**
 * Compute effective preload status: SSE data takes precedence over API data.
 */
function chapterPreload(ch) {
  return getEffectiveStatus(ch.file, ch.preload)
}

/**
 * Get queue position (1-based) for display, -1 if not present.
 */
function chapterQueuePos(file) {
  const pos = getQueuePosition(file)
  return pos >= 0 ? pos + 1 : -1
}

// Re-fetch when storyId changes
watch(() => props.storyId, () => {
  fetchChapters()
}, { immediate: true })

// Scroll to playing chapter
watch(
  () => audioStore.currentChapterFile,
  async (file) => {
    if (!file) return
    await nextTick()
    const el = document.getElementById(`chapter-${file}`)
    el?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  },
)
</script>

<template>
  <div class="chapter-list">
    <!-- Header -->
    <div class="chapter-list__header">
      <h3 class="chapter-list__title">Chapter list</h3>
      <span class="chapter-list__count" v-if="chapters.length">{{ chapters.length }} chapters</span>
    </div>

    <!-- Loading skeleton -->
    <div v-if="isLoading" class="chapter-list__skeleton">
      <div v-for="i in 8" :key="i" class="chapter-list__skeleton-row"></div>
    </div>

    <!-- Error state -->
    <div v-else-if="error" class="chapter-list__error">
      <p>{{ error }}</p>
      <button @click="fetchChapters" class="chapter-list__retry">Retry</button>
    </div>

    <!-- Empty state -->
    <div v-else-if="chapters.length === 0" class="chapter-list__empty">
      <p>No chapters yet</p>
    </div>

    <!-- Chapter rows -->
    <div v-else class="chapter-list__body">
      <div
        v-for="(ch, idx) in chapters"
        :key="ch.file"
        :id="'chapter-' + ch.file"
      >
        <ChapterRow
          :chapter="ch"
          :index="idx"
          :is-playing="isChapterPlaying(ch.file)"
          :effective-preload="chapterPreload(ch)"
          :queue-position="chapterQueuePos(ch.file)"
          @play="handlePlay"
          @read="handleRead"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.chapter-list {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.chapter-list__header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 16px 12px;
  border-bottom: 1px solid var(--border-primary, #1a1d2e);
}
.chapter-list__title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-heading, #f0f2f5);
  margin: 0;
}
.chapter-list__count {
  font-size: 12px;
  color: var(--text-muted, #8b8fa3);
}

.chapter-list__body {
  flex: 1;
  overflow-y: auto;
  padding: 4px 4px;
}

/* Skeleton */
.chapter-list__skeleton {
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.chapter-list__skeleton-row {
  height: 42px;
  background: rgba(255,255,255,0.03);
  border-radius: 8px;
  animation: shimmer 1.5s ease-in-out infinite;
}
@keyframes shimmer {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 0.6; }
}

/* Error */
.chapter-list__error {
  padding: 24px 16px;
  text-align: center;
  color: var(--status-error, #ef4444);
  font-size: 13px;
}
.chapter-list__retry {
  margin-top: 8px;
  padding: 6px 16px;
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.2);
  color: var(--status-error, #ef4444);
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
}

/* Empty */
.chapter-list__empty {
  padding: 32px 16px;
  text-align: center;
  color: var(--text-subtle, #555872);
  font-size: 13px;
}
</style>
