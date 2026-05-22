<script setup>
/**
 * StoriesView — Main Stories page (Library + Chapter detail).
 *
 * Desktop: Split panel (library left + chapter list right)
 * Mobile: Toggle between library ↔ chapter list
 *
 * Shared by routes:
 *  - /stories (library)
 *  - /stories/:storyId (library + chapter detail)
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

const selectedStoryId = computed(() => route.params.storyId || null)
const selectedStory = computed(() =>
  stories.value.find(s => s.id === selectedStoryId.value)
)
const isMobileChapterView = computed(() =>
  selectedStoryId.value && isMobile.value
)

// ─── Fetch stories ───
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

// ─── Actions ───
function handleSelect(storyId) {
  router.push({ name: 'StoryDetail', params: { storyId } })
}

async function handleDelete(storyId) {
  // Check if currently playing this story
  if (audioStore.currentStoryId === storyId) {
    audioStore.stopAndReset()
  }
  try {
    await apiFetch(`/api/stories/${encodeURIComponent(storyId)}`, {
      method: 'DELETE',
    })
    stories.value = stories.value.filter(s => s.id !== storyId)
    toast.success('Story deleted')
    // If viewing this story, go back to library
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

// ─── Lifecycle ───
onMounted(fetchStories)

// Refresh on navigation back to library
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
  <div class="stories-view">
    <!-- Page header -->
    <div class="stories-view__header">
      <button
        v-if="isMobileChapterView"
        class="stories-view__back"
        @click="handleBack"
      >
        <svg viewBox="0 0 24 24" fill="none" width="18" height="18">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
      <h1 class="stories-view__title">{{ selectedStory?.title || 'Stories' }}</h1>
    </div>

    <!-- Body — single full-width state when loading/error/empty,
         split panel only when stories exist. -->
    <div
      v-if="isLoading"
      class="stories-view__body stories-view__body--full"
    >
      <div class="stories-view__skeleton">
        <div v-for="i in 4" :key="i" class="stories-view__skeleton-card"></div>
      </div>
    </div>

    <div
      v-else-if="error"
      class="stories-view__body stories-view__body--full stories-view__body--center"
    >
      <div class="stories-view__error">
        <p>{{ error }}</p>
        <button @click="fetchStories" class="stories-view__retry-btn">Retry</button>
      </div>
    </div>

    <div
      v-else-if="stories.length === 0"
      class="stories-view__body stories-view__body--full stories-view__body--center"
    >
      <div class="stories-view__empty">
        <svg viewBox="0 0 24 24" fill="none" width="48" height="48" style="margin-bottom: 12px;">
          <path d="M4 19.5A2.5 2.5 0 016.5 17H20" stroke="currentColor" stroke-width="1.5"/>
          <path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z" stroke="currentColor" stroke-width="1.5"/>
        </svg>
        <p>No stories yet</p>
        <p style="font-size: 12px; color: var(--text-subtle);">Use the AI to crawl stories from the web.</p>
      </div>
    </div>

    <div v-else class="stories-view__body">
      <!-- Left: Library -->
      <div
        class="stories-view__library"
        :class="{ 'stories-view__library--hidden': isMobileChapterView }"
      >
        <div class="stories-view__list">
          <StoryCard
            v-for="story in stories"
            :key="story.id"
            :story="story"
            :is-active="story.id === selectedStoryId"
            @select="handleSelect"
            @delete="handleDelete"
          />
        </div>
      </div>

      <!-- Right: Chapter detail -->
      <div
        v-if="selectedStoryId"
        class="stories-view__detail"
      >
        <ChapterList
          :story-id="selectedStoryId"
          :story-title="selectedStory?.title || selectedStoryId"
        />
      </div>

      <!-- No selection placeholder (desktop) -->
      <div
        v-else
        class="stories-view__placeholder"
      >
        <svg viewBox="0 0 24 24" fill="none" width="40" height="40" style="margin-bottom: 8px;">
          <path d="M15 6l-6 6 6 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <p>Select a story to view its chapter list</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.stories-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

/* Header */
.stories-view__header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
  flex-shrink: 0;
}
.stories-view__title {
  font-size: 20px;
  font-weight: 700;
  color: var(--text-heading, #f0f2f5);
  margin: 0;
}
.stories-view__back {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: none;
  background: rgba(255,255,255,0.06);
  color: var(--text-secondary, #c4c8d4);
  border-radius: 8px;
  cursor: pointer;
}

/* Body: split panel */
.stories-view__body {
  display: flex;
  gap: 20px;
  flex: 1;
  min-height: 0;
}
/* Full-width body for loading/error/empty (no split). */
.stories-view__body--full {
  display: block;
  overflow-y: auto;
}
.stories-view__body--center {
  display: flex;
  align-items: center;
  justify-content: center;
}

/* Library panel */
.stories-view__library {
  width: 380px;
  min-width: 300px;
  flex-shrink: 0;
  overflow-y: auto;
}
.stories-view__list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* Detail panel */
.stories-view__detail {
  flex: 1;
  background: var(--bg-card, #0c0e15);
  border: 1px solid var(--border-primary, #1a1d2e);
  border-radius: 12px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

/* Placeholder (no selection) */
.stories-view__placeholder {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: var(--text-subtle, #555872);
  font-size: 14px;
}

/* Skeleton */
.stories-view__skeleton {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.stories-view__skeleton-card {
  height: 88px;
  background: rgba(255,255,255,0.03);
  border-radius: 10px;
  animation: shimmer 1.5s ease-in-out infinite;
}
@keyframes shimmer {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 0.6; }
}

/* Error */
.stories-view__error {
  padding: 32px;
  text-align: center;
  color: var(--status-error, #ef4444);
  font-size: 14px;
}
.stories-view__retry-btn {
  margin-top: 12px;
  padding: 8px 20px;
  background: rgba(239,68,68,0.1);
  border: 1px solid rgba(239,68,68,0.2);
  color: var(--status-error, #ef4444);
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
}

/* Empty */
.stories-view__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 60px 24px;
  color: var(--text-subtle, #555872);
  text-align: center;
  font-size: 14px;
}

/* Mobile responsive */
@media (max-width: 768px) {
  .stories-view__body {
    flex-direction: column;
  }
  .stories-view__library {
    width: 100%;
    min-width: 0;
  }
  .stories-view__library--hidden {
    display: none;
  }
  .stories-view__detail {
    width: 100%;
  }
  .stories-view__placeholder {
    display: none;
  }
}

@media (min-width: 769px) {
  .stories-view__back {
    display: none;
  }
}
</style>
