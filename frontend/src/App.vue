<script setup>
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from './components/AppLayout.vue'
import ToastContainer from './components/ToastContainer.vue'
import ConfirmModal from './components/ConfirmModal.vue'
import AuthGate from './components/AuthGate.vue'
import { useConfirmState } from './composables/useConfirm'
import { onSetupRequired, onUnauthorized } from './api'
import { useAuthStore, STATUS as AUTH_STATUS } from './stores/auth'
import { on as onAuthEvent, EVENTS as AUTH_EVENTS } from './auth/bus.js'

// Single globally-mounted confirmation dialog driven by useConfirm().  Views
// call ``await confirm({...})`` instead of ``window.confirm(...)`` so every
// prompt shares the themed look + Esc/overlay behaviour.
const { state: confirmState, onConfirm, onCancel } = useConfirmState()

const route = useRoute()
const router = useRouter()

// Routes under /setup render in a standalone "bare" layout — no sidebar, no
// toast container — because the user has no configured backend yet and the
// normal chrome would show empty/erroring panels.
const useBareLayout = computed(() => route.meta?.layout === 'bare')

const auth = useAuthStore()

onMounted(async () => {
  // Install the global 503 handler once.  Any apiFetch that hits the setup
  // gate will trigger this — we push the user into the wizard unless they're
  // already inside it.
  onSetupRequired(() => {
    if (router.currentRoute.value.path.startsWith('/setup')) return
    router.push('/setup')
  })

  // 401s from any apiFetch go through the auth store's soft-fail path
  // (re-probes once before locking the UI; see stores/auth.js#on401).
  onUnauthorized((reason) => {
    auth.on401(reason)
  })

  // Recovering from a LOCKOUT (AuthGate modal login — passkey or API key)
  // reloads the page. Views mount behind the gate overlay (it is not a
  // route guard), so their onMounted fetches already 401'd; only SSE
  // composables and ChatView replay on RESTORED, every other view stays
  // broken until a refresh. Rather than wiring a RESTORED listener into
  // 20+ views (and every future one), replay everything: at lockout there
  // is no in-page state worth preserving — it is all 401-poisoned.
  // Boot probe (from === 'unknown') and challenged-recovery must NOT
  // reload — those paths have healthy in-page state.
  onAuthEvent(AUTH_EVENTS.RESTORED, ({ from } = {}) => {
    if (from === AUTH_STATUS.UNAUTHENTICATED) {
      window.location.reload()
    }
  })

  // Boot probe: ask the backend whether the existing cookie is still good.
  // Skip on the bare /setup pages — the user has no cookie there yet and
  // the AuthGate would cover the wizard otherwise.
  if (route.meta?.layout !== 'bare') {
    await auth.init()
  }
})
</script>

<template>
  <template v-if="useBareLayout">
    <router-view />
  </template>
  <template v-else>
    <ToastContainer />
    <AppLayout />
  </template>
  <ConfirmModal
    :visible="confirmState.visible"
    :title="confirmState.title"
    :message="confirmState.message"
    :confirm-text="confirmState.confirmText"
    :cancel-text="confirmState.cancelText"
    :variant="confirmState.variant"
    @confirm="onConfirm"
    @cancel="onCancel"
  />
  <!-- Mounted at root so it covers both bare /setup and the main app
       layouts. Visibility is driven by the auth store. -->
  <AuthGate />
</template>
