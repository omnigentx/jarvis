<script setup>
/**
 * Step 1 — Master API Key.
 *
 * The user either types their own key or hits "Generate".  In both cases
 * the key is sent to POST /api/setup/auth; on success we persist it in
 * localStorage (via the store) so the remaining wizard calls authenticate.
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useSetupStore, generateApiKey } from '../../stores/setup'
import { useLang } from '../../composables/useLang'
import { apiFetch } from '../../api'
import WizardCard from './WizardCard.vue'
import './wizard.css'

const { t } = useLang()
const router = useRouter()
const store = useSetupStore()

const apiKey = ref('')
const confirm = ref('')
const reveal = ref(false)
const submitting = ref(false)
const copiedHint = ref(false)
// True when the backend already has a master key configured (e.g. from .env
// or a previous aborted setup). In that case we pivot from "pick a key" to
// "confirm the existing one or generate a new one via Settings".
const keyAlreadyConfigured = ref(false)

const MIN_LEN = 16

const errors = computed(() => {
  const errs = []
  const trimmed = apiKey.value.trim()
  const confirmTrimmed = confirm.value.trim()
  if (trimmed && trimmed.length < MIN_LEN) {
    errs.push(t('setup.auth.errMinLen', { n: MIN_LEN }))
  }
  if (!keyAlreadyConfigured.value && trimmed && confirmTrimmed && trimmed !== confirmTrimmed) {
    errs.push(t('setup.auth.errMismatch'))
  }
  return errs
})

const canSubmit = computed(() => {
  if (submitting.value) return false
  const trimmed = apiKey.value.trim()
  if (!trimmed) return false
  if (trimmed.length < MIN_LEN) return false
  if (!keyAlreadyConfigured.value && trimmed !== confirm.value.trim()) return false
  return true
})

function onGenerate() {
  const key = generateApiKey()
  apiKey.value = key
  confirm.value = key
  reveal.value = true
}

async function onCopy() {
  try {
    await navigator.clipboard.writeText(apiKey.value)
    copiedHint.value = true
    setTimeout(() => (copiedHint.value = false), 1500)
  } catch (_) {
    // clipboard may be blocked (insecure context); user can copy manually
  }
}

async function onSubmit() {
  if (!canSubmit.value) return
  submitting.value = true
  try {
    await store.submitAuth({ apiKey: apiKey.value.trim() })
    router.push({ name: 'SetupLLM' })
  } catch (_) {
    // Error message surfaces via store.lastSubmitError
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  try {
    const probe = await apiFetch('/api/setup/auth/probe', {
      skipSetupRedirect: true,
    })
    keyAlreadyConfigured.value = Boolean(probe?.configured)
  } catch (_) {
    // Non-fatal — we'll show the normal "pick a key" form.
  }
})
</script>

<template>
  <WizardCard
    :title="keyAlreadyConfigured ? t('setup.auth.titleConfirm') : t('setup.auth.titleWelcome')"
    :subtitle="keyAlreadyConfigured
      ? t('setup.auth.subtitleConfirm')
      : t('setup.auth.subtitleWelcome')"
    :step-label="t('setup.auth.stepLabel')"
  >
    <div v-if="keyAlreadyConfigured" class="wizard-callout warn">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--warning)" stroke-width="2" style="flex-shrink:0; margin-top: 2px;">
        <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
        <line x1="12" y1="9" x2="12" y2="13" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
      <div>
        <strong>{{ t('setup.auth.existingTitle') }}</strong>
        {{ t('setup.auth.existingBody1') }} <code>.env</code> {{ t('setup.auth.existingBody2') }}
        {{ t('setup.auth.existingBody3') }} <code>.env</code> {{ t('setup.auth.existingBody4') }}
      </div>
    </div>

    <div class="wizard-field">
      <label for="new-key">{{ keyAlreadyConfigured ? t('setup.auth.labelExisting') : t('setup.auth.labelNew') }}</label>
      <div class="wizard-input-group">
        <input
          id="new-key"
          class="wizard-input"
          :type="reveal ? 'text' : 'password'"
          autocomplete="new-password"
          :placeholder="keyAlreadyConfigured
            ? t('setup.auth.placeholderExisting')
            : t('setup.auth.placeholderNew', { n: MIN_LEN })"
          v-model="apiKey"
          @keyup.enter="onSubmit"
        />
        <button
          type="button"
          class="icon-btn"
          :title="reveal ? t('setup.auth.hide') : t('setup.auth.reveal')"
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

    <div v-if="!keyAlreadyConfigured" class="wizard-field">
      <label for="confirm-key">{{ t('setup.auth.confirmLabel') }}</label>
      <input
        id="confirm-key"
        class="wizard-input"
        :type="reveal ? 'text' : 'password'"
        autocomplete="new-password"
        :placeholder="t('setup.auth.confirmPlaceholder')"
        v-model="confirm"
        @keyup.enter="onSubmit"
      />
    </div>

    <div class="actions-row">
      <button v-if="!keyAlreadyConfigured" type="button" class="wizard-btn ghost" @click="onGenerate">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M21 2v6h-6" />
          <path d="M3 12a9 9 0 0 1 15-6.7L21 8" />
          <path d="M3 22v-6h6" />
          <path d="M21 12a9 9 0 0 1-15 6.7L3 16" />
        </svg>
        {{ t('setup.auth.generate') }}
      </button>
      <button
        type="button"
        class="wizard-btn ghost"
        :disabled="!apiKey"
        @click="onCopy"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="9" y="9" width="13" height="13" rx="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
        {{ copiedHint ? t('setup.auth.copied') : t('common.copy') }}
      </button>
    </div>

    <div class="wizard-callout">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--primary-hover)" stroke-width="2" style="flex-shrink:0; margin-top: 2px;">
        <circle cx="12" cy="12" r="10" />
        <line x1="12" y1="8" x2="12" y2="12" />
        <line x1="12" y1="16" x2="12.01" y2="16" />
      </svg>
      <div>
        <strong>{{ t('setup.auth.saveTitle') }}</strong> {{ t('setup.auth.saveBody') }}
      </div>
    </div>

    <div v-if="errors.length" class="wizard-error">
      <div v-for="e in errors" :key="e">{{ e }}</div>
    </div>
    <div v-if="store.lastSubmitError" class="wizard-error">
      {{ store.lastSubmitError }}
    </div>

    <template #footer-left>
      <!-- No back on step 1 -->
      <span />
    </template>
    <template #footer-right>
      <button
        type="button"
        class="wizard-btn primary"
        :disabled="!canSubmit"
        @click="onSubmit"
      >
        {{ submitting ? t('common.saving') : t('common.continue') }}
        <svg v-if="!submitting" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="5" y1="12" x2="19" y2="12" />
          <polyline points="12 5 19 12 12 19" />
        </svg>
      </button>
    </template>
  </WizardCard>
</template>

<style scoped>
.actions-row {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
</style>
