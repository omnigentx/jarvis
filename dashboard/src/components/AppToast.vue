<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'

const props = defineProps({
  id: { type: [String, Number], required: true },
  type: { type: String, default: 'info' }, // success | error | warning | info
  title: { type: String, required: true },
  description: { type: String, default: '' },
  duration: { type: Number, default: 4000 },
})

const emit = defineEmits(['dismiss'])

const isVisible = ref(false)
const isLeaving = ref(false)
const isPaused = ref(false)
let timer = null
let elapsed = 0
let startTime = 0

const typeConfig = computed(() => {
  const configs = {
    success: {
      icon: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="7" stroke="#34d399" stroke-width="1.5"/><path d="M5 8.5l2 2 4-4.5" stroke="#34d399" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
      accentColor: '#34d399',
      bgColor: 'rgba(13, 26, 18, 0.94)',
      borderColor: 'rgba(26, 51, 40, 0.6)',
      glowColor: 'rgba(52, 211, 153, 0.06)',
    },
    error: {
      icon: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="7" stroke="#f87171" stroke-width="1.5"/><path d="M6 6l4 4M10 6l-4 4" stroke="#f87171" stroke-width="1.5" stroke-linecap="round"/></svg>`,
      accentColor: '#f87171',
      bgColor: 'rgba(26, 13, 13, 0.94)',
      borderColor: 'rgba(61, 31, 31, 0.6)',
      glowColor: 'rgba(248, 113, 113, 0.06)',
    },
    warning: {
      icon: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M8 2L14.5 13H1.5L8 2z" stroke="#fbbf24" stroke-width="1.3" stroke-linejoin="round"/><path d="M8 6.5v3" stroke="#fbbf24" stroke-width="1.5" stroke-linecap="round"/><circle cx="8" cy="11.5" r="0.7" fill="#fbbf24"/></svg>`,
      accentColor: '#fbbf24',
      bgColor: 'rgba(26, 21, 8, 0.94)',
      borderColor: 'rgba(61, 50, 24, 0.6)',
      glowColor: 'rgba(251, 191, 36, 0.06)',
    },
    info: {
      icon: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="7" stroke="#60a5fa" stroke-width="1.5"/><path d="M8 7v4" stroke="#60a5fa" stroke-width="1.5" stroke-linecap="round"/><circle cx="8" cy="5" r="0.7" fill="#60a5fa"/></svg>`,
      accentColor: '#60a5fa',
      bgColor: 'rgba(13, 18, 30, 0.94)',
      borderColor: 'rgba(30, 42, 72, 0.6)',
      glowColor: 'rgba(96, 165, 250, 0.06)',
    },
  }
  return configs[props.type] || configs.info
})

const progressPercent = ref(100)
let animFrame = null

function startTimer() {
  startTime = Date.now() - elapsed
  timer = setTimeout(() => {
    dismiss()
  }, props.duration - elapsed)
  tickProgress()
}

function tickProgress() {
  animFrame = requestAnimationFrame(() => {
    if (isPaused.value) return
    const now = Date.now()
    elapsed = now - startTime
    progressPercent.value = Math.max(0, 100 - (elapsed / props.duration) * 100)
    if (progressPercent.value > 0) {
      tickProgress()
    }
  })
}

function pauseTimer() {
  isPaused.value = true
  clearTimeout(timer)
  cancelAnimationFrame(animFrame)
}

function resumeTimer() {
  isPaused.value = false
  startTimer()
}

function dismiss() {
  isLeaving.value = true
  clearTimeout(timer)
  cancelAnimationFrame(animFrame)
  setTimeout(() => {
    emit('dismiss', props.id)
  }, 280)
}

onMounted(() => {
  // Trigger enter animation
  requestAnimationFrame(() => {
    isVisible.value = true
  })
  startTimer()
})

onBeforeUnmount(() => {
  clearTimeout(timer)
  cancelAnimationFrame(animFrame)
})
</script>

<template>
  <div
    class="toast-item"
    :class="{ 'toast-enter': isVisible, 'toast-leave': isLeaving }"
    :style="{
      '--toast-bg': typeConfig.bgColor,
      '--toast-border': typeConfig.borderColor,
      '--toast-accent': typeConfig.accentColor,
      '--toast-glow': typeConfig.glowColor,
    }"
    @mouseenter="pauseTimer"
    @mouseleave="resumeTimer"
  >
    <!-- Accent left bar -->
    <div class="toast-accent-bar" />

    <!-- Icon -->
    <div class="toast-icon" v-html="typeConfig.icon" />

    <!-- Content -->
    <div class="toast-content">
      <div class="toast-title">{{ title }}</div>
      <div v-if="description" class="toast-description">{{ description }}</div>
    </div>

    <!-- Close button -->
    <button class="toast-close" @click="dismiss" aria-label="Close notification">
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
        <path d="M4 4l6 6M10 4l-6 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
      </svg>
    </button>

    <!-- Progress bar -->
    <div class="toast-progress-track">
      <div
        class="toast-progress-bar"
        :style="{ width: progressPercent + '%' }"
      />
    </div>
  </div>
</template>

<style scoped>
.toast-item {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  width: 360px;
  padding: 12px 14px 14px 0;
  background: var(--toast-bg);
  border: 1px solid var(--toast-border);
  border-radius: 10px;
  overflow: hidden;
  pointer-events: auto;
  box-shadow:
    0 12px 40px rgba(0, 0, 0, 0.5),
    0 4px 12px rgba(0, 0, 0, 0.25),
    inset 0 1px 0 var(--toast-glow);
  backdrop-filter: blur(16px) saturate(1.3);
  -webkit-backdrop-filter: blur(16px) saturate(1.3);

  /* Enter animation */
  opacity: 0;
  transform: translateX(100%) scale(0.95);
  transition: opacity 0.3s cubic-bezier(0.16, 1, 0.3, 1),
              transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
}

.toast-item.toast-enter {
  opacity: 1;
  transform: translateX(0) scale(1);
}

.toast-item.toast-leave {
  opacity: 0;
  transform: translateX(40%) scale(0.95);
  transition: opacity 0.25s ease-in,
              transform 0.25s ease-in;
}

.toast-accent-bar {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--toast-accent);
  border-radius: 10px 0 0 10px;
  opacity: 0.8;
}

.toast-icon {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-left: 14px;
  margin-top: 1px;
  width: 18px;
  height: 18px;
}

.toast-content {
  flex: 1;
  min-width: 0;
}

.toast-title {
  font-size: 13px;
  font-weight: 500;
  line-height: 1.4;
  color: var(--toast-accent);
  letter-spacing: -0.01em;
}

.toast-description {
  font-size: 12px;
  font-weight: 400;
  line-height: 1.45;
  color: #8b8fa3;
  margin-top: 2px;
  word-break: break-word;
}

.toast-close {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  margin-top: 0px;
  background: rgba(30, 34, 51, 0.5);
  border: 1px solid rgba(42, 53, 86, 0.3);
  border-radius: 5px;
  color: #555872;
  cursor: pointer;
  transition: all 0.15s ease;
}

.toast-close:hover {
  background: rgba(42, 53, 86, 0.8);
  color: #f0f2f5;
  border-color: rgba(59, 130, 246, 0.3);
}

.toast-progress-track {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: rgba(255, 255, 255, 0.03);
}

.toast-progress-bar {
  height: 100%;
  background: var(--toast-accent);
  opacity: 0.35;
  border-radius: 0 1px 0 0;
  transition: width 0.1s linear;
}
</style>
