import { ref, readonly } from 'vue'

/**
 * Global toast notification manager.
 * Drop-in replacement for vue-sonner's `toast` API.
 *
 * Usage:
 *   import { useToast } from '@/composables/useToast'
 *   const toast = useToast()
 *   toast.success('Done!', { description: 'All saved.' })
 *   toast.error('Failed', { description: err.message, duration: 6000 })
 */

const toasts = ref([])
let nextId = 1

function addToast(type, title, options = {}) {
  const id = nextId++
  const toast = {
    id,
    type,
    title,
    description: options.description || '',
    duration: options.duration || 4000,
  }
  toasts.value = [...toasts.value, toast]
  return id
}

function dismissToast(id) {
  toasts.value = toasts.value.filter(t => t.id !== id)
}

// Public API — matches vue-sonner's toast.success() / toast.error() / etc.
const toast = {
  success: (title, opts) => addToast('success', title, opts),
  error: (title, opts) => addToast('error', title, opts),
  warning: (title, opts) => addToast('warning', title, opts),
  info: (title, opts) => addToast('info', title, opts),
  message: (title, opts) => addToast('info', title, opts),
}

export function useToast() {
  return toast
}

// For the container component
export function useToastState() {
  return {
    toasts: readonly(toasts),
    dismissToast,
  }
}
