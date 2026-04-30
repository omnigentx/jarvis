<script setup>
/**
 * StoryCard — Card hiển thị truyện trong library grid.
 *
 * Props:
 *  - story: { id, title, chapters, last_chapter_file, last_chapter_num, last_played_at }
 *
 * Emits:
 *  - select: khi click chọn truyện
 *  - delete: khi xác nhận xóa
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
const progressText = computed(() => {
  if (!hasProgress.value) return null
  return `Ch.${props.story.last_chapter_num || 0} / ${props.story.chapters}`
})
const progressPercent = computed(() => {
  if (!hasProgress.value || !props.story.chapters) return 0
  return Math.min(((props.story.last_chapter_num || 0) / props.story.chapters) * 100, 100)
})

const lastPlayedLabel = computed(() => {
  if (!props.story.last_played_at) return null
  const diff = Date.now() / 1000 - props.story.last_played_at
  if (diff < 60) return 'Vừa xong'
  if (diff < 3600) return `${Math.floor(diff / 60)} phút trước`
  if (diff < 86400) return `${Math.floor(diff / 3600)} giờ trước`
  return `${Math.floor(diff / 86400)} ngày trước`
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
    <!-- Cover placeholder with chapter count -->
    <div class="story-card__cover">
      <svg viewBox="0 0 24 24" fill="none" class="story-card__icon">
        <path d="M4 19.5A2.5 2.5 0 016.5 17H20" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span class="story-card__chapter-count">{{ story.chapters }} chương</span>
    </div>

    <!-- Info -->
    <div class="story-card__info">
      <h3 class="story-card__title">{{ story.title }}</h3>

      <!-- Progress -->
      <div v-if="hasProgress" class="story-card__progress">
        <div class="story-card__progress-bar">
          <div class="story-card__progress-fill" :style="{ width: progressPercent + '%' }"></div>
        </div>
        <span class="story-card__progress-text">{{ progressText }}</span>
      </div>

      <!-- Continue badge -->
      <div v-if="hasProgress" class="story-card__continue">
        <span class="story-card__continue-dot"></span>
        <span>Tiếp tục · {{ lastPlayedLabel }}</span>
      </div>
    </div>

    <!-- Delete button -->
    <button class="story-card__delete" @click="handleDelete" title="Xóa truyện">
      <svg viewBox="0 0 24 24" fill="none" width="14" height="14">
        <path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6h14z"
          stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>

    <!-- Delete Confirmation -->
    <ConfirmModal
      :visible="showDeleteConfirm"
      title="Xóa truyện"
      :message="`Bạn có chắc muốn xóa truyện &quot;${story.title}&quot;?\nAudio cache cũng sẽ bị xóa.`"
      confirm-text="Xóa"
      cancel-text="Hủy"
      variant="danger"
      @confirm="confirmDelete"
      @cancel="showDeleteConfirm = false"
    />
  </div>
</template>

<style scoped>
.story-card {
  display: flex;
  gap: 14px;
  padding: 14px 16px;
  background: var(--bg-card, #0c0e15);
  border: 1px solid var(--border-primary, #1a1d2e);
  border-radius: 10px;
  cursor: pointer;
  transition: all 0.2s ease;
  position: relative;
}
.story-card:hover {
  border-color: var(--border-hover, #2a3556);
  background: var(--bg-card-hover, #111318);
}
.story-card--active {
  border-color: var(--accent-blue, #3b82f6);
  background: rgba(59, 130, 246, 0.06);
}

/* Cover */
.story-card__cover {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  width: 56px;
  min-width: 56px;
  height: 72px;
  background: linear-gradient(135deg, rgba(59,130,246,0.12), rgba(99,102,241,0.12));
  border-radius: 8px;
  gap: 4px;
}
.story-card__icon {
  width: 22px;
  height: 22px;
  color: var(--accent-blue, #3b82f6);
}
.story-card__chapter-count {
  font-size: 9px;
  font-weight: 600;
  color: var(--text-muted, #8b8fa3);
  white-space: nowrap;
}

/* Info */
.story-card__info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  justify-content: center;
}
.story-card__title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-heading, #f0f2f5);
  line-height: 1.3;
  margin: 0;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* Progress bar */
.story-card__progress {
  display: flex;
  align-items: center;
  gap: 8px;
}
.story-card__progress-bar {
  flex: 1;
  height: 3px;
  background: rgba(255,255,255,0.06);
  border-radius: 2px;
  overflow: hidden;
}
.story-card__progress-fill {
  height: 100%;
  background: var(--accent-blue, #3b82f6);
  border-radius: 2px;
  transition: width 0.3s ease;
}
.story-card__progress-text {
  font-size: 11px;
  color: var(--text-muted, #8b8fa3);
  white-space: nowrap;
}

/* Continue badge */
.story-card__continue {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--status-success, #10b981);
}
.story-card__continue-dot {
  width: 6px;
  height: 6px;
  background: var(--status-success, #10b981);
  border-radius: 50%;
  animation: pulse 2s infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

/* Delete */
.story-card__delete {
  position: absolute;
  top: 8px;
  right: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: none;
  background: transparent;
  color: var(--text-subtle, #555872);
  border-radius: 6px;
  cursor: pointer;
  opacity: 0;
  transition: all 0.15s ease;
}
.story-card:hover .story-card__delete { opacity: 1; }
.story-card__delete:hover {
  background: rgba(239, 68, 68, 0.1);
  color: var(--status-error, #ef4444);
}
</style>
