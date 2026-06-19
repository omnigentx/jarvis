<script setup>
/**
 * Setup Wizard — parent layout + router outlet for the 5 step screens.
 *
 * Visual treatment from DESIGN_HANDOFF.md §7: a browser-window chrome
 * (traffic-light dots, URL pill, LIVE indicator) wraps the entire wizard.
 * Progress strip sits at the top under the chrome; the active step body
 * renders below and the navigation footer is rendered per step via the
 * WizardCard slots.
 *
 * Logic responsibilities (unchanged):
 *   - Pull fresh setup status on mount so direct-link visitors don't see
 *     stale state from a prior session.
 *   - Forward the user to /setup/<current> if they land on an earlier step
 *     they've already passed (Back nav still works since we only nudge
 *     forward).
 *   - Watch overallComplete → redirect to Verify when the backend flips it.
 */
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSetupStore } from '../../stores/setup'
import { useLang } from '../../composables/useLang'
import WizardStepper from './WizardStepper.vue'
import '../../assets/tokens.css'

const { t } = useLang()
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
  <div class="wizard-shell jv">
    <header class="brand-strip">
      <div class="brand">
        <div class="logo">J</div>
        <span>{{ t('setup.wizard.brand') }}</span>
      </div>
    </header>

    <div class="browser-chrome">
      <!-- Browser top: traffic-light dots + URL pill + LIVE indicator -->
      <div class="chrome-bar">
        <span class="dots" aria-hidden="true">
          <span class="dot red" />
          <span class="dot yellow" />
          <span class="dot green" />
        </span>
        <div class="url-pill">
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
            <path d="M7 11V7a5 5 0 0 1 10 0v4" />
          </svg>
          <span>localhost/#/setup</span>
        </div>
        <span class="live">
          <span class="live-dot pulse-dot" />
          <span class="live-label">LIVE</span>
        </span>
      </div>

      <!-- Progress strip -->
      <WizardStepper :steps="store.steps" :current="currentStepName" />

      <!-- Step body -->
      <main class="wizard-main">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </main>
    </div>
  </div>
</template>

<style scoped>
.wizard-shell {
  min-height: 100vh;
  background: var(--bg-0);
  color: var(--text);
  padding: 24px 24px 40px;
  display: flex;
  flex-direction: column;
  gap: 18px;
  font-family: var(--font-body);
}
.brand-strip {
  display: flex;
  justify-content: center;
  align-items: center;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}
.brand .logo {
  width: 30px;
  height: 30px;
  border-radius: var(--r-md);
  background: linear-gradient(135deg, var(--primary-hover), var(--primary));
  display: grid;
  place-items: center;
  color: #fff;
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 15px;
  box-shadow: 0 4px 14px rgba(99, 102, 241, 0.35);
}

/* Browser-window chrome */
.browser-chrome {
  width: 100%;
  max-width: 1080px;
  margin: 0 auto;
  background: var(--bg-1);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xl);
  overflow: hidden;
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.chrome-bar {
  height: 38px;
  padding: 0 14px;
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--bg-2);
  border-bottom: 1px solid var(--border);
}
.dots { display: inline-flex; gap: 6px; }
.dot {
  width: 11px;
  height: 11px;
  border-radius: 50%;
  display: inline-block;
}
.dot.red { background: #FF5F57; }
.dot.yellow { background: #FEBC2E; }
.dot.green { background: #28C840; }
.url-pill {
  flex: 1;
  max-width: 360px;
  margin: 0 auto;
  height: 22px;
  padding: 0 10px;
  background: var(--bg-3);
  border: 1px solid var(--border);
  border-radius: 999px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-muted);
}
.live {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.live-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 8px var(--accent);
}
.live-label {
  font-family: var(--font-mono);
  font-size: 9.5px;
  letter-spacing: 0.14em;
  color: var(--text-muted);
}

.wizard-main {
  padding: 28px 32px;
  display: flex;
  justify-content: center;
}
.fade-enter-active, .fade-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}
.fade-enter-from { opacity: 0; transform: translateY(6px); }
.fade-leave-to { opacity: 0; transform: translateY(-4px); }

@keyframes pulseDot {
  0%, 100% { opacity: 1; transform: scale(1); }
  50%      { opacity: 0.55; transform: scale(1.15); }
}
.pulse-dot { animation: pulseDot 1.4s ease-in-out infinite; }
</style>
