<script setup>
import { TransitionGroup } from 'vue'
import AppToast from './AppToast.vue'
import { useToastState } from '../composables/useToast'

const { toasts, dismissToast } = useToastState()
</script>

<template>
  <Teleport to="body">
    <div class="toast-container" aria-live="polite" aria-atomic="false">
      <TransitionGroup name="toast-stack">
        <AppToast
          v-for="t in toasts"
          :key="t.id"
          :id="t.id"
          :type="t.type"
          :title="t.title"
          :description="t.description"
          :duration="t.duration"
          @dismiss="dismissToast"
        />
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.toast-container {
  position: fixed;
  top: max(16px, var(--safe-top));
  right: 16px;
  z-index: 99999;
  display: flex;
  flex-direction: column;
  gap: 8px;
  pointer-events: none;
  font-family: var(--font-body);
}

@media (max-width: 480px) {
  /* On phones let toasts span the available width minus 8px gutters —
     fixed 360px toasts overflowed iPhone SE / Mini's 360-375px screen. */
  .toast-container {
    left: 8px;
    right: 8px;
  }
}

/* TransitionGroup handles list add/remove — the actual animations
   are driven by the AppToast component's own CSS so we keep these minimal */
.toast-stack-move {
  transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
}
</style>
