<script setup>
/**
 * FullAudioPlayer — Full-screen audio player (mobile + desktop expand).
 *
 * Shown when audioPlayerStore.isFullPlayerOpen = true.
 * Features: large controls, waveform-style progress, chapter info, speed.
 */
import { useAudioPlayerStore } from '../../stores/audioPlayer'
import AudioProgressBar from './AudioProgressBar.vue'

const store = useAudioPlayerStore()

function handleSeek(seconds) {
  store.seekTo(seconds)
}

function close() {
  store.isFullPlayerOpen = false
}
</script>

<template>
  <Teleport to="body">
    <Transition name="slide-up">
      <div v-if="store.isFullPlayerOpen" class="full-player" @click.self="close">
        <div class="full-player__container">
          <!-- Close handle -->
          <div class="full-player__handle" @click="close">
            <div class="full-player__handle-bar"></div>
          </div>

          <!-- Story info -->
          <div class="full-player__info">
            <div class="full-player__icon-wrapper">
              <svg viewBox="0 0 24 24" fill="none" width="48" height="48">
                <path d="M9 18V5l12-2v13" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                <circle cx="6" cy="18" r="3" stroke="currentColor" stroke-width="1.5"/>
                <circle cx="18" cy="16" r="3" stroke="currentColor" stroke-width="1.5"/>
              </svg>
            </div>
            <h2 class="full-player__title">{{ store.currentStoryTitle || 'Audio' }}</h2>
            <p class="full-player__chapter">{{ store.currentChapterLabel }}</p>
            <p class="full-player__progress-label">{{ store.chapterProgress }}</p>
          </div>

          <!-- Progress -->
          <div class="full-player__progress-area">
            <AudioProgressBar
              :current="store.currentTime"
              :total="store.duration"
              :buffering="store.isBuffering"
              @seek="handleSeek"
            />
            <div class="full-player__time-labels">
              <span>{{ store.formattedTime }}</span>
              <span>{{ store.formattedDuration }}</span>
            </div>
          </div>

          <!-- Large controls -->
          <div class="full-player__controls">
            <button
              class="full-player__btn"
              :disabled="!store.canPlayPrev"
              @click="store.prevChapter()"
            >
              <svg viewBox="0 0 24 24" fill="none" width="28" height="28">
                <polygon points="19,20 9,12 19,4" fill="currentColor"/>
                <line x1="5" y1="4" x2="5" y2="20" stroke="currentColor" stroke-width="2.5"/>
              </svg>
            </button>

            <button class="full-player__btn" @click="store.skipBackward(10)">
              <svg viewBox="0 0 24 24" fill="none" width="24" height="24">
                <polyline points="1,4 1,10 7,10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
              </svg>
              <span class="full-player__skip-label">10</span>
            </button>

            <button class="full-player__btn full-player__btn--play" @click="store.togglePlayPause()">
              <div v-if="store.isBuffering" class="full-player__spinner"></div>
              <svg v-else-if="!store.isPlaying" viewBox="0 0 24 24" fill="none" width="36" height="36">
                <polygon points="6,3 20,12 6,21" fill="currentColor"/>
              </svg>
              <svg v-else viewBox="0 0 24 24" fill="none" width="36" height="36">
                <rect x="5" y="3" width="5" height="18" rx="1.5" fill="currentColor"/>
                <rect x="14" y="3" width="5" height="18" rx="1.5" fill="currentColor"/>
              </svg>
            </button>

            <button class="full-player__btn" @click="store.skipForward(30)">
              <svg viewBox="0 0 24 24" fill="none" width="24" height="24">
                <polyline points="23,4 23,10 17,10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
              </svg>
              <span class="full-player__skip-label">30</span>
            </button>

            <button
              class="full-player__btn"
              :disabled="!store.canPlayNext"
              @click="store.nextChapter()"
            >
              <svg viewBox="0 0 24 24" fill="none" width="28" height="28">
                <polygon points="5,4 15,12 5,20" fill="currentColor"/>
                <line x1="19" y1="4" x2="19" y2="20" stroke="currentColor" stroke-width="2.5"/>
              </svg>
            </button>
          </div>

          <!-- Speed control -->
          <div class="full-player__speed">
            <span class="full-player__speed-label">Speed</span>
            <div class="full-player__speed-options">
              <button
                v-for="speed in store.SPEED_OPTIONS"
                :key="speed"
                class="full-player__speed-btn"
                :class="{ 'full-player__speed-btn--active': store.playbackSpeed === speed }"
                @click="store.setSpeed(speed)"
              >
                {{ speed }}x
              </button>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.full-player {
  position: fixed;
  inset: 0;
  z-index: 200;
  background: rgba(0,0,0,0.7);
  display: flex;
  align-items: flex-end;
  justify-content: center;
  backdrop-filter: blur(4px);
}

