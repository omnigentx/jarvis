<script setup>
/**
 * AudioProgressBar — Thanh tiến trình có seek, dùng chung cho Mini + Full player.
 *
 * Props:
 *  - current: number (seconds)
 *  - total: number (seconds, 0 = unknown)
 *  - buffering: boolean
 *
 * Emits:
 *  - seek: (seconds: number)
 */
import { ref, computed } from 'vue'

const props = defineProps({
  current: { type: Number, default: 0 },
  total: { type: Number, default: 0 },
  buffering: { type: Boolean, default: false },
})

const emit = defineEmits(['seek'])

const isDragging = ref(false)
const barRef = ref(null)

const percent = computed(() => {
  if (!props.total || props.total <= 0) return 0
  return Math.min((props.current / props.total) * 100, 100)
})

function handleClick(e) {
  if (!props.total || props.total <= 0) return
  const rect = barRef.value.getBoundingClientRect()
  const x = Math.max(0, Math.min(e.clientX - rect.left, rect.width))
  const pct = x / rect.width
  emit('seek', pct * props.total)
}

function handleMouseDown(e) {
  isDragging.value = true
  handleClick(e)
  const onMove = (ev) => handleClick(ev)
  const onUp = () => {
    isDragging.value = false
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

// Touch support
function handleTouchStart(e) {
  isDragging.value = true
  const touch = e.touches[0]
  _seekFromTouch(touch)
}
function handleTouchMove(e) {
  if (!isDragging.value) return
  const touch = e.touches[0]
  _seekFromTouch(touch)
}
function handleTouchEnd() {
  isDragging.value = false
}
function _seekFromTouch(touch) {
  if (!props.total || props.total <= 0) return
  const rect = barRef.value.getBoundingClientRect()
  const x = Math.max(0, Math.min(touch.clientX - rect.left, rect.width))
  const pct = x / rect.width
  emit('seek', pct * props.total)
}
</script>

<template>
  <div
    ref="barRef"
    class="progress-bar"
    :class="{ 'progress-bar--active': isDragging, 'progress-bar--buffering': buffering }"
    @mousedown="handleMouseDown"
    @touchstart.passive="handleTouchStart"
    @touchmove.passive="handleTouchMove"
    @touchend="handleTouchEnd"
  >
    <div class="progress-bar__track">
      <!-- Buffering indicator -->
      <div v-if="buffering" class="progress-bar__buffer"></div>
      <!-- Fill -->
      <div class="progress-bar__fill" :style="{ width: percent + '%' }">
        <div class="progress-bar__thumb"></div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.progress-bar {
  position: relative;
  height: 20px;
  display: flex;
  align-items: center;
  cursor: pointer;
  touch-action: none;
}

.progress-bar__track {
  width: 100%;
  height: 4px;
  background: rgba(255,255,255,0.08);
  border-radius: 2px;
  overflow: visible;
  position: relative;
  transition: height 0.15s ease;
}
.progress-bar:hover .progress-bar__track,
.progress-bar--active .progress-bar__track {
  height: 6px;
}

/* Buffer animation */
.progress-bar__buffer {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    90deg,
    transparent 0%,
    rgba(59,130,246,0.2) 50%,
    transparent 100%
  );
  animation: buffer-slide 1.5s ease-in-out infinite;
  border-radius: 2px;
}
@keyframes buffer-slide {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(200%); }
}

/* Fill */
.progress-bar__fill {
  height: 100%;
  background: var(--accent-blue, #3b82f6);
  border-radius: 2px;
  position: relative;
  transition: width 0.1s linear;
}

/* Thumb */
.progress-bar__thumb {
  position: absolute;
  right: -6px;
  top: 50%;
  transform: translateY(-50%);
  width: 12px;
  height: 12px;
  background: var(--accent-blue, #3b82f6);
  border-radius: 50%;
  box-shadow: 0 0 4px rgba(59,130,246,0.4);
  opacity: 0;
  transition: opacity 0.15s ease;
}
.progress-bar:hover .progress-bar__thumb,
.progress-bar--active .progress-bar__thumb {
  opacity: 1;
}
</style>
