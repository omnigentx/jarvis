<script setup>
/**
 * AudioProgressBar — seekable progress bar shared between mini + full players.
 * Logic unchanged; restyled to use design tokens.
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
      <div v-if="buffering" class="progress-bar__buffer"></div>
      <div class="progress-bar__fill" :style="{ width: percent + '%' }">
        <div class="progress-bar__thumb"></div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.progress-bar {
  position: relative;
  height: 18px;
  display: flex;
  align-items: center;
  cursor: pointer;
  touch-action: none;
}

.progress-bar__track {
  width: 100%;
  height: 3px;
  background: var(--bg-3);
  border-radius: 999px;
  overflow: visible;
  position: relative;
  transition: height 0.15s var(--ease-out);
}
.progress-bar:hover .progress-bar__track,
.progress-bar--active .progress-bar__track {
  height: 5px;
}

.progress-bar__buffer {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    90deg,
    transparent 0%,
    rgba(34, 211, 238, 0.25) 50%,
    transparent 100%
  );
  animation: buffer-slide 1.5s ease-in-out infinite;
  border-radius: 999px;
}
@keyframes buffer-slide {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(200%); }
}

.progress-bar__fill {
  height: 100%;
  background: var(--accent);
  border-radius: 999px;
  position: relative;
  transition: width 0.1s linear;
}

.progress-bar__thumb {
  position: absolute;
  right: -5px;
  top: 50%;
  transform: translateY(-50%);
  width: 10px;
  height: 10px;
  background: white;
  border: 2px solid var(--accent);
  border-radius: 50%;
  box-shadow: 0 0 6px rgba(34, 211, 238, 0.5);
  opacity: 0;
  transition: opacity 0.15s var(--ease-out);
}
.progress-bar:hover .progress-bar__thumb,
.progress-bar--active .progress-bar__thumb {
  opacity: 1;
}

/* Touch devices: hover doesn't exist so the thumb stayed invisible
   and seeking required guessing where to tap. Always show the thumb,
   bump the hit area + thumb size so a finger can land on it. */
@media (pointer: coarse) {
  .progress-bar { height: 28px; }
  .progress-bar__thumb {
    opacity: 1;
    width: 14px;
    height: 14px;
    right: -7px;
  }
}
</style>
