<script setup>
/**
 * Step 2 — LLM Provider.
 * Saves provider/model/api_key/base_url to the `llm` category. The backend
 * encrypts api_key at rest via Fernet.
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useSetupStore } from '../../stores/setup'
import WizardCard from './WizardCard.vue'
import './wizard.css'

const router = useRouter()
const setupStore = useSetupStore()

const PRESETS = {
  openai: {
    label: 'OpenAI',
    sub: 'compatible · sk-…',
    defaultModel: 'gpt-4o',
    baseUrl: '',
    keyPrefix: 'sk-…',
    baseUrlPlaceholder: 'https://api.openai.com/v1 (leave blank for default)',
    modelExamples: 'gpt-4o, gpt-4o-mini, o1-mini — or whatever alias your proxy exposes',
    hint: 'Use this for api.openai.com OR any OpenAI-compatible proxy (9router, CLIProxyAPI, LiteLLM — they speak OpenAI wire format). Models are addressed as openai.{model}.',
  },
  anthropic: {
    label: 'Anthropic',
    sub: 'claude-sonnet-4',
    defaultModel: 'claude-sonnet-4-20250514',
    baseUrl: '',
    keyPrefix: 'sk-ant-api03-…',
    baseUrlPlaceholder: 'https://api.anthropic.com (leave blank for default)',
    modelExamples: 'claude-sonnet-4-20250514, claude-3-5-haiku-20241022, claude-opus-4-20250514',
    hint: 'Official Anthropic API. Models will be addressed as anthropic.{model}.',
  },
  custom: {
    label: 'Generic / Local',
    sub: 'ollama · llama.cpp',
    defaultModel: '',
    baseUrl: 'http://localhost:11434/v1',
    keyPrefix: '(often empty for local)',
    baseUrlPlaceholder: 'http://localhost:11434/v1 (Ollama) · http://localhost:1234/v1 (LM Studio)',
    modelExamples: 'llama3.1:8b, qwen2.5-coder:7b, mistral-nemo',
    hint: 'For self-hosted models behind OpenAI-compatible endpoints. Models will be addressed as generic.{model} — remember to update default_model in fastagent.config.yaml.',
  },
}

// UI "custom" → fast-agent "generic" slot; that's the namespace used for
// per-provider storage (`llm.{slot}_api_key`).
const SLOT_BY_UI = { openai: 'openai', anthropic: 'anthropic', custom: 'generic' }

const provider = ref('anthropic')
const model = ref(PRESETS.anthropic.defaultModel)
const apiKey = ref('')
const baseUrl = ref('')
const reveal = ref(false)
const submitting = ref(false)
// Per-provider "has a key already been stored?" map — keyed by slot.
const keysBySlot = ref({ openai: false, anthropic: false, generic: false })

const hasStoredKey = computed(
  () => !!keysBySlot.value[SLOT_BY_UI[provider.value]],
)

onMounted(async () => {
  try {
    await setupStore.fetchStatus()
    const step = setupStore.stepByName('llm')
    const data = step?.data || {}
    if (data.provider && PRESETS[data.provider]) {
      provider.value = data.provider
    }
    if (data.model) model.value = data.model
    if (data.base_url) baseUrl.value = data.base_url
    if (data.keys_by_slot && typeof data.keys_by_slot === 'object') {
      keysBySlot.value = { ...keysBySlot.value, ...data.keys_by_slot }
    } else if (data.api_key_set && data.provider) {
      const slot = SLOT_BY_UI[data.provider]
      if (slot) keysBySlot.value[slot] = true
    }
  } catch (_) {
    // Fine — first-time visit has no prior step data.
  }
})

const preset = computed(() => PRESETS[provider.value] || PRESETS.custom)

function pickProvider(name) {
  provider.value = name
  const p = PRESETS[name]
  apiKey.value = ''
  baseUrl.value = p.baseUrl
  if (!model.value || Object.values(PRESETS).some((x) => x.defaultModel === model.value)) {
    model.value = p.defaultModel
  }
}

const canSubmit = computed(() => {
  if (submitting.value) return false
  if (!provider.value) return false
  if (!model.value.trim()) return false
  if (!apiKey.value.trim() && !hasStoredKey.value) return false
  return true
})

async function onSubmit() {
  if (!canSubmit.value) return
  submitting.value = true
  try {
    const payload = {
      provider: provider.value,
      model: model.value.trim(),
      base_url: baseUrl.value.trim() || null,
    }
    if (apiKey.value.trim()) payload.api_key = apiKey.value.trim()
    await setupStore.submitLLM(payload)
    router.push({ name: 'SetupServices' })
  } catch (_) {
    // surfaces via setupStore.lastSubmitError
  } finally {
    submitting.value = false
  }
}

function onBack() {
  router.push({ name: 'SetupAuth' })
}
</script>

<template>
  <WizardCard
    title="LLM provider configuration"
    subtitle="Configure the AI model provider that powers Jarvis. Use OpenAI, Anthropic, or any OpenAI-compatible API."
    step-label="STEP 02 / 05 · LLM"
  >
    <div class="wizard-choice-grid">
      <button
        v-for="(p, key) in PRESETS"
        :key="key"
        type="button"
        class="wizard-choice"
        :class="{ selected: provider === key }"
        @click="pickProvider(key)"
      >
        <span v-if="provider === key" class="choice-check" aria-hidden="true">
          <svg width="10" height="10" viewBox="0 0 16 16">
            <path
              d="M3 8.5l3 3L13 4.5"
              fill="none"
              stroke="currentColor"
              stroke-width="2.8"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
        </span>
        <span class="choice-title">{{ p.label }}</span>
        <span class="choice-sub">{{ p.sub }}</span>
      </button>
    </div>

    <div v-if="preset.hint" class="wizard-callout">
      <div>{{ preset.hint }}</div>
    </div>

    <div class="wizard-field">
      <label for="llm-key">
        API KEY
        <span v-if="hasStoredKey" class="field-hint">· already stored, leave blank to keep</span>
      </label>
      <div class="wizard-input-group">
        <input
          id="llm-key"
          class="wizard-input"
          :type="reveal ? 'text' : 'password'"
          autocomplete="off"
          :placeholder="hasStoredKey ? '•••••••• (stored — leave blank to keep)' : (preset.keyPrefix || 'Paste provider API key')"
          v-model="apiKey"
        />
        <button
          type="button"
          class="icon-btn"
          :title="reveal ? 'Hide' : 'Reveal'"
          @click="reveal = !reveal"
        >
          <svg v-if="reveal" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
            <line x1="1" y1="1" x2="23" y2="23" />
          </svg>
          <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
            <circle cx="12" cy="12" r="3" />
          </svg>
        </button>
      </div>
    </div>

    <div class="wizard-field">
      <label for="llm-base">BASE URL <span class="field-hint">· optional · override</span></label>
      <input
        id="llm-base"
        class="wizard-input mono"
        type="url"
        :placeholder="preset.baseUrlPlaceholder || 'https://...'"
        v-model="baseUrl"
      />
    </div>

    <div class="wizard-field">
      <label for="llm-model">MODEL <span class="field-hint">· default · streaming</span></label>
      <input
        id="llm-model"
        class="wizard-input mono"
        type="text"
        :placeholder="preset.defaultModel || 'e.g. gpt-4o'"
        v-model="model"
      />
      <div v-if="preset.modelExamples" class="wizard-help">Examples: {{ preset.modelExamples }}</div>
    </div>

    <div v-if="setupStore.lastSubmitError" class="wizard-error">
      {{ setupStore.lastSubmitError }}
    </div>

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
        :disabled="!canSubmit"
        @click="onSubmit"
      >
        {{ submitting ? 'Saving…' : 'Continue' }}
        <svg v-if="!submitting" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="5" y1="12" x2="19" y2="12" />
          <polyline points="12 5 19 12 12 19" />
        </svg>
      </button>
    </template>
  </WizardCard>
</template>

<style scoped>
.field-hint {
  font-family: var(--font-mono);
  font-weight: 400;
  font-size: 10px;
  letter-spacing: 0.10em;
  text-transform: uppercase;
  color: var(--text-subtle);
  margin-left: 4px;
}
.wizard-input.mono {
  font-family: var(--font-mono);
  font-size: 12.5px;
}
</style>
