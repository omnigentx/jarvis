<script setup>
/**
 * Step 5 — Verify & Launch.
 *
 * Reads the final wizard state + a fresh settings snapshot so we can show
 * the user what Jarvis will boot with.  On Launch we POST /api/setup/verify
 * (which flips the backend setup-gate open), then route the user into the
 * dashboard.
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useSetupStore } from '../../stores/setup'
import WizardCard from './WizardCard.vue'
import './wizard.css'

const router = useRouter()
const setupStore = useSetupStore()

const submitting = ref(false)
const acceptWarnings = ref(false)

onMounted(async () => {
  // Rely entirely on /api/setup/status (always reachable) — /api/settings is
  // still 503'd by the setup-gate here, so reading from settingsStore gives
  // false "Missing" rows for values the user already saved.
  try {
    await setupStore.fetchStatus()
  } catch (_) {}
})

const summary = computed(() => {
  const authStep = setupStore.stepByName('auth')
  const llmStep = setupStore.stepByName('llm')
  const servicesStep = setupStore.stepByName('services')
  const yamlStep = setupStore.stepByName('yaml_config')

  const llmData = llmStep?.data || {}
  const hasMasterKey = !!authStep?.completed
  const llmProv = llmData.provider
  const llmModel = llmData.model
  const services = Array.isArray(servicesStep?.data?.configured)
    ? servicesStep.data.configured
    : []
  return {
    hasMasterKey,
    llmApiKeySet: !!llmData.api_key_set,
    llmModel,
    llm: llmProv || llmModel
      ? `${llmProv || '?'}${llmModel ? ` (${llmModel})` : ''}`
      : null,
    services,
    yaml: yamlStep?.completed
      ? 'Accepted defaults'
      : yamlStep?.skipped
        ? 'Skipped'
        : 'Pending',
  }
})

const pageTitle = computed(() =>
  setupStore.overallComplete ? 'Setup Complete!' : 'Ready to Launch',
)
const pageSubtitle = computed(() =>
  setupStore.overallComplete
    ? "Jarvis is configured and ready to go. Click Launch when you're ready."
    : "Here's the final summary. Review and launch when ready.",
)

const missing = computed(() => {
  const m = []
  if (!summary.value.hasMasterKey) m.push('auth.JARVIS_API_KEY')
  if (!summary.value.llmApiKeySet) {
    // Name the exact namespaced slot so the user knows where to look in
    // Settings → LLM Provider (each provider has its own api_key now).
    const prov = setupStore.stepByName('llm')?.data?.provider
    const slot = prov === 'custom' ? 'generic' : (prov || 'anthropic')
    m.push(`llm.${slot}_api_key`)
  }
  if (!summary.value.llmModel) m.push('llm.model')
  return m
})

async function onLaunch() {
  submitting.value = true
  try {
    await setupStore.submitVerify({ accept_warnings: acceptWarnings.value })
    router.push({ path: '/' })
  } catch (_) {
    // surfaces via setupStore.lastSubmitError
  } finally {
    submitting.value = false
  }
}

function onBack() { router.push({ name: 'SetupYaml' }) }
</script>

<template>
  <WizardCard
    :title="pageTitle"
    :subtitle="pageSubtitle"
    step-label="Step 5 of 5  ·  Required"
  >
    <div class="check-badge" aria-hidden="true">
      <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <path d="M20 6 9 17l-5-5" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    </div>

    <div class="wizard-summary">
      <div class="row">
        <span class="label">Master API Key</span>
        <span class="value" :class="{ ok: summary.hasMasterKey }">
          {{ summary.hasMasterKey ? 'Configured ✓' : 'Missing' }}
        </span>
      </div>
      <div class="row">
        <span class="label">LLM Provider</span>
        <span class="value" :class="{ muted: !summary.llm }">
          {{ summary.llm || 'Not configured' }}
        </span>
      </div>
      <div class="row">
        <span class="label">External Services</span>
        <span class="value" :class="{ muted: summary.services.length === 0 }">
          {{ summary.services.length ? summary.services.join(', ') : 'None' }}
        </span>
      </div>
      <div class="row">
        <span class="label">YAML Config</span>
        <span class="value muted">{{ summary.yaml }}</span>
      </div>
    </div>

    <div v-if="missing.length" class="wizard-callout warn">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2" style="flex-shrink:0; margin-top: 2px;">
        <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
        <line x1="12" y1="9" x2="12" y2="13" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
      <div style="flex: 1;">
        <strong>Missing critical config:</strong>
        <ul style="margin: 6px 0 10px 16px; padding: 0;">
          <li v-for="m in missing" :key="m"><code>{{ m }}</code></li>
        </ul>
        <label style="display: flex; gap: 8px; align-items: center; cursor: pointer;">
          <input type="checkbox" v-model="acceptWarnings" />
          <span>Launch anyway (I'll configure later in Settings)</span>
        </label>
      </div>
    </div>

    <div v-if="setupStore.lastSubmitError" class="wizard-error">
      {{ setupStore.lastSubmitError }}
    </div>

    <p class="fineprint">
      You can always change these settings later from the Settings page.
    </p>

    <template #footer-left>
      <button type="button" class="wizard-btn ghost" @click="onBack">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="19" y1="12" x2="5" y2="12" />
          <polyline points="12 19 5 12 12 5" />
        </svg>
        Back
      </button>
    </template>
    <template #footer-right>
      <button
        type="button"
        class="wizard-btn primary"
        :disabled="submitting || (missing.length > 0 && !acceptWarnings)"
        @click="onLaunch"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
          <polygon points="5 3 19 12 5 21 5 3" />
        </svg>
        {{ submitting ? 'Launching...' : 'Launch Jarvis' }}
      </button>
    </template>
  </WizardCard>
</template>

<style scoped>
.check-badge {
  margin: 0 auto 8px;
  width: 72px;
  height: 72px;
  border-radius: 50%;
  background: rgba(34, 197, 94, 0.15);
  color: #22c55e;
  display: grid;
  place-items: center;
  box-shadow: 0 0 0 12px rgba(34, 197, 94, 0.06);
}
.fineprint {
  text-align: center;
  font-size: 13px;
  color: #64748b;
  margin: 0;
}
</style>
