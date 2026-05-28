<script setup>
/**
 * StoriesView — library + chapter detail.
 *
 * Logic preserved verbatim from the original view; only the visual
 * shell + tokens changed. Stores/composables/api untouched.
 */
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { apiFetch } from '../api'
import { useAudioPlayerStore } from '../stores/audioPlayer'
import { useToast } from '../composables/useToast'
import { useBreakpoint } from '../composables/useBreakpoint'
import StoryCard from '../components/stories/StoryCard.vue'
import ChapterList from '../components/stories/ChapterList.vue'

const route = useRoute()
const router = useRouter()
const audioStore = useAudioPlayerStore()
const toast = useToast()
const { isMobile } = useBreakpoint()

// ─── State ───
const stories = ref([])
const isLoading = ref(false)
const error = ref(null)
const search = ref('')
const filterMode = ref('all') // all | in_progress | done

const selectedStoryId = computed(() => route.params.storyId || null)
const selectedStory = computed(() =>
  stories.value.find(s => s.id === selectedStoryId.value)
)
const isMobileChapterView = computed(() =>
  selectedStoryId.value && isMobile.value
)

const filteredStories = computed(() => {
  const q = search.value.trim().toLowerCase()
  return stories.value.filter(s => {
    if (filterMode.value === 'in_progress') {
      if (!s.last_chapter_file) return false
      if (s.chapters && (s.last_chapter_num || 0) >= s.chapters) return false
    } else if (filterMode.value === 'done') {
      if (!s.chapters || (s.last_chapter_num || 0) < s.chapters) return false
    }
    if (!q) return true
    return (s.title || '').toLowerCase().includes(q)
      || (s.id || '').toLowerCase().includes(q)
  })
})

const doneCount = computed(() =>
  stories.value.filter(s => s.chapters && (s.last_chapter_num || 0) >= s.chapters).length
)
const inProgressCount = computed(() =>
  stories.value.filter(s => s.last_chapter_file
    && (!s.chapters || (s.last_chapter_num || 0) < s.chapters)).length
)

// ─── Fetch ───
async function fetchStories() {
  isLoading.value = true
  error.value = null
  try {
    const data = await apiFetch('/api/stories')
    stories.value = data || []
  } catch (e) {
    error.value = e.message
  } finally {
    isLoading.value = false
  }
}

function handleSelect(storyId) {
  router.push({ name: 'StoryDetail', params: { storyId } })
}

async function handleDelete(storyId) {
  if (audioStore.currentStoryId === storyId) {
    audioStore.stopAndReset()
  }
  try {
    await apiFetch(`/api/stories/${encodeURIComponent(storyId)}`, {
      method: 'DELETE',
    })
    stories.value = stories.value.filter(s => s.id !== storyId)
    toast.success('Story deleted')
    if (selectedStoryId.value === storyId) {
      router.push({ name: 'Stories' })
    }
  } catch (e) {
    toast.error('Delete failed', { description: e.message })
  }
}

function handleBack() {
  router.push({ name: 'Stories' })
}

onMounted(fetchStories)

watch(
  () => route.path,
  (newPath) => {
    if (newPath === '/stories') {
      fetchStories()
    }
  },
)
</script>

