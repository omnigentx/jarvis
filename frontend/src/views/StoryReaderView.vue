<script setup>
/**
 * StoryReaderView — Chapter text reader + audio play.
 *
 * Logic preserved from the original (font-size persistence, prev/next chapter
 * navigation, play/pause via the global audioPlayer store). Only the shell
 * was restyled to match the redesign tokens.
 */
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { apiFetch } from '../api'
import { useAudioPlayerStore } from '../stores/audioPlayer'
import { useToast } from '../composables/useToast'
import { useBreakpoint } from '../composables/useBreakpoint'
import { useLang } from '../composables/useLang'

const { isMobile } = useBreakpoint()
const { t } = useLang()

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
    toast.error(t('stories.unableLoadContent'), { description: e.message })
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
  try {
    const chapterFiles = chapters.value.map(c => c.file)
    await audioStore.playChapter(
      currentStoryId.value,
      currentStoryId.value,
      currentFilename.value,
      chapterFiles,
    )
  } catch (e) {
    toast.error(t('stories.unablePlayAudio'), { description: e.message })
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

function setFontSize(size) {
  fontSize.value = size
  localStorage.setItem(FONT_SIZE_KEY, String(size))
}

function _loadFontSize() {
  const saved = localStorage.getItem(FONT_SIZE_KEY)
  return saved ? parseInt(saved, 10) : 18
}

function handleBack() {
  router.push({ name: 'StoryDetail', params: { storyId: currentStoryId.value } })
}

watch(currentFilename, () => {
  fetchContent()
  window.scrollTo({ top: 0, behavior: 'smooth' })
})

onMounted(() => {
  fetchContent()
  fetchChapters()
})
</script>

<template>
  <div class="reader jv">
    <!-- Header -->
    <div class="reader__header">
      <!-- Mobile already shows a back arrow in the app-bar; hide this one there. -->
      <button v-if="!isMobile" class="btn btn-icon btn-ghost" @click="handleBack" :title="t('common.back')">
        <svg viewBox="0 0 24 24" fill="none" width="14" height="14">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>

      <div class="reader__info">
        <div class="mono-label" style="font-size: 10px;">{{ currentStoryId.toUpperCase() }} · CH.{{ chapterNum }}</div>
        <h1 class="reader__title">{{ chapterTitle }}</h1>
      </div>

      <span v-if="isThisChapterPlaying" class="chip chip-success">
        <span class="chip-dot pulse-dot"></span>
        {{ t('stories.playing') }}
      </span>
    </div>

    <!-- Controls: prev chapter · font size · next chapter · play (one row).
         Chapter nav is up here too so the reader doesn't have to scroll to the
         bottom to move chapters. -->
    <div class="reader__controls">
      <button class="btn btn-icon btn-secondary reader__chap" :disabled="!canPrev" @click="goChapter(-1)" :title="t('stories.prevChapter')" :aria-label="t('stories.prevChapter')">
        <svg viewBox="0 0 24 24" fill="none" width="15" height="15">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>

      <div class="seg reader__seg">
        <button
          v-for="size in FONT_SIZES"
          :key="size"
          :class="{ 'is-active': fontSize === size }"
          @click="setFontSize(size)"
        >Aa {{ size }}</button>
      </div>

      <button class="btn btn-icon btn-secondary reader__chap" :disabled="!canNext" @click="goChapter(1)" :title="t('stories.nextChapter')" :aria-label="t('stories.nextChapter')">
        <svg viewBox="0 0 24 24" fill="none" width="15" height="15">
          <path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>

      <button
        class="btn btn-icon"
        :class="isThisChapterPlaying ? 'btn-primary' : 'btn-secondary'"
        @click="handlePlayToggle"
        :title="isThisChapterPlaying ? t('stories.pause') : t('stories.playAudio')"
      >
        <svg v-if="!isThisChapterPlaying" viewBox="0 0 24 24" fill="none" width="14" height="14">
          <polygon points="6,3 20,12 6,21" fill="currentColor"/>
        </svg>
        <svg v-else viewBox="0 0 24 24" fill="none" width="14" height="14">
          <rect x="6" y="4" width="4" height="16" rx="1" fill="currentColor"/>
          <rect x="14" y="4" width="4" height="16" rx="1" fill="currentColor"/>
        </svg>
      </button>
    </div>

    <!-- Loading -->
    <div v-if="isLoading" class="reader__loading">
      <div class="reader__spinner"></div>
      <p>{{ t('stories.loadingContent') }}</p>
    </div>

    <!-- Content -->
    <div v-else class="reader__body">
      <div class="reader__content" :style="{ fontSize: fontSize + 'px' }">
        <p
          v-for="(para, idx) in paragraphs"
          :key="idx"
          class="reader__paragraph"
        >{{ para }}</p>
      </div>
    </div>

    <!-- Bottom chapter nav -->
    <div class="reader__nav">
      <button class="btn btn-secondary" :disabled="!canPrev" @click="goChapter(-1)">
        <svg viewBox="0 0 24 24" fill="none" width="12" height="12">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        {{ t('stories.previous') }}
      </button>
      <span class="reader__nav-progress">
        {{ currentIndex + 1 }} / {{ chapters.length || '—' }}
      </span>
      <button class="btn btn-secondary" :disabled="!canNext" @click="goChapter(1)">
        {{ t('stories.next') }}
        <svg viewBox="0 0 24 24" fill="none" width="12" height="12">
          <path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
    </div>
  </div>
</template>

<style scoped>
.reader {
  display: flex;
  flex-direction: column;
  min-height: 100%;
}

/* Header */
.reader__header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-0);
  position: sticky;
  top: 0;
  z-index: 10;
  flex-wrap: wrap;
}
/* Controls row: prev · font-size · next · play, all on one line. The font
   segment flexes + scrolls so the play button stays on the same row. */
.reader__controls {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
}
.reader__controls .reader__seg {
  flex: 1;
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: none;
}
.reader__controls .reader__seg::-webkit-scrollbar { display: none; }
.reader__chap { flex: 0 0 auto; }
.reader__info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.reader__title {
  font-size: 16px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.reader__seg button { padding: 0 8px; font-size: 11px; font-family: var(--font-mono); }

/* Body */
.reader__body {
  flex: 1;
  overflow-y: auto;
  display: flex;
  justify-content: center;
  padding: 24px;
}
.reader__content {
  max-width: 720px;
  width: 100%;
  line-height: 1.75;
  color: var(--text-dim);
}
.reader__paragraph {
  margin: 0 0 18px;
}

/* Loading */
.reader__loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 60px;
  gap: 10px;
  color: var(--text-muted);
}
.reader__spinner {
  width: 22px;
  height: 22px;
  border: 2px solid var(--primary-bg-strong);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* Nav footer */
.reader__nav {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 24px;
  border-top: 1px solid var(--border);
  background: var(--bg-1);
  gap: 12px;
}
.reader__nav-progress {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
}

@media (max-width: 768px) {
  .reader__body { padding: 16px; }
  /* Keep the font-size picker visible on mobile — phones are the
     primary reading surface for this app, hiding the affordance forced
     users to switch to desktop just to change reading size. Shrink the
     buttons + tighten gap so it still fits the toolbar. */
  .reader__seg button { padding: 0 6px; font-size: 10.5px; }
}
</style>
