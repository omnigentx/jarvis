<script setup>
/**
 * MiniAudioPlayer — Fixed bottom player bar, persists across route changes.
 *
 * Mounted in AppLayout.vue, shown when audioPlayerStore.isMiniPlayerVisible = true.
 * Features: play/pause, next/prev, progress bar, speed, chapter label, expand to full.
 */
import { useAudioPlayerStore } from '../../stores/audioPlayer'
import AudioProgressBar from './AudioProgressBar.vue'

const store = useAudioPlayerStore()

function handleSeek(seconds) {
  store.seekTo(seconds)
}
</script>

<template>
  <div class="mini-player">
    <!-- Progress bar on top edge -->
    <div class="mini-player__progress-top">
      <AudioProgressBar
        :current="store.currentTime"
        :total="store.duration"
        :buffering="store.isBuffering"
        @seek="handleSeek"
      />
    </div>

    <div class="mini-player__content">
      <!-- Info -->
      <div class="mini-player__info" @click="store.isFullPlayerOpen = true">
        <div class="mini-player__title">{{ store.currentStoryTitle || 'Audio' }}</div>
        <div class="mini-player__chapter">{{ store.currentChapterLabel }}</div>
      </div>

      <!-- Time -->
      <div class="mini-player__time">
        {{ store.formattedTime }} / {{ store.formattedDuration }}
      </div>

      <!-- Controls -->
      <div class="mini-player__controls">
        <!-- Prev -->
        <button
          class="mini-player__btn"
          :disabled="!store.canPlayPrev"
          @click="store.prevChapter()"
          title="Previous chapter"
        >
          <svg viewBox="0 0 24 24" fill="none" width="18" height="18">
            <polygon points="19,20 9,12 19,4" fill="currentColor"/>
            <line x1="5" y1="4" x2="5" y2="20" stroke="currentColor" stroke-width="2"/>
          </svg>
        </button>

        <button class="mini-player__btn" @click="store.skipBackward(10)" title="-10s">
          <svg viewBox="0 0 24 24" fill="none" width="18" height="18">
            <polyline points="1,4 1,10 7,10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
          </svg>
        </button>

        <!-- Play/Pause -->
        <button class="mini-player__btn mini-player__btn--play" @click="store.togglePlayPause()">
          <!-- Buffering spinner -->
          <div v-if="store.isBuffering" class="mini-player__spinner"></div>
          <!-- Play -->
          <svg v-else-if="!store.isPlaying" viewBox="0 0 24 24" fill="none" width="22" height="22">
            <polygon points="6,3 20,12 6,21" fill="currentColor"/>
          </svg>
          <!-- Pause -->
          <svg v-else viewBox="0 0 24 24" fill="none" width="22" height="22">
            <rect x="6" y="4" width="4" height="16" rx="1" fill="currentColor"/>
            <rect x="14" y="4" width="4" height="16" rx="1" fill="currentColor"/>
          </svg>
        </button>

        <button class="mini-player__btn" @click="store.skipForward(30)" title="+30s">
          <svg viewBox="0 0 24 24" fill="none" width="18" height="18">
            <polyline points="23,4 23,10 17,10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
          </svg>
        </button>

        <!-- Next -->
        <button
          class="mini-player__btn"
          :disabled="!store.canPlayNext"
          @click="store.nextChapter()"
          title="Next chapter"
        >
          <svg viewBox="0 0 24 24" fill="none" width="18" height="18">
            <polygon points="5,4 15,12 5,20" fill="currentColor"/>
            <line x1="19" y1="4" x2="19" y2="20" stroke="currentColor" stroke-width="2"/>
          </svg>
        </button>

        <!-- Speed -->
        <button class="mini-player__btn mini-player__btn--speed" @click="store.cycleSpeed()" title="Playback speed">
          {{ store.playbackSpeed }}x
        </button>

        <!-- Close -->
        <button class="mini-player__btn mini-player__btn--close" @click="store.stopAndReset()" title="Close">
          <svg viewBox="0 0 24 24" fill="none" width="16" height="16">
            <path d="M18 6L6 18M6 6l12 12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
          </svg>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mini-player {
  position: fixed;
  bottom: 0;
  left: 180px; /* sidebar width */
  right: 0;
  z-index: 100;
  background: var(--bg-card, #0c0e15);
  border-top: 1px solid var(--border-primary, #1a1d2e);
  backdrop-filter: blur(12px);
  padding-bottom: env(safe-area-inset-bottom, 0px);
}

.mini-player__progress-top {
  padding: 0 16px;
  margin-top: -8px;
}

.mini-player__content {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 6px 20px 10px;
  min-height: 48px;
}

/* Info */
.mini-player__info {
  flex: 1;
  min-width: 0;
  cursor: pointer;
}
.mini-player__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-heading, #f0f2f5);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.mini-player__chapter {
  font-size: 11px;
  color: var(--text-muted, #8b8fa3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Time */
.mini-player__time {
  font-size: 11px;
  color: var(--text-muted, #8b8fa3);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

/* Controls */
.mini-player__controls {
  display: flex;
  align-items: center;
  gap: 2px;
}

.mini-player__btn {
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
  transition: all 0.15s ease;
}
.mini-player__btn:hover {
  background: rgba(255,255,255,0.06);
  color: var(--text-heading, #f0f2f5);
}
.mini-player__btn:disabled {
  opacity: 0.3;
  cursor: default;
}
.mini-player__btn:disabled:hover {
  background: transparent;
}

.mini-player__btn--play {
  width: 40px;
  height: 40px;
  background: var(--accent-blue, #3b82f6);
  color: white;
  border-radius: 50%;
  margin: 0 4px;
}
.mini-player__btn--play:hover {
  background: #4f8ff7;
  color: white;
}

.mini-player__btn--speed {
  font-size: 11px;
  font-weight: 700;
  width: auto;
  padding: 0 8px;
  color: var(--accent-blue, #3b82f6);
}

.mini-player__btn--close {
  color: var(--text-subtle, #555872);
}
.mini-player__btn--close:hover {
  color: var(--status-error, #ef4444);
  background: rgba(239, 68, 68, 0.1);
}

/* Spinner */
.mini-player__spinner {
  width: 20px;
  height: 20px;
  border: 2px solid rgba(255,255,255,0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}

/* Mobile responsive */
@media (max-width: 768px) {
  .mini-player {
    left: 0;
  }
  .mini-player__content {
    gap: 8px;
    padding: 6px 12px 10px;
  }
  .mini-player__time {
    display: none;
  }
  .mini-player__title {
    font-size: 12px;
  }
  .mini-player__chapter {
    font-size: 10px;
  }
  .mini-player__btn {
    width: 30px;
    height: 30px;
  }
  .mini-player__btn--play {
    width: 36px;
    height: 36px;
  }
  .mini-player__btn--speed {
    font-size: 10px;
    padding: 0 6px;
  }
}
</style>
