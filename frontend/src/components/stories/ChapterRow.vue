<script setup>
/**
 * ChapterRow — single chapter row in the chapter list.
 * Logic preserved; visual shell restyled to match the redesign
 * (Ch.xx · title · status dot · duration · actions).
 */
import { computed } from 'vue'
import { useLang } from '../../composables/useLang'

const { t } = useLang()

const props = defineProps({
  chapter: { type: Object, required: true },
  // Actively playing (not paused) — drives the pause icon + eq animation.
  isPlaying: { type: Boolean, default: false },
  // This chapter is the one loaded in the player (playing OR paused) — drives
  // the row highlight so a paused chapter stays visually selected.
  isCurrent: { type: Boolean, default: false },
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
  if (props.isCurrent) return 'playing'
  return props.effectivePreload || props.chapter.preload || 'none'
})
</script>

<template>
  <div
    class="ch-row"
    :class="{ 'ch-row--playing': isCurrent }"
    @click="emit('play', chapter.file)"
  >
    <span class="ch-row__num">Ch.{{ String(chapterNum).padStart(2, '0') }}</span>

    <!-- Status indicator -->
    <span class="ch-row__status" :data-status="statusType">
      <template v-if="isPlaying">
        <span class="ch-row__eq"><span></span><span></span><span></span></span>
      </template>
      <template v-else-if="statusType === 'generating'">
        <span class="ch-row__spinner"></span>
      </template>
      <template v-else-if="statusType === 'ready'">
        <span class="ch-row__dot ch-row__dot--ready"></span>
      </template>
      <template v-else-if="statusType === 'queued'">
        <span class="ch-row__queue">{{ queuePosition > 0 ? '#' + queuePosition : '…' }}</span>
      </template>
      <template v-else>
        <span class="ch-row__dot ch-row__dot--none"></span>
      </template>
    </span>

    <!-- Title -->
    <span class="ch-row__title">{{ chapterTitle }}</span>

    <!-- Actions -->
    <span class="ch-row__actions">
      <button class="btn btn-icon btn-ghost ch-row__btn" @click.stop="emit('read', chapter.file)" :title="t('stories.readText')">
        <svg viewBox="0 0 24 24" fill="none" width="13" height="13">
          <path d="M2 3h6a4 4 0 014 4v14a3 3 0 00-3-3H2zM22 3h-6a4 4 0 00-4 4v14a3 3 0 013-3h7z"
            stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
      <button class="btn btn-icon btn-ghost ch-row__btn ch-row__btn--play"
        @click.stop="emit('play', chapter.file)" :title="t('stories.playAudio')" data-testid="chapter-play">
        <svg v-if="!isPlaying" viewBox="0 0 24 24" fill="none" width="13" height="13">
          <polygon points="5,3 19,12 5,21" fill="currentColor"/>
        </svg>
        <svg v-else viewBox="0 0 24 24" fill="none" width="13" height="13">
          <rect x="6" y="4" width="4" height="16" rx="1" fill="currentColor"/>
          <rect x="14" y="4" width="4" height="16" rx="1" fill="currentColor"/>
        </svg>
      </button>
    </span>
  </div>
</template>

<style scoped>
.ch-row {
  display: grid;
  grid-template-columns: 56px 24px 1fr auto;
  align-items: center;
  gap: 12px;
  padding: 8px 18px;
  border-left: 3px solid transparent;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  transition: background 0.12s var(--ease-out);
}
.ch-row:hover {
  background: var(--bg-2);
}
.ch-row--playing {
  background: var(--primary-bg);
  border-left-color: var(--primary);
}

.ch-row__num {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  letter-spacing: 0.02em;
}

/* Status */
.ch-row__status {
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.ch-row__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.ch-row__dot--ready { background: var(--success); box-shadow: 0 0 6px rgba(16,185,129,0.45); }
.ch-row__dot--none { background: var(--bg-4); border: 1px solid var(--border-bright); }

.ch-row__eq {
  display: inline-flex;
  align-items: flex-end;
  gap: 2px;
  height: 12px;
}
.ch-row__eq span {
  display: block;
  width: 3px;
  background: var(--primary);
  border-radius: 1px;
  animation: eq-bounce 1s ease-in-out infinite;
}
.ch-row__eq span:nth-child(1) { height: 60%; animation-delay: 0s; }
.ch-row__eq span:nth-child(2) { height: 100%; animation-delay: 0.15s; }
.ch-row__eq span:nth-child(3) { height: 40%; animation-delay: 0.3s; }
@keyframes eq-bounce {
  0%, 100% { transform: scaleY(0.4); }
  50% { transform: scaleY(1); }
}

.ch-row__spinner {
  width: 13px;
  height: 13px;
  border: 2px solid rgba(245, 158, 11, 0.2);
  border-top-color: var(--warning);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

.ch-row__queue {
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 700;
  color: var(--primary-hover);
  background: var(--primary-bg);
  border: 1px solid var(--primary-bg-strong);
  border-radius: 3px;
  padding: 1px 4px;
}

/* Title */
.ch-row__title {
  font-size: 13px;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}
.ch-row--playing .ch-row__title {
  color: var(--primary-hover);
  font-weight: 500;
}

/* Actions */
.ch-row__actions {
  display: flex;
  gap: 2px;
  opacity: 0;
  transition: opacity 0.12s var(--ease-out);
}
.ch-row:hover .ch-row__actions,
.ch-row--playing .ch-row__actions { opacity: 1; }

.ch-row__btn {
  width: 26px;
  height: 26px;
  color: var(--text-muted);
}
.ch-row__btn--play:hover { color: var(--primary-hover); }

@media (max-width: 768px) {
  .ch-row__actions { opacity: 1; }
  /* 26px tap targets were below the iOS HIG 44px minimum. Bump to
     38 — works inside the row height without breaking layout. */
  .ch-row__btn { width: 38px; height: 38px; }
}
</style>
