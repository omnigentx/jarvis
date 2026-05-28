<script setup>
/**
 * ChapterList — chapter list pane with live pregen status (SSE).
 * Logic preserved; visual shell restyled.
 */
import { ref, computed, watch, nextTick, toRef } from 'vue'
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

const storyIdRef = toRef(props, 'storyId')
const { getQueuePosition, getEffectiveStatus } = usePregenStream(storyIdRef)

const chapters = ref([])
const isLoading = ref(false)
const error = ref(null)

const chapterFiles = computed(() => chapters.value.map(c => c.file))
const readyCount = computed(() =>
  chapters.value.filter(c => (getEffectiveStatus(c.file, c.preload) || c.preload) === 'ready').length
)

async function fetchChapters() {
  isLoading.value = true
  error.value = null
  try {
    const data = await apiFetch(`/api/stories/${encodeURIComponent(props.storyId)}/chapters`)
    chapters.value = data || []
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

function chapterPreload(ch) {
  return getEffectiveStatus(ch.file, ch.preload)
}

function chapterQueuePos(file) {
  const pos = getQueuePosition(file)
  return pos >= 0 ? pos + 1 : -1
}

watch(() => props.storyId, () => {
  fetchChapters()
}, { immediate: true })

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
      <div class="chapter-list__heading">
        <div class="mono-label" style="font-size: 10px;">CHAPTERS</div>
        <h3 class="chapter-list__title">{{ storyTitle || storyId }}</h3>
      </div>
      <div class="chapter-list__counts" v-if="chapters.length">
        <span class="chapter-list__count">{{ chapters.length }} chapters</span>
        <span class="chapter-list__count chapter-list__count--ok">{{ readyCount }} ready</span>
      </div>
    </div>

    <!-- Loading -->
    <div v-if="isLoading" class="chapter-list__skeleton">
      <div v-for="i in 8" :key="i" class="chapter-list__skeleton-row"></div>
    </div>

    <!-- Error -->
    <div v-else-if="error" class="chapter-list__error">
      <p>{{ error }}</p>
      <button @click="fetchChapters" class="btn btn-secondary">Retry</button>
    </div>

    <!-- Empty -->
    <div v-else-if="chapters.length === 0" class="chapter-list__empty">
      <p>No chapters yet</p>
    </div>

    <!-- Body -->
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
  min-height: 0;
}

.chapter-list__header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  padding: 14px 18px 12px;
  border-bottom: 1px solid var(--border);
  gap: 12px;
  flex-shrink: 0;
}
.chapter-list__heading { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.chapter-list__title {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chapter-list__counts {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}
.chapter-list__count {
  font-family: var(--font-mono);
  font-size: 10px;
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--bg-3);
  color: var(--text-muted);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  border: 1px solid var(--border-strong);
}
.chapter-list__count--ok {
  color: var(--success);
  border-color: rgba(16,185,129,0.25);
  background: var(--success-bg);
}

.chapter-list__body {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}

.chapter-list__skeleton {
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.chapter-list__skeleton-row {
  height: 40px;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  animation: shimmer 1.5s ease-in-out infinite;
}
@keyframes shimmer {
  0%, 100% { opacity: 0.5; }
  50% { opacity: 0.85; }
}

.chapter-list__error,
.chapter-list__empty {
  padding: 32px 16px;
  text-align: center;
  font-size: 13px;
}
.chapter-list__error { color: var(--danger); display: flex; flex-direction: column; gap: 8px; align-items: center; }
.chapter-list__empty { color: var(--text-muted); }
</style>
