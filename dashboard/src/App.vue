<script setup>
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from './components/AppLayout.vue'
import ToastContainer from './components/ToastContainer.vue'
import ConfirmModal from './components/ConfirmModal.vue'
import AuthGate from './components/AuthGate.vue'
import { useConfirmState } from './composables/useConfirm'
import { onSetupRequired, onUnauthorized } from './api'
import { useAuthStore } from './stores/auth'

// Single globally-mounted confirmation dialog driven by useConfirm().  Views
// call ``await confirm({...})`` instead of ``window.confirm(...)`` so every
// prompt shares the themed look + Esc/overlay behaviour.
const { state: confirmState, onConfirm, onCancel } = useConfirmState()

const route = useRoute()
const router = useRouter()

// Routes under /setup render in a standalone "bare" layout — no sidebar, no
// toast container — because the user has no configured backend yet and the
// normal chrome would show empty/erroring panels. Public marketing routes also
// avoid dashboard chrome and auth probes so unauthenticated visitors can read
// the page without seeing AuthGate.
const useBareLayout = computed(() => route.meta?.layout === 'bare')
const usePublicLayout = computed(() => route.meta?.layout === 'public' || route.meta?.public === true)
const useStandaloneLayout = computed(() => useBareLayout.value || usePublicLayout.value)
const shouldShowAuthGate = computed(() => !usePublicLayout.value)

const auth = useAuthStore()

onMounted(async () => {
  // Install the global 503 handler once.  Any apiFetch that hits the setup
  // gate will trigger this — we push the user into the wizard unless they're
  // already inside it.
  onSetupRequired(() => {
    if (router.currentRoute.value.path.startsWith('/setup')) return
    if (router.currentRoute.value.meta?.public === true) return
    router.push('/setup')
  })

  // 401s from any apiFetch go through the auth store's soft-fail path
  // (re-probes once before locking the UI; see stores/auth.js#on401).
  onUnauthorized((reason) => {
    if (router.currentRoute.value.meta?.public === true) return
    auth.on401(reason)
  })

  // Boot probe: ask the backend whether the existing cookie is still good.
  // Skip on bare /setup pages and public marketing routes — the user has no
  // cookie there yet and AuthGate must not cover those pages.
  if (!route.meta?.public && route.meta?.layout !== 'bare') {
    await auth.init()
  }
})
</script>

<template>
  <template v-if="useStandaloneLayout">
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
  <!-- Mounted at root so it covers dashboard and setup layouts. Public
       marketing routes suppress it to preserve unauthenticated access. -->
  <AuthGate v-if="shouldShowAuthGate" />
</template>
