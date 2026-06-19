<script setup>
/**
 * MiniAudioPlayer — fixed bottom player bar, persists across routes.
 * Mounted in AppLayout when audioPlayerStore.isMiniPlayerVisible is true.
 * Logic untouched; restyled to match the new design.
 */
import { useAudioPlayerStore } from '../../stores/audioPlayer'
import { useLang } from '../../composables/useLang'
import AudioProgressBar from './AudioProgressBar.vue'

const store = useAudioPlayerStore()
const { t } = useLang()

function handleSeek(seconds) {
  store.seekTo(seconds)
}
</script>

<template>
  <div class="mini-player">
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
        <div class="mini-player__title">{{ store.currentStoryTitle || t('stories.audio') }}</div>
        <div class="mini-player__chapter">{{ store.currentChapterLabel }}</div>
      </div>

      <!-- Time -->
      <div class="mini-player__time">
        {{ store.formattedTime }} / {{ store.formattedDuration }}
      </div>

      <!-- Controls -->
      <div class="mini-player__controls">
        <button
          class="mini-player__btn"
          :disabled="!store.canPlayPrev"
          @click="store.prevChapter()"
          :title="t('stories.prevChapter')"
        >
          <svg viewBox="0 0 24 24" fill="none" width="16" height="16">
            <polygon points="19,20 9,12 19,4" fill="currentColor"/>
            <line x1="5" y1="4" x2="5" y2="20" stroke="currentColor" stroke-width="2"/>
          </svg>
        </button>

        <button class="mini-player__btn mini-player__btn--skip" @click="store.skipBackward(10)" :title="t('stories.back10')">
          <svg viewBox="0 0 24 24" fill="none" width="16" height="16">
            <polyline points="1,4 1,10 7,10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
          </svg>
        </button>

        <button class="mini-player__btn mini-player__btn--play" @click="store.togglePlayPause()">
          <div v-if="store.isBuffering" class="mini-player__spinner"></div>
          <svg v-else-if="!store.isPlaying" viewBox="0 0 24 24" fill="none" width="18" height="18">
            <polygon points="6,3 20,12 6,21" fill="currentColor"/>
          </svg>
          <svg v-else viewBox="0 0 24 24" fill="none" width="18" height="18">
            <rect x="6" y="4" width="4" height="16" rx="1" fill="currentColor"/>
            <rect x="14" y="4" width="4" height="16" rx="1" fill="currentColor"/>
          </svg>
        </button>

        <button class="mini-player__btn mini-player__btn--skip" @click="store.skipForward(30)" :title="t('stories.fwd30')">
          <svg viewBox="0 0 24 24" fill="none" width="16" height="16">
            <polyline points="23,4 23,10 17,10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
          </svg>
        </button>

        <button
          class="mini-player__btn"
          :disabled="!store.canPlayNext"
          @click="store.nextChapter()"
          :title="t('stories.nextChapter')"
        >
          <svg viewBox="0 0 24 24" fill="none" width="16" height="16">
            <polygon points="5,4 15,12 5,20" fill="currentColor"/>
            <line x1="19" y1="4" x2="19" y2="20" stroke="currentColor" stroke-width="2"/>
          </svg>
        </button>

        <button class="mini-player__btn mini-player__btn--speed" @click="store.cycleSpeed()" :title="t('stories.playbackSpeed')">
          {{ store.playbackSpeed }}x
        </button>

        <button class="mini-player__btn mini-player__btn--close" @click="store.stopAndReset()" :title="t('common.close')">
          <svg viewBox="0 0 24 24" fill="none" width="14" height="14">
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
  left: var(--sidebar-w, 248px);
  right: 0;
  z-index: 100;
  background: var(--bg-1);
  border-top: 1px solid var(--border);
  backdrop-filter: blur(12px);
  padding-bottom: env(safe-area-inset-bottom, 0px);
}

/* Mobile: sit above the bottom tab bar, span full width (no sidebar). */
@media (max-width: 767px) {
  .mini-player {
    left: 0;
    bottom: calc(var(--mobile-tabbar-h) + var(--safe-bottom));
    /* Above tabbar's z-index (180) so the player stays interactive
       even when sticky composers exist beneath. */
    z-index: 190;
    padding-bottom: 0; /* tabbar already handles safe-area-bottom */
  }
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

.mini-player__info {
  flex: 1;
  min-width: 0;
  cursor: pointer;
}
.mini-player__title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.mini-player__chapter {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  letter-spacing: 0.04em;
}

.mini-player__time {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.mini-player__controls {
  display: flex;
  align-items: center;
  gap: 2px;
}

.mini-player__btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: 0;
  background: transparent;
  color: var(--text-dim);
  border-radius: var(--r-md);
  cursor: pointer;
  transition: all 0.15s var(--ease-out);
}
.mini-player__btn:hover {
  background: var(--bg-3);
  color: var(--text);
}
.mini-player__btn:disabled {
  opacity: 0.3;
  cursor: default;
}
.mini-player__btn:disabled:hover {
  background: transparent;
}

.mini-player__btn--play {
  width: 38px;
  height: 38px;
  background: linear-gradient(180deg, var(--primary-hover), var(--primary));
  color: white;
  border-radius: 50%;
  margin: 0 4px;
  box-shadow: 0 4px 14px var(--primary-glow);
}
.mini-player__btn--play:hover {
  background: linear-gradient(180deg, var(--primary-hover), var(--primary));
  color: white;
  transform: translateY(-1px);
}

.mini-player__btn--speed {
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 600;
  width: auto;
  padding: 0 8px;
  color: var(--accent);
  letter-spacing: 0.04em;
}

.mini-player__btn--close { color: var(--text-subtle); }
.mini-player__btn--close:hover {
  color: var(--danger);
  background: var(--danger-bg);
}

.mini-player__spinner {
  width: 18px;
  height: 18px;
  border: 2px solid rgba(255,255,255,0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 768px) {
  .mini-player {
    left: 0;
  }
  .mini-player__content {
    gap: 8px;
    padding: 6px 12px 10px;
  }
  .mini-player__time { display: none; }
  .mini-player__title { font-size: 12px; }
  .mini-player__chapter { font-size: 10px; }
  .mini-player__btn {
    width: 30px;
    height: 30px;
  }
  .mini-player__btn--play { width: 34px; height: 34px; }
  .mini-player__btn--speed { font-size: 10px; padding: 0 6px; }
}

/* At iPhone Mini width the row of 7+ controls crowds out the title.
   Skip buttons are nice-to-have (the progress bar covers fine seek)
   — drop them so prev/play/next + close stay reachable. */
@media (max-width: 380px) {
  .mini-player__btn--skip { display: none; }
  .mini-player__chapter { display: none; }
}
</style>