<template>
  <div class="stories jv">
    <!-- ─── Header ─── -->
    <div class="stories__header">
      <div class="stories__heading">
        <div class="eyebrow">WORKSPACE · STORIES</div>
        <h1 class="stories__title">
          <span class="grad" style="font-style: italic;">{{ stories.length }}</span> stories
          <span class="stories__title-sub" v-if="stories.length">
            · {{ doneCount }} done · {{ inProgressCount }} in progress
          </span>
        </h1>
        <p class="stories__desc">
          Library managed via
          <code class="stories__inline-code">local_list_stories</code>; TTS pregen runs in the background.
        </p>
      </div>
    </div>

    <!-- ─── Loading skeleton ─── -->
    <div v-if="isLoading" class="stories__body stories__body--full">
      <div class="stories__skeleton">
        <div v-for="i in 4" :key="i" class="stories__skeleton-card"></div>
      </div>
    </div>

    <!-- ─── Error ─── -->
    <div v-else-if="error" class="stories__body stories__body--full stories__body--center">
      <div class="stories__error">
        <p>{{ error }}</p>
        <button @click="fetchStories" class="btn btn-secondary">Retry</button>
      </div>
    </div>

    <!-- ─── Empty ─── -->
    <div v-else-if="stories.length === 0" class="stories__body stories__body--full stories__body--center">
      <div class="stories__empty">
        <svg viewBox="0 0 24 24" fill="none" width="44" height="44" class="stories__empty-icon">
          <path d="M4 19.5A2.5 2.5 0 016.5 17H20" stroke="currentColor" stroke-width="1.5"/>
          <path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z" stroke="currentColor" stroke-width="1.5"/>
        </svg>
        <p class="stories__empty-title">No stories yet</p>
        <p class="stories__empty-sub">Ask the AI to crawl stories from the web to fill your library.</p>
      </div>
    </div>

    <!-- ─── Library + Detail ─── -->
    <div v-else class="stories__body">
      <!-- Library -->
      <aside
        class="stories__library"
        :class="{ 'stories__library--hidden': isMobileChapterView }"
      >
        <div class="stories__library-toolbar">
          <div class="stories__search">
            <svg viewBox="0 0 24 24" fill="none" width="14" height="14" stroke="currentColor" stroke-width="1.8">
              <circle cx="11" cy="11" r="7"/>
              <line x1="21" y1="21" x2="16.65" y2="16.65" stroke-linecap="round"/>
            </svg>
            <input
              v-model="search"
              type="search"
              placeholder="Filter library…"
              class="stories__search-input"
            />
          </div>
          <div class="seg stories__seg">
            <button :class="{ 'is-active': filterMode === 'all' }" @click="filterMode = 'all'">
              All {{ stories.length }}
            </button>
            <button :class="{ 'is-active': filterMode === 'in_progress' }" @click="filterMode = 'in_progress'">
              In progress
            </button>
            <button :class="{ 'is-active': filterMode === 'done' }" @click="filterMode = 'done'">
              Done
            </button>
          </div>
        </div>

        <div class="stories__list">
          <StoryCard
            v-for="story in filteredStories"
            :key="story.id"
            :story="story"
            :is-active="story.id === selectedStoryId"
            @select="handleSelect"
            @delete="handleDelete"
          />
          <div v-if="!filteredStories.length" class="stories__empty-filter">
            No stories match your filter.
          </div>
        </div>
      </aside>

      <!-- Detail -->
      <section v-if="selectedStoryId" class="stories__detail">
        <div class="stories__detail-back" v-if="isMobileChapterView">
          <button class="btn btn-ghost" @click="handleBack">
            <svg viewBox="0 0 24 24" fill="none" width="14" height="14">
              <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            Library
          </button>
        </div>
        <ChapterList
          :story-id="selectedStoryId"
          :story-title="selectedStory?.title || selectedStoryId"
        />
      </section>

      <section v-else class="stories__placeholder">
        <div class="stories__placeholder-inner">
          <div class="mono-label" style="font-size: 10px;">SELECT A STORY</div>
          <p class="stories__placeholder-text">
            Pick a story from the library to view its chapter list.
          </p>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.stories {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  color: var(--text);
}

/* Header */
.stories__header {
  padding: 4px 0 18px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 14px;
  flex-shrink: 0;
}
.stories__heading {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.stories__title {
  font-family: var(--font-display);
  font-size: 22px;
  letter-spacing: -0.02em;
  margin: 4px 0 0;
}
.stories__title-sub {
  font-family: var(--font-body);
  color: var(--text-muted);
  font-size: 14px;
  font-weight: 400;
  margin-left: 6px;
}
.stories__desc {
  font-size: 12.5px;
  color: var(--text-dim);
}
.stories__inline-code {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--accent);
}

/* Body */
.stories__body {
  display: grid;
  grid-template-columns: 380px 1fr;
  gap: 14px;
  flex: 1;
  min-height: 0;
}
.stories__body--full {
  display: block;
  overflow-y: auto;
}
.stories__body--center {
  display: flex;
  align-items: center;
  justify-content: center;
}

/* Library */
.stories__library {
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  overflow: hidden;
}
.stories__library-toolbar {
  padding: 12px 12px;
  border-bottom: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.stories__search {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 32px;
  padding: 0 10px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  color: var(--text-muted);
}
.stories__search:focus-within {
  border-color: var(--primary);
  color: var(--text);
}
.stories__search-input {
  flex: 1;
  background: transparent;
  border: 0;
  outline: 0;
  font-family: var(--font-body);
  font-size: 12.5px;
  color: var(--text);
}
.stories__search-input::placeholder { color: var(--text-muted); }
.stories__seg button { flex: 1; }

.stories__list {
  flex: 1;
  overflow-y: auto;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.stories__empty-filter {
  padding: 24px 16px;
  text-align: center;
  color: var(--text-muted);
  font-size: 12.5px;
}

/* Detail */
.stories__detail {
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.stories__detail-back {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
}

/* Placeholder */
.stories__placeholder {
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-muted);
}
.stories__placeholder-inner {
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.stories__placeholder-text {
  font-size: 13px;
  color: var(--text-dim);
}

/* Skeleton */
.stories__skeleton {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 0;
}
.stories__skeleton-card {
  height: 88px;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  animation: shimmer 1.5s ease-in-out infinite;
}
@keyframes shimmer {
  0%, 100% { opacity: 0.5; }
  50% { opacity: 0.85; }
}

/* Error / Empty */
.stories__error {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 40px;
  text-align: center;
  color: var(--danger);
  font-size: 13px;
}
.stories__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 60px 24px;
  text-align: center;
  gap: 6px;
}
.stories__empty-icon { color: var(--text-muted); margin-bottom: 6px; }
.stories__empty-title { font-size: 15px; color: var(--text); font-weight: 500; }
.stories__empty-sub { font-size: 12px; color: var(--text-muted); }

/* Mobile */
@media (max-width: 768px) {
  .stories__body {
    grid-template-columns: 1fr;
  }
  .stories__library--hidden { display: none; }
  .stories__placeholder { display: none; }
}
</style>