.full-player__container {
  width: 100%;
  max-width: 480px;
  background: var(--bg-card, #0c0e15);
  border-radius: 20px 20px 0 0;
  padding: 12px 24px 32px;
  padding-bottom: calc(32px + env(safe-area-inset-bottom, 0px));
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 24px;
}

/* Handle */
.full-player__handle {
  width: 100%;
  display: flex;
  justify-content: center;
  padding: 4px 0;
  cursor: pointer;
}
.full-player__handle-bar {
  width: 36px;
  height: 4px;
  background: rgba(255,255,255,0.15);
  border-radius: 2px;
}

/* Info */
.full-player__info {
  text-align: center;
}
.full-player__icon-wrapper {
  width: 88px;
  height: 88px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, rgba(59,130,246,0.15), rgba(99,102,241,0.15));
  border-radius: 20px;
  margin: 0 auto 16px;
  color: var(--accent-blue, #3b82f6);
}
.full-player__title {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-heading, #f0f2f5);
  margin: 0 0 4px;
}
.full-player__chapter {
  font-size: 14px;
  color: var(--text-secondary, #c4c8d4);
  margin: 0 0 2px;
}
.full-player__progress-label {
  font-size: 12px;
  color: var(--text-muted, #8b8fa3);
  margin: 0;
}

/* Progress area */
.full-player__progress-area {
  width: 100%;
}
.full-player__time-labels {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--text-muted, #8b8fa3);
  font-variant-numeric: tabular-nums;
  margin-top: 4px;
}

/* Controls */
.full-player__controls {
  display: flex;
  align-items: center;
  gap: 12px;
}
.full-player__btn {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  width: 48px;
  height: 48px;
  border: none;
  background: transparent;
  color: var(--text-secondary, #c4c8d4);
  border-radius: 50%;
  cursor: pointer;
  transition: all 0.15s;
  position: relative;
}
.full-player__btn:hover {
  background: rgba(255,255,255,0.06);
  color: var(--text-heading, #f0f2f5);
}
.full-player__btn:disabled {
  opacity: 0.3;
  cursor: default;
}

.full-player__btn--play {
  width: 64px;
  height: 64px;
  background: var(--accent-blue, #3b82f6);
  color: white;
}
.full-player__btn--play:hover {
  background: #4f8ff7;
}

.full-player__skip-label {
  font-size: 9px;
  font-weight: 700;
  margin-top: -2px;
}

/* Spinner */
.full-player__spinner {
  width: 28px;
  height: 28px;
  border: 3px solid rgba(255,255,255,0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* Speed */
.full-player__speed {
  width: 100%;
}
.full-player__speed-label {
  display: block;
  font-size: 12px;
  color: var(--text-muted, #8b8fa3);
  margin-bottom: 8px;
  text-align: center;
}
.full-player__speed-options {
  display: flex;
  gap: 6px;
  justify-content: center;
}
.full-player__speed-btn {
  padding: 6px 14px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary, #c4c8d4);
  background: rgba(255,255,255,0.04);
  border: 1px solid var(--border-primary, #1a1d2e);
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.15s;
}
.full-player__speed-btn:hover {
  background: rgba(255,255,255,0.08);
}
.full-player__speed-btn--active {
  background: rgba(59,130,246,0.15);
  border-color: var(--accent-blue, #3b82f6);
  color: var(--accent-blue, #3b82f6);
}

/* Animation */
.slide-up-enter-active,
.slide-up-leave-active {
  transition: all 0.3s ease;
}
.slide-up-enter-from {
  opacity: 0;
}
.slide-up-enter-from .full-player__container {
  transform: translateY(100%);
}
.slide-up-leave-to {
  opacity: 0;
}
.slide-up-leave-to .full-player__container {
  transform: translateY(100%);
}

/* Desktop */
@media (min-width: 769px) {
  .full-player__container {
    border-radius: 20px;
    margin-bottom: 80px; /* above mini player */
  }
}
</style>
