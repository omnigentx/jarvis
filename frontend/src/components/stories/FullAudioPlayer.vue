<script setup>
/**
 * FullAudioPlayer — full-screen audio player (mobile + desktop expand).
 * Logic preserved; visuals restyled to match new design tokens.
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
      <div v-if="store.isFullPlayerOpen" class="full-player jv" @click.self="close">
        <div class="full-player__container">
          <div class="full-player__handle" @click="close">
            <div class="full-player__handle-bar"></div>
          </div>

          <div class="full-player__info">
            <div class="full-player__icon-wrapper">
              <svg viewBox="0 0 24 24" fill="none" width="42" height="42">
                <path d="M9 18V5l12-2v13" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                <circle cx="6" cy="18" r="3" stroke="currentColor" stroke-width="1.5"/>
                <circle cx="18" cy="16" r="3" stroke="currentColor" stroke-width="1.5"/>
              </svg>
            </div>
            <h2 class="full-player__title">{{ store.currentStoryTitle || 'Audio' }}</h2>
            <p class="full-player__chapter">{{ store.currentChapterLabel }}</p>
            <p class="full-player__progress-label">{{ store.chapterProgress }}</p>
          </div>

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

          <div class="full-player__controls">
            <button class="full-player__btn" :disabled="!store.canPlayPrev" @click="store.prevChapter()">
              <svg viewBox="0 0 24 24" fill="none" width="26" height="26">
                <polygon points="19,20 9,12 19,4" fill="currentColor"/>
                <line x1="5" y1="4" x2="5" y2="20" stroke="currentColor" stroke-width="2.5"/>
              </svg>
            </button>

            <button class="full-player__btn" @click="store.skipBackward(10)">
              <svg viewBox="0 0 24 24" fill="none" width="22" height="22">
                <polyline points="1,4 1,10 7,10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
              </svg>
              <span class="full-player__skip-label">10</span>
            </button>

            <button class="full-player__btn full-player__btn--play" @click="store.togglePlayPause()">
              <div v-if="store.isBuffering" class="full-player__spinner"></div>
              <svg v-else-if="!store.isPlaying" viewBox="0 0 24 24" fill="none" width="32" height="32">
                <polygon points="6,3 20,12 6,21" fill="currentColor"/>
              </svg>
              <svg v-else viewBox="0 0 24 24" fill="none" width="32" height="32">
                <rect x="5" y="3" width="5" height="18" rx="1.5" fill="currentColor"/>
                <rect x="14" y="3" width="5" height="18" rx="1.5" fill="currentColor"/>
              </svg>
            </button>

            <button class="full-player__btn" @click="store.skipForward(30)">
              <svg viewBox="0 0 24 24" fill="none" width="22" height="22">
                <polyline points="23,4 23,10 17,10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
              </svg>
              <span class="full-player__skip-label">30</span>
            </button>

            <button class="full-player__btn" :disabled="!store.canPlayNext" @click="store.nextChapter()">
              <svg viewBox="0 0 24 24" fill="none" width="26" height="26">
                <polygon points="5,4 15,12 5,20" fill="currentColor"/>
                <line x1="19" y1="4" x2="19" y2="20" stroke="currentColor" stroke-width="2.5"/>
              </svg>
            </button>
          </div>

          <div class="full-player__speed">
            <span class="mono-label">Speed</span>
            <div class="seg full-player__speed-options">
              <button
                v-for="speed in store.SPEED_OPTIONS"
                :key="speed"
                :class="{ 'is-active': store.playbackSpeed === speed }"
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
  background: var(--bg-overlay);
  display: flex;
  align-items: flex-end;
  justify-content: center;
  backdrop-filter: blur(8px);
}

.full-player__container {
  width: 100%;
  max-width: 480px;
  background: var(--bg-1);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xl) var(--r-xl) 0 0;
  padding: 12px 24px 32px;
  padding-bottom: calc(32px + env(safe-area-inset-bottom, 0px));
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 20px;
}

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
  background: var(--border-bright);
  border-radius: 999px;
}

.full-player__info {
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}
.full-player__icon-wrapper {
  width: 80px;
  height: 80px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, var(--primary-bg-strong), var(--accent-bg));
  border-radius: var(--r-xl);
  margin-bottom: 10px;
  color: var(--accent);
}
.full-player__title {
  font-size: 17px;
  font-weight: 600;
  color: var(--text);
}
.full-player__chapter {
  font-size: 13px;
  color: var(--text-dim);
}
.full-player__progress-label {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
}

.full-player__progress-area {
  width: 100%;
}
.full-player__time-labels {
  display: flex;
  justify-content: space-between;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 4px;
}

.full-player__controls {
  display: flex;
  align-items: center;
  gap: 10px;
}
.full-player__btn {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border: 0;
  background: transparent;
  color: var(--text-dim);
  border-radius: 50%;
  cursor: pointer;
  transition: all 0.15s var(--ease-out);
  position: relative;
}
.full-player__btn:hover {
  background: var(--bg-3);
  color: var(--text);
}
.full-player__btn:disabled {
  opacity: 0.3;
  cursor: default;
}

.full-player__btn--play {
  width: 60px;
  height: 60px;
  background: linear-gradient(180deg, var(--primary-hover), var(--primary));
  color: white;
  box-shadow: 0 4px 18px var(--primary-glow);
}
.full-player__btn--play:hover {
  background: linear-gradient(180deg, var(--primary-hover), var(--primary));
  color: white;
}

.full-player__skip-label {
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 700;
  margin-top: -2px;
}

.full-player__spinner {
  width: 26px;
  height: 26px;
  border: 3px solid rgba(255,255,255,0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

.full-player__speed {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.full-player__speed-options {
  width: max-content;
}

.slide-up-enter-active,
.slide-up-leave-active {
  transition: all 0.28s var(--ease-out);
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

@media (min-width: 769px) {
  .full-player__container {
    border-radius: var(--r-xl);
    margin-bottom: 80px;
  }
}
</style>
