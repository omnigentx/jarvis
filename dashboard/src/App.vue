<script setup>
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from './components/AppLayout.vue'
import ToastContainer from './components/ToastContainer.vue'
import ConfirmModal from './components/ConfirmModal.vue'
import { useConfirmState } from './composables/useConfirm'
import { onSetupRequired } from './api'

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

onMounted(() => {
  // Install the global 503 handler once.  Any apiFetch that hits the setup
  // gate will trigger this — we push the user into the wizard unless they're
  // already inside it.
  onSetupRequired(() => {
    if (router.currentRoute.value.path.startsWith('/setup')) return
    router.push('/setup')
  })
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
</template>
