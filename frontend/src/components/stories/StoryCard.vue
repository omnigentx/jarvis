<script setup>
/**
 * StoryCard — Library card for a single story.
 *
 * Logic preserved; layout restyled to match the redesign:
 * cover gradient · title · progress bar · status chips · delete affordance.
 */
import { ref, computed } from 'vue'
import ConfirmModal from '../ConfirmModal.vue'

const props = defineProps({
  story: { type: Object, required: true },
  isActive: { type: Boolean, default: false },
})

const emit = defineEmits(['select', 'delete'])

const showDeleteConfirm = ref(false)

const hasProgress = computed(() => !!props.story.last_chapter_file)
const progressPercent = computed(() => {
  if (!hasProgress.value || !props.story.chapters) return 0
  return Math.min(((props.story.last_chapter_num || 0) / props.story.chapters) * 100, 100)
})
const progressFraction = computed(() => {
  if (!props.story.chapters) return ''
  return `${props.story.last_chapter_num || 0}/${props.story.chapters}`
})
const isComplete = computed(() =>
  props.story.chapters && (props.story.last_chapter_num || 0) >= props.story.chapters
)

const lastPlayedLabel = computed(() => {
  if (!props.story.last_played_at) return null
  const diff = Date.now() / 1000 - props.story.last_played_at
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
})

// Deterministic cover hue based on title — same title always gets the same color.
const coverColor = computed(() => {
  const palette = [
    'var(--role-pm)', 'var(--role-sa)', 'var(--role-dev)',
    'var(--role-qe)', 'var(--role-des)', 'var(--role-ba)', 'var(--role-dso)',
  ]
  const seed = (props.story.title || props.story.id || '?')
  let hash = 0
  for (let i = 0; i < seed.length; i++) {
    hash = (hash * 31 + seed.charCodeAt(i)) >>> 0
  }
  return palette[hash % palette.length]
})

const coverLetter = computed(() => {
  const t = (props.story.title || props.story.id || '?').trim()
  return t.charAt(0).toUpperCase()
})

function handleDelete(e) {
  e.stopPropagation()
  showDeleteConfirm.value = true
}

function confirmDelete() {
  showDeleteConfirm.value = false
  emit('delete', props.story.id)
}
</script>

<template>
  <div
    class="story-card"
    :class="{ 'story-card--active': isActive }"
    @click="emit('select', story.id)"
  >
    <!-- Cover -->
    <div
      class="story-card__cover"
      :style="{ background: `linear-gradient(135deg, ${coverColor}, color-mix(in srgb, ${coverColor} 55%, transparent))` }"
    >
      {{ coverLetter }}
    </div>

    <!-- Info -->
    <div class="story-card__info">
      <div class="story-card__title-row">
        <span class="story-card__title">{{ story.title || story.id }}</span>
      </div>
      <div class="story-card__meta">
        <span>{{ story.chapters || 0 }} chapters</span>
        <span v-if="story.size">· {{ story.size }}</span>
      </div>

      <div class="story-card__progress-bar">
        <div
          class="story-card__progress-fill"
          :class="{ 'story-card__progress-fill--done': isComplete }"
          :style="{ width: progressPercent + '%' }"
        ></div>
      </div>

      <div class="story-card__badges">
        <span v-if="isComplete" class="story-card__pill story-card__pill--ok">
          <span class="story-card__pill-dot"></span>
          DONE
        </span>
        <span v-else-if="hasProgress" class="story-card__pill story-card__pill--active">
          <span class="story-card__pill-dot"></span>
          CONTINUE
        </span>
        <span v-else class="story-card__pill story-card__pill--muted">
          <span class="story-card__pill-dot"></span>
          NEW
        </span>
        <span v-if="lastPlayedLabel" class="story-card__last">{{ lastPlayedLabel }}</span>
        <span class="story-card__progress-text">
          {{ Math.round(progressPercent) }}%
          <template v-if="progressFraction">· {{ progressFraction }}</template>
        </span>
      </div>
    </div>

    <!-- Delete -->
    <button class="story-card__delete" @click="handleDelete" title="Delete story">
      <svg viewBox="0 0 24 24" fill="none" width="13" height="13">
        <path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6h14z"
          stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>

    <ConfirmModal
      :visible="showDeleteConfirm"
      title="Delete story"
      :message="`Are you sure you want to delete the story &quot;${story.title}&quot;?\nThe audio cache will also be removed.`"
      confirm-text="Delete"
      cancel-text="Cancel"
      variant="danger"
      @confirm="confirmDelete"
      @cancel="showDeleteConfirm = false"
    />
  </div>
</template>

<style scoped>
.story-card {
  display: grid;
  grid-template-columns: 48px 1fr auto;
  gap: 12px;
  padding: 12px;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  cursor: pointer;
  transition: border-color 0.15s var(--ease-out), background 0.15s var(--ease-out);
  position: relative;
  align-items: center;
}
.story-card:hover {
  border-color: var(--border-strong);
  background: var(--bg-3);
}
.story-card--active {
  background: var(--primary-bg);
  border-color: var(--primary);
}

/* Cover */
.story-card__cover {
  width: 48px;
  height: 48px;
  border-radius: var(--r-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #0E1019;
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 16px;
  flex-shrink: 0;
}

/* Info */
.story-card__info {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.story-card__title-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.story-card__title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.story-card__meta {
  font-size: 11px;
  color: var(--text-muted);
  display: flex;
  gap: 6px;
  font-family: var(--font-mono);
  letter-spacing: 0.02em;
}

/* Progress bar */
.story-card__progress-bar {
  height: 4px;
  background: var(--bg-3);
  border-radius: 999px;
  overflow: hidden;
  margin-top: 2px;
}
.story-card__progress-fill {
  height: 100%;
  background: var(--primary);
  border-radius: 999px;
  transition: width 0.3s var(--ease-out);
}
.story-card__progress-fill--done {
  background: var(--success);
}

/* Pill / badges row */
.story-card__badges {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 2px;
}
.story-card__pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  font-family: var(--font-mono);
  font-size: 9.5px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-muted);
}
.story-card__pill-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}
.story-card__pill--ok { color: var(--success); border-color: rgba(16,185,129,0.30); }
.story-card__pill--active { color: var(--primary-hover); border-color: var(--primary-bg-strong); }
.story-card__pill--muted { color: var(--text-muted); }
.story-card__last {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-subtle);
}
.story-card__progress-text {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-muted);
}

/* Delete */
.story-card__delete {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 0;
  background: transparent;
  color: var(--text-subtle);
  border-radius: var(--r-sm);
  opacity: 0;
  transition: opacity 0.15s var(--ease-out), background 0.15s var(--ease-out), color 0.15s var(--ease-out);
}
.story-card:hover .story-card__delete { opacity: 1; }
.story-card__delete:hover {
  background: var(--danger-bg);
  color: var(--danger);
}
</style>
