<script setup>
/**
 * ChapterRow — Một hàng chương trong danh sách.
 *
 * Props:
 *  - chapter: { file, preload } (preload: 'ready' | 'generating' | 'none')
 *  - isPlaying: boolean (chương này đang phát)
 *  - index: number (0-based)
 *  - effectivePreload: 'ready' | 'generating' | 'queued' | 'none' (from SSE overlay)
 *  - queuePosition: number (-1 = not queued, 1+ = position in queue)
 */
import { computed } from 'vue'

const props = defineProps({
  chapter: { type: Object, required: true },
  isPlaying: { type: Boolean, default: false },
  index: { type: Number, required: true },
  effectivePreload: { type: String, default: null },
  queuePosition: { type: Number, default: -1 },
})

const emit = defineEmits(['play', 'read'])

const chapterNum = computed(() => {
  const match = props.chapter.file.match(/^(\d+)/)
  return match ? parseInt(match[1], 10) : props.index + 1
})

const chapterTitle = computed(() => {
  const name = props.chapter.file.replace('.txt', '')
  const parts = name.split('_')
  if (parts.length <= 1) return name
  return parts.slice(1).map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
})

const statusType = computed(() => {
  if (props.isPlaying) return 'playing'
  // Use effective preload from SSE if available, else fallback to API preload
  return props.effectivePreload || props.chapter.preload || 'none'
})
</script>

<template>
  <div
    class="chapter-row"
    :class="{ 'chapter-row--playing': isPlaying }"
    @click="emit('play', chapter.file)"
  >
    <!-- Status indicator -->
    <div class="chapter-row__status" :class="'chapter-row__status--' + statusType">
      <template v-if="isPlaying">
        <div class="chapter-row__equalizer">
          <span></span><span></span><span></span>
        </div>
      </template>
      <template v-else-if="statusType === 'generating'">
        <div class="chapter-row__spinner"></div>
      </template>
      <template v-else-if="statusType === 'ready'">
        <div class="chapter-row__dot chapter-row__dot--ready"></div>
      </template>
      <template v-else-if="statusType === 'queued'">
        <div class="chapter-row__badge">{{ queuePosition > 0 ? '#' + queuePosition : '…' }}</div>
      </template>
      <template v-else>
        <div class="chapter-row__dot chapter-row__dot--none"></div>
      </template>
    </div>

    <!-- Info -->
    <div class="chapter-row__info">
      <span class="chapter-row__num">Ch.{{ chapterNum }}</span>
      <span class="chapter-row__title">{{ chapterTitle }}</span>
    </div>

    <!-- Actions -->
    <div class="chapter-row__actions">
      <button class="chapter-row__btn" @click.stop="emit('read', chapter.file)" title="Đọc text">
        <svg viewBox="0 0 24 24" fill="none" width="16" height="16">
          <path d="M2 3h6a4 4 0 014 4v14a3 3 0 00-3-3H2zM22 3h-6a4 4 0 00-4 4v14a3 3 0 013-3h7z"
            stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
      <button class="chapter-row__btn chapter-row__btn--play" @click.stop="emit('play', chapter.file)" title="Phát audio" aria-label="Play audio" data-testid="chapter-play">
        <svg v-if="!isPlaying" viewBox="0 0 24 24" fill="none" width="16" height="16">
          <polygon points="5,3 19,12 5,21" fill="currentColor"/>
        </svg>
        <svg v-else viewBox="0 0 24 24" fill="none" width="16" height="16">
          <rect x="6" y="4" width="4" height="16" rx="1" fill="currentColor"/>
          <rect x="14" y="4" width="4" height="16" rx="1" fill="currentColor"/>
        </svg>
      </button>
    </div>
  </div>
</template>

<style scoped>
.chapter-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.15s ease;
}
.chapter-row:hover {
  background: rgba(255,255,255,0.03);
}
.chapter-row--playing {
  background: rgba(59,130,246,0.08);
}

/* Status indicator */
.chapter-row__status {
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

/* Equalizer animation (playing) */
.chapter-row__equalizer {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 14px;
}
.chapter-row__equalizer span {
  display: block;
  width: 3px;
  background: var(--accent-blue, #3b82f6);
  border-radius: 1px;
  animation: eq-bounce 1s ease-in-out infinite;
}
.chapter-row__equalizer span:nth-child(1) { height: 60%; animation-delay: 0s; }
.chapter-row__equalizer span:nth-child(2) { height: 100%; animation-delay: 0.15s; }
.chapter-row__equalizer span:nth-child(3) { height: 40%; animation-delay: 0.3s; }
@keyframes eq-bounce {
  0%, 100% { transform: scaleY(0.4); }
  50% { transform: scaleY(1); }
}

/* Spinner (generating) */
.chapter-row__spinner {
  width: 14px;
  height: 14px;
  border: 2px solid rgba(245, 158, 11, 0.2);
  border-top-color: var(--status-warning, #f59e0b);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}

/* Dots */
.chapter-row__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.chapter-row__dot--ready { background: var(--status-success, #10b981); }
.chapter-row__dot--none { background: var(--text-subtle, #555872); }

/* Queue badge */
.chapter-row__badge {
  font-size: 9px;
  font-weight: 700;
  color: var(--accent-blue, #3b82f6);
  background: rgba(59, 130, 246, 0.12);
  border: 1px solid rgba(59, 130, 246, 0.25);
  border-radius: 4px;
  padding: 1px 4px;
  line-height: 1.2;
  white-space: nowrap;
}

/* Info */
.chapter-row__info {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.chapter-row__num {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted, #8b8fa3);
  white-space: nowrap;
}
.chapter-row__title {
  font-size: 13px;
  color: var(--text-secondary, #c4c8d4);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chapter-row--playing .chapter-row__title {
  color: var(--accent-blue, #3b82f6);
  font-weight: 500;
}

/* Action buttons */
.chapter-row__actions {
  display: flex;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.15s ease;
}
.chapter-row:hover .chapter-row__actions { opacity: 1; }
.chapter-row--playing .chapter-row__actions { opacity: 1; }

.chapter-row__btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border: none;
  background: transparent;
  color: var(--text-muted, #8b8fa3);
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s ease;
}
.chapter-row__btn:hover {
  background: rgba(255,255,255,0.06);
  color: var(--text-heading, #f0f2f5);
}
.chapter-row__btn--play:hover {
  background: rgba(59,130,246,0.15);
  color: var(--accent-blue, #3b82f6);
}

/* Mobile: always show actions (no hover on touch devices) */
@media (max-width: 768px) {
  .chapter-row__actions {
    opacity: 1;
  }
}
</style>
