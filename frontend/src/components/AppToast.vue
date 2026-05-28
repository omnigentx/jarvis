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

// Visual contract: each toast role maps to a single token (no
// hard-coded hex). Background stays the elevated bg-3, only the
// accent border + icon colour changes per role.
const typeConfig = computed(() => {
  const configs = {
    success: {
      icon: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="7" stroke="currentColor" stroke-width="1.5"/><path d="M5 8.5l2 2 4-4.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
      accentVar: '--success',
    },
    error: {
      icon: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="7" stroke="currentColor" stroke-width="1.5"/><path d="M6 6l4 4M10 6l-4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
      accentVar: '--danger',
    },
    warning: {
      icon: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M8 2L14.5 13H1.5L8 2z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M8 6.5v3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="8" cy="11.5" r="0.7" fill="currentColor"/></svg>`,
      accentVar: '--warning',
    },
    info: {
      icon: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="7" stroke="currentColor" stroke-width="1.5"/><path d="M8 7v4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="8" cy="5" r="0.7" fill="currentColor"/></svg>`,
      accentVar: '--info',
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
    class="toast-item jv"
    :class="{ 'toast-enter': isVisible, 'toast-leave': isLeaving }"
    :style="{ '--toast-accent': `var(${typeConfig.accentVar})` }"
    @mouseenter="pauseTimer"
    @mouseleave="resumeTimer"
  >
    <div class="toast-accent-bar" />

    <div class="toast-icon" v-html="typeConfig.icon" />

    <div class="toast-content">
      <div class="toast-title">{{ title }}</div>
      <div v-if="description" class="toast-description">{{ description }}</div>
    </div>

    <button class="toast-close" @click="dismiss" aria-label="Close notification">
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
        <path d="M4 4l6 6M10 4l-6 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
      </svg>
    </button>

    <div class="toast-progress-track">
      <div class="toast-progress-bar" :style="{ width: progressPercent + '%' }" />
    </div>
  </div>
</template>

<style scoped>
.toast-item {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  /* Cap at 360 but shrink on narrow screens so the toast never
     overflows the viewport. The container handles outer gutters. */
  width: 100%;
  max-width: 360px;
  padding: 12px 14px 14px 0;
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--toast-accent);
  border-radius: var(--r-md);
  overflow: hidden;
  pointer-events: auto;
  box-shadow: var(--shadow-md);
  backdrop-filter: blur(16px) saturate(1.3);
  -webkit-backdrop-filter: blur(16px) saturate(1.3);
  color: var(--text);

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
  transition: opacity 0.25s ease-in, transform 0.25s ease-in;
}

/* Decorative accent-coloured glow inside the card. */
.toast-accent-bar {
  position: absolute;
  inset: 0 0 0 0;
  pointer-events: none;
  background:
    linear-gradient(90deg, color-mix(in srgb, var(--toast-accent) 14%, transparent), transparent 40%);
  opacity: 0.55;
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
  color: var(--toast-accent);
  z-index: 1;
}

.toast-content { flex: 1; min-width: 0; z-index: 1; }

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
  color: var(--text-dim);
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
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: 5px;
  color: var(--text-muted);
  cursor: pointer;
  transition: all 0.15s ease;
  z-index: 1;
}

.toast-close:hover {
  background: var(--bg-4);
  color: var(--text);
  border-color: var(--border-bright);
}

.toast-progress-track {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: var(--border);
}

.toast-progress-bar {
  height: 100%;
  background: var(--toast-accent);
  opacity: 0.4;
  border-radius: 0 1px 0 0;
  transition: width 0.1s linear;
}
</style>
