<script setup>
/**
 * Step 5 — Verify & Launch.
 *
 * Reads the final wizard state so we can show the user what Jarvis will boot
 * with as a green checklist.  On Launch we POST /api/setup/verify (which
 * flips the backend setup-gate open), then route the user into the dashboard.
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
  setupStore.overallComplete ? 'Setup complete' : 'Ready to launch',
)
const pageSubtitle = computed(() =>
  setupStore.overallComplete
    ? "Jarvis is configured and ready to go. Click Launch when you're ready."
    : "Here's the final summary. Review and launch when ready.",
)

// Checklist items — each row has its own pass/skip/warn state so the user
// sees exactly what's good vs. what they're launching without.
const checklist = computed(() => [
  {
    label: 'Master API key',
    sub: summary.value.hasMasterKey
      ? 'Configured · stored encrypted'
      : 'Required — please go back to Step 1',
    ok: summary.value.hasMasterKey,
  },
  {
    label: 'LLM provider',
    sub: summary.value.llm
      ? `${summary.value.llm}${summary.value.llmApiKeySet ? ' · key stored' : ' · key missing'}`
      : 'Required — please go back to Step 2',
    ok: !!summary.value.llm && summary.value.llmApiKeySet,
  },
  {
    label: 'External services',
    sub: summary.value.services.length
      ? summary.value.services.join(', ')
      : 'None configured · optional',
    ok: true,
    optional: true,
  },
  {
    label: 'YAML config',
    sub: summary.value.yaml,
    ok: true,
    optional: true,
  },
])

const missing = computed(() => {
  const m = []
  if (!summary.value.hasMasterKey) m.push('auth.JARVIS_API_KEY')
  if (!summary.value.llmApiKeySet) {
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
    step-label="STEP 05 / 05 · VERIFY"
  >
    <ul class="verify-list">
      <li
        v-for="item in checklist"
        :key="item.label"
        :class="{ ok: item.ok, warn: !item.ok }"
      >
        <span class="check-circle" aria-hidden="true">
          <svg v-if="item.ok" width="13" height="13" viewBox="0 0 16 16">
            <path
              d="M3 8.5l3 3L13 4.5"
              fill="none"
              stroke="currentColor"
              stroke-width="2.6"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
          <svg v-else width="13" height="13" viewBox="0 0 16 16">
            <path
              d="M8 4v5M8 12v.01"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
            />
          </svg>
        </span>
        <div class="text">
          <div class="label">
            {{ item.label }}
            <span v-if="item.optional" class="tag">OPTIONAL</span>
          </div>
          <div class="sub">{{ item.sub }}</div>
        </div>
      </li>
    </ul>

    <div v-if="missing.length" class="wizard-callout warn">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--warning)" stroke-width="2" style="flex-shrink:0; margin-top: 2px;">
        <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
        <line x1="12" y1="9" x2="12" y2="13" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
      <div style="flex: 1;">
        <strong>Missing critical config:</strong>
        <ul class="missing-list">
          <li v-for="m in missing" :key="m"><code>{{ m }}</code></li>
        </ul>
        <label class="accept-row">
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
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
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
        <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor">
          <polygon points="5 3 19 12 5 21 5 3" />
        </svg>
        {{ submitting ? 'Launching…' : 'Launch Jarvis' }}
      </button>
    </template>
  </WizardCard>
</template>

<style scoped>
.verify-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.verify-list li {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 12px 14px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
}
.check-circle {
  width: 22px;
  height: 22px;
  flex-shrink: 0;
  border-radius: 50%;
  display: grid;
  place-items: center;
  color: #ffffff;
  margin-top: 1px;
}
.verify-list li.ok .check-circle { background: var(--success); }
.verify-list li.warn .check-circle { background: var(--warning); }
.text { flex: 1; min-width: 0; }
.label {
  font-family: var(--font-display);
  font-size: 14px;
  font-weight: 500;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 8px;
}
.tag {
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 500;
  letter-spacing: 0.10em;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--bg-3);
  color: var(--text-muted);
  border: 1px solid var(--border);
}
.sub {
  font-size: 12px;
  color: var(--text-dim);
  margin-top: 2px;
  line-height: 1.45;
}

.missing-list {
  margin: 6px 0 10px 16px;
  padding: 0;
}
.missing-list code { color: var(--accent); }
.accept-row {
  display: flex;
  gap: 8px;
  align-items: center;
  cursor: pointer;
  font-size: 12.5px;
  color: var(--text-dim);
}

.fineprint {
  text-align: center;
  font-size: 12px;
  color: var(--text-subtle);
  margin: 0;
}
</style>
