<script setup>
/**
 * BottomSheet — mobile-first slide-up sheet anchored to the viewport
 * bottom. Used for attach menus, confirmations, or any content that
 * a dropdown would otherwise show on desktop. On `>= 768px` callers
 * typically render a dropdown/popover instead — this component is
 * intentionally not "responsive": render it only when you actually
 * want sheet semantics.
 *
 * Pattern reference: Mobile.html · `MobileChatAttachSheet`.
 *
 * Props:
 *   open       — boolean v-model. Parent owns the open state.
 *   title      — optional eyebrow shown above the body (mono uppercase).
 *   dismissible (default true) — backdrop click + drag-down close.
 *
 * Emits: `update:open` so parents can use `v-model:open`.
 */
import { watch, onBeforeUnmount } from 'vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: '' },
  dismissible: { type: Boolean, default: true },
})
const emit = defineEmits(['update:open', 'close'])

function close() {
  if (!props.dismissible) return
  emit('update:open', false)
  emit('close')
}

// Lock the page beneath while the sheet is open so the user can't
// scroll the underlying view through the backdrop.
watch(() => props.open, (open) => {
  document.body.classList.toggle('no-scroll', open)
})

onBeforeUnmount(() => {
  document.body.classList.remove('no-scroll')
})
</script>

<template>
  <Teleport to="body">
    <Transition name="sheet">
      <div v-if="open" class="sheet-overlay jv" @click.self="close" role="dialog" aria-modal="true">
        <div class="sheet" role="document">
          <button v-if="dismissible" class="sheet__grab" @click="close" aria-label="Close">
            <span class="sheet__grab-bar" />
          </button>
          <div v-if="title" class="sheet__title mono-label">{{ title }}</div>
          <div class="sheet__body">
            <slot />
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.sheet-overlay {
  position: fixed;
  inset: 0;
  z-index: 9500;
  background: rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  display: flex;
  align-items: flex-end;
  justify-content: center;
}

.sheet {
  width: 100%;
  max-width: 640px;
  background: var(--bg-1);
  border-top-left-radius: 22px;
  border-top-right-radius: 22px;
  border: 1px solid var(--border-strong);
  border-bottom: none;
  /* Clear the iOS home indicator. Without env(), 24px is a generous
     baseline on non-iOS so buttons in the sheet don't sit at the very
     edge of the screen. */
  padding: 8px 14px max(24px, var(--safe-bottom));
  box-shadow: 0 -8px 32px rgba(0, 0, 0, 0.4);
  max-height: 85dvh;
  overflow-y: auto;
}

.sheet__grab {
  display: block;
  width: 100%;
  padding: 4px 0 8px;
  background: transparent;
  border: 0;
  cursor: pointer;
}
.sheet__grab-bar {
  display: block;
  width: 36px;
  height: 4px;
  margin: 0 auto;
  border-radius: 2px;
  background: var(--border-bright);
}

.sheet__title {
  padding: 6px 4px 10px;
  font-size: 10px;
}

.sheet__body {
  padding-top: 4px;
}

/* Enter/leave: slide up + fade overlay. */
.sheet-enter-active, .sheet-leave-active {
  transition: opacity 0.22s var(--ease-out);
}
.sheet-enter-active .sheet,
.sheet-leave-active .sheet {
  transition: transform 0.28s var(--ease-out);
}
.sheet-enter-from, .sheet-leave-to { opacity: 0; }
.sheet-enter-from .sheet, .sheet-leave-to .sheet { transform: translateY(100%); }
</style>
