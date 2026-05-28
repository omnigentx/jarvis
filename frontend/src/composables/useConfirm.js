import { ref, readonly } from 'vue'

/**
 * Global confirmation-modal manager — Promise-based replacement for the
 * browser's native ``window.confirm``.  A single <ConfirmModal> is mounted
 * in App.vue and driven by this module's shared state, so any component
 * can surface a themed dialog without importing or placing its own modal.
 *
 * Usage:
 *   import { useConfirm } from '@/composables/useConfirm'
 *   const { confirm } = useConfirm()
 *
 *   if (!(await confirm({
 *     title: 'Disconnect Google',
 *     message: 'Gmail and Calendar tools will stop working until you reconnect.',
 *     confirmText: 'Disconnect',
 *     variant: 'danger',
 *   }))) return
 *
 * Resolves ``true`` when the user clicks confirm, ``false`` when they
 * cancel (button, overlay click, or Esc).  Never rejects.
 */

const DEFAULTS = Object.freeze({
  visible: false,
  title: 'Confirm',
  message: '',
  confirmText: 'Confirm',
  cancelText: 'Cancel',
  variant: 'danger',
})

const state = ref({ ...DEFAULTS })
let resolver = null

function resolve(result) {
  const r = resolver
  resolver = null
  state.value = { ...state.value, visible: false }
  if (r) r(result)
}

function confirm(opts = {}) {
  // Only one dialog can be open at a time — if a caller fires a second
  // confirm while the first is still pending, resolve the first as
  // cancelled so we don't leak the old resolver.
  if (resolver) resolve(false)
  state.value = { ...DEFAULTS, ...opts, visible: true }
  return new Promise((r) => {
    resolver = r
  })
}

export function useConfirm() {
  return { confirm }
}

// For the modal host component (App.vue).
export function useConfirmState() {
  return {
    state: readonly(state),
    onConfirm: () => resolve(true),
    onCancel: () => resolve(false),
  }
}
