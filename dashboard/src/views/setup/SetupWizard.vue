<script setup>
/**
 * Setup Wizard — parent layout + router outlet for the 5 step screens.
 *
 * Responsibilities:
 *   - Pull fresh setup status on mount so direct-link visitors don't see
 *     stale state from a prior session.
 *   - Render the top progress-stepper (shared across every step).
 *   - Redirect to /setup/<current> if the user somehow lands on a completed
 *     step, so the back button can't escape into an already-done state.
 */
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSetupStore } from '../../stores/setup'
import WizardStepper from './WizardStepper.vue'

const store = useSetupStore()
const route = useRoute()
const router = useRouter()

onMounted(async () => {
  try {
    await store.fetchStatus()
  } catch (err) {
    // Surface only inside the wizard — the outer app need not know.
    console.warn('[setup] failed to load status', err)
  }
  // If the user lands on a step they've already passed (e.g. via direct URL
  // after a reload), forward them to the current pending step.  We don't
  // touch navigation when the wizard is already complete — the Verify step
  // is where we want them to be anyway.
  if (!store.overallComplete && store.currentStep) {
    const wantName = _routeNameFor(store.currentStep)
    const cur = route.name
    if (cur && cur !== wantName && cur !== 'SetupVerify') {
      // Only auto-advance past completed steps; allow Back navigation.
      const currentIdx = _indexOf(_stepFromRoute(cur))
      const wantIdx = _indexOf(store.currentStep)
      if (currentIdx !== -1 && currentIdx < wantIdx) {
        router.replace({ name: wantName })
      }
    }
  }
})

watch(() => store.overallComplete, (done) => {
  if (done && route.name !== 'SetupVerify') {
    router.replace({ name: 'SetupVerify' })
  }
})

function _indexOf(name) {
  return ['auth', 'llm', 'services', 'yaml_config', 'verify'].indexOf(name)
}
function _stepFromRoute(routeName) {
  return {
    SetupAuth: 'auth',
    SetupLLM: 'llm',
    SetupServices: 'services',
    SetupYaml: 'yaml_config',
    SetupVerify: 'verify',
  }[routeName] || null
}

function _routeNameFor(stepName) {
  return {
    auth: 'SetupAuth',
    llm: 'SetupLLM',
    services: 'SetupServices',
    yaml_config: 'SetupYaml',
    verify: 'SetupVerify',
  }[stepName] || 'SetupAuth'
}

const currentStepName = computed(() => {
  const map = {
    SetupAuth: 'auth',
    SetupLLM: 'llm',
    SetupServices: 'services',
    SetupYaml: 'yaml_config',
    SetupVerify: 'verify',
  }
  return map[route.name] || store.currentStep || 'auth'
})
</script>

<template>
  <div class="wizard-shell">
    <div class="glow" aria-hidden="true" />
    <header class="wizard-header">
      <div class="brand">
        <div class="logo">J</div>
        <span>Jarvis Setup</span>
      </div>
      <WizardStepper :steps="store.steps" :current="currentStepName" />
    </header>

    <main class="wizard-main">
      <router-view v-slot="{ Component }">
        <transition name="fade" mode="out-in">
          <component :is="Component" />
        </transition>
      </router-view>
    </main>
  </div>
</template>

<style scoped>
.wizard-shell {
  position: relative;
  min-height: 100vh;
  background: #0b1020;
  color: #e5eef8;
  overflow-x: hidden;
  padding: 40px 24px 64px;
}
.glow {
  position: absolute;
  top: -200px;
  right: -120px;
  width: 400px;
  height: 400px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(91, 140, 255, 0.18) 0%, transparent 70%);
  filter: blur(40px);
  pointer-events: none;
  z-index: 0;
}
.wizard-header {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 24px;
  margin-bottom: 48px;
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 16px;
  font-weight: 600;
  color: #e5eef8;
}
.brand .logo {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: linear-gradient(135deg, #5b8cff, #6366f1);
  display: grid;
  place-items: center;
  color: #fff;
  font-weight: 700;
  font-size: 18px;
  box-shadow: 0 4px 14px rgba(91, 140, 255, 0.35);
}
.wizard-main {
  position: relative;
  z-index: 1;
  display: flex;
  justify-content: center;
}
.fade-enter-active, .fade-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}
.fade-enter-from { opacity: 0; transform: translateY(6px); }
.fade-leave-to { opacity: 0; transform: translateY(-4px); }
</style>
