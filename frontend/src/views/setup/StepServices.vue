<script setup>
/**
 * Step 3 — External Services.  Non-critical: user can skip entirely.
 *
 * - Project Repository (jarvis_repo): rendered as a dedicated, always-open
 *   panel above everything else.  This URL is what powers Jarvis evolving
 *   itself — agile-team agents (Dev, DSO, ...) inject it into their system
 *   prompt and use it as the target for clone/push/issue-filing.  Without
 *   it, "work on jarvis" prompts have nowhere to land.  Submitted in the
 *   same payload as the other services on Continue.
 * - Google OAuth (Gmail + Calendar): handled by the shared GoogleOAuthCard
 *   component (same UX as Settings → Services).  Wizards can't punt to
 *   Settings since /settings is gated mid-setup.
 * - Other services (Roborock, GitHub): collected as field values then sent
 *   in a single POST /api/setup/services payload on Continue.
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useSetupStore } from '../../stores/setup'
import { useLang } from '../../composables/useLang'
import GoogleOAuthCard from '../../components/GoogleOAuthCard.vue'
import WizardCard from './WizardCard.vue'
import './wizard.css'

const { t } = useLang()
const router = useRouter()
const store = useSetupStore()

// Project Repository — pulled out of SERVICES because it is the
// pivot for Jarvis self-improvement. Rendered as its own always-open
// panel above the optional integrations.
const JARVIS_REPO_ID = 'jarvis_repo'
const JARVIS_REPO_URL_KEY = 'JARVIS_REPO_URL'
// User-facing label/desc/field-label resolved via t() so they follow the
// active locale; ids/keys/flags stay plain. Computed so labels re-render on
// language toggle.
const SERVICES = computed(() => [
  {
    id: 'roborock',
    label: t('setup.services.roborockLabel'),
    desc: t('setup.services.roborockDesc'),
    mode: 'fields',
    fields: [
      { key: 'ROBOROCK_USERNAME', label: t('setup.services.roborockUsername'), secret: false },
      { key: 'ROBOROCK_PASSWORD', label: t('setup.services.roborockPassword'), secret: true },
    ],
  },
  {
    // Stored in voice.secrets.cloudflare_turn.* (same slots Settings → Voice
    // manages) — routes/setup.py maps it there so both surfaces share one
    // source of truth. See VOICE_SERVICES in voice_engine_registry.py.
    id: 'cloudflare_turn',
    label: t('setup.services.turnLabel'),
    desc: t('setup.services.turnDesc'),
    mode: 'fields',
    fields: [
      { key: 'key_id', label: t('setup.services.turnTokenId'), secret: false },
      { key: 'api_token', label: t('setup.services.turnApiToken'), secret: true },
    ],
    requireAllIfAny: true,
  },
  {
    id: 'github',
    label: t('setup.services.githubLabel'),
    desc: t('setup.services.githubDesc'),
    mode: 'fields',
    fields: [
      { key: 'personal_access_token', label: t('setup.services.githubPat'), secret: true },
      { key: 'user_name', label: t('setup.services.githubUserName'), secret: false },
      { key: 'user_email', label: t('setup.services.githubUserEmail'), secret: false },
    ],
    requireAllIfAny: true,
  },
])

const values = ref({})
const expanded = ref({})
const submitting = ref(false)

onMounted(() => {
  const draft = store.serviceDraft || {}
  if (Object.keys(draft).length > 0) {
    values.value = JSON.parse(JSON.stringify(draft))
    const toExpand = {}
    for (const svcId of Object.keys(draft)) {
      if (draft[svcId] && Object.values(draft[svcId]).some(Boolean)) {
        toExpand[svcId] = true
      }
    }
    expanded.value = toExpand
  }
})
const revealed = ref({})

function revealKey(serviceId, fieldKey) { return `${serviceId}:${fieldKey}` }
function isRevealed(serviceId, fieldKey) { return !!revealed.value[revealKey(serviceId, fieldKey)] }
function toggleReveal(serviceId, fieldKey) {
  const k = revealKey(serviceId, fieldKey)
  revealed.value = { ...revealed.value, [k]: !revealed.value[k] }
}

function toggle(id) {
  expanded.value = { ...expanded.value, [id]: !expanded.value[id] }
}
function isExpanded(id) { return !!expanded.value[id] }
function setField(serviceId, fieldKey, val) {
  const current = { ...(values.value[serviceId] || {}) }
  current[fieldKey] = val
  values.value = { ...values.value, [serviceId]: current }
  store.setServiceDraft(values.value)
}
function getField(serviceId, fieldKey) {
  return values.value[serviceId]?.[fieldKey] ?? ''
}

const payload = computed(() => {
  const out = {}
  const repoUrl = (values.value[JARVIS_REPO_ID]?.[JARVIS_REPO_URL_KEY] || '').trim()
  if (repoUrl) {
    out[JARVIS_REPO_ID] = { [JARVIS_REPO_URL_KEY]: repoUrl }
  }
  for (const svc of SERVICES.value) {
    if (svc.mode !== 'fields') continue
    const raw = values.value[svc.id] || {}
    const filled = Object.fromEntries(
      Object.entries(raw).filter(([, v]) => v && String(v).trim()),
    )
    if (Object.keys(filled).length > 0) {
      out[svc.id] = filled
    }
  }
  return out
})

const hasAny = computed(() => Object.keys(payload.value).length > 0)

const validationError = computed(() => {
  for (const svc of SERVICES.value) {
    if (svc.mode !== 'fields' || !svc.requireAllIfAny) continue
    const raw = values.value[svc.id] || {}
    const filled = svc.fields.filter((f) => raw[f.key] && String(raw[f.key]).trim())
    if (filled.length === 0) continue
    if (filled.length === svc.fields.length) continue
    const missing = svc.fields
      .filter((f) => !(raw[f.key] && String(raw[f.key]).trim()))
      .map((f) => f.label)
      .join(', ')
    return t('setup.services.validationFill', { service: svc.label, missing })
  }
  return ''
})

async function onSubmit() {
  if (validationError.value) return
  submitting.value = true
  try {
    await store.submitServices({ services: payload.value })
    router.push({ name: 'SetupYaml' })
  } catch (_) {
    // surfaces via store.lastSubmitError
  } finally {
    submitting.value = false
  }
}

async function onSkip() {
  submitting.value = true
  try {
    await store.skipStep('services')
    router.push({ name: 'SetupYaml' })
  } catch (_) {} finally {
    submitting.value = false
  }
}

function onBack() { router.push({ name: 'SetupLLM' }) }
</script>

<template>
  <WizardCard
    :title="t('setup.services.title')"
    :subtitle="t('setup.services.subtitle')"
    :step-label="t('setup.services.stepLabel')"
    width="820px"
  >
    <!-- Project Repository — promoted, always open -->
    <div class="wizard-service-card jarvis-repo-card" data-testid="wizard-service-jarvis-repo">
      <div class="row">
        <div style="flex: 1;">
          <div class="repo-title-row">
            <h3>{{ t('setup.services.repoTitle') }}</h3>
            <span class="recommended-badge" :title="t('setup.services.repoBadgeTitle')">
              {{ t('setup.services.recommended') }}
            </span>
          </div>
          <div class="desc">
            <strong>{{ t('setup.services.repoDescStrong') }}</strong> {{ t('setup.services.repoDescBody1') }}
            <em>{{ t('setup.services.repoDescQuote') }}</em>{{ t('setup.services.repoDescBody2') }}
          </div>
        </div>
        <div style="display: flex; align-items: center; gap: 10px;">
          <span
            class="badge"
            :class="{ connected: !!(values[JARVIS_REPO_ID]?.[JARVIS_REPO_URL_KEY] || '').trim() }"
          >
            {{
              (values[JARVIS_REPO_ID]?.[JARVIS_REPO_URL_KEY] || '').trim()
                ? t('setup.services.willConfigure')
                : t('setup.services.notConfigured')
            }}
          </span>
        </div>
      </div>

      <div class="fields">
        <div class="wizard-field">
          <label :for="`${JARVIS_REPO_ID}-${JARVIS_REPO_URL_KEY}`">
            {{ t('setup.services.repoUrlLabel') }}
          </label>
          <input
            :id="`${JARVIS_REPO_ID}-${JARVIS_REPO_URL_KEY}`"
            class="wizard-input mono"
            type="text"
            autocomplete="off"
            placeholder="https://github.com/your-user/jarvis.git"
            :value="getField(JARVIS_REPO_ID, JARVIS_REPO_URL_KEY)"
            @input="setField(JARVIS_REPO_ID, JARVIS_REPO_URL_KEY, $event.target.value)"
          />
        </div>
      </div>
    </div>

    <!-- Google OAuth — full credential paste + consent flow lives in the
         shared component (same UX as Settings → Services). -->
    <div class="wizard-service-card" data-testid="wizard-service-google">
      <GoogleOAuthCard />
    </div>

    <div
      v-for="svc in SERVICES"
      :key="svc.id"
      class="wizard-service-card"
      :data-testid="`wizard-service-${svc.id}`"
    >
      <div class="row">
        <div style="flex: 1;">
          <h3>{{ svc.label }}</h3>
          <div class="desc">{{ svc.desc }}</div>
        </div>
        <div style="display: flex; align-items: center; gap: 10px;">
          <span
            class="badge"
            :class="{ connected: values[svc.id] && Object.values(values[svc.id]).some(Boolean) }"
          >
            {{
              values[svc.id] && Object.values(values[svc.id]).some(Boolean)
                ? t('setup.services.willConfigure')
                : t('setup.services.notConfigured')
            }}
          </span>
          <button
            v-if="svc.mode === 'fields'"
            type="button"
            class="wizard-btn ghost"
            @click="toggle(svc.id)"
          >
            {{ isExpanded(svc.id) ? t('common.close') : t('setup.services.configure') }}
          </button>
        </div>
      </div>

      <div v-if="svc.mode === 'fields' && isExpanded(svc.id)" class="fields">
        <div
          v-for="f in svc.fields"
          :key="f.key"
          class="wizard-field"
        >
          <label :for="`${svc.id}-${f.key}`">{{ f.label }}</label>
          <div v-if="f.secret" class="wizard-input-group">
            <input
              :id="`${svc.id}-${f.key}`"
              class="wizard-input"
              :type="isRevealed(svc.id, f.key) ? 'text' : 'password'"
              autocomplete="off"
              :value="getField(svc.id, f.key)"
              @input="setField(svc.id, f.key, $event.target.value)"
            />
            <button
              type="button"
              class="icon-btn"
              :title="isRevealed(svc.id, f.key) ? t('setup.services.hide') : t('setup.services.reveal')"
              @click="toggleReveal(svc.id, f.key)"
            >
              <svg v-if="isRevealed(svc.id, f.key)" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                <line x1="1" y1="1" x2="23" y2="23" />
              </svg>
              <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                <circle cx="12" cy="12" r="3" />
              </svg>
            </button>
          </div>
          <input
            v-else
            :id="`${svc.id}-${f.key}`"
            class="wizard-input"
            type="text"
            autocomplete="off"
            :value="getField(svc.id, f.key)"
            @input="setField(svc.id, f.key, $event.target.value)"
          />
        </div>
      </div>
    </div>

    <div v-if="validationError" class="wizard-error">
      {{ validationError }}
    </div>
    <div v-else-if="store.lastSubmitError" class="wizard-error">
      {{ store.lastSubmitError }}
    </div>

    <template #footer-left>
      <button type="button" class="wizard-btn ghost" @click="onBack">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="19" y1="12" x2="5" y2="12" />
          <polyline points="12 19 5 12 12 5" />
        </svg>
        {{ t('common.back') }}
      </button>
    </template>
    <template #footer-right>
      <button type="button" class="wizard-btn ghost" :disabled="submitting" @click="onSkip">
        {{ t('common.skip') }}
      </button>
      <button
        type="button"
        class="wizard-btn primary"
        :disabled="submitting || !hasAny || !!validationError"
        @click="onSubmit"
      >
        {{ submitting ? t('common.saving') : hasAny ? t('setup.services.saveContinue') : t('setup.services.nothingToSave') }}
        <svg v-if="!submitting && hasAny" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="5" y1="12" x2="19" y2="12" />
          <polyline points="12 5 19 12 12 19" />
        </svg>
      </button>
    </template>
  </WizardCard>
</template>

<style scoped>
.fields {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  margin-top: 4px;
}

.wizard-input.mono {
  font-family: var(--font-mono);
  font-size: 12.5px;
}

/* Promoted "Project Repository" card — visually distinct so users can't miss it. */
.jarvis-repo-card {
  border-color: var(--primary);
  background: var(--primary-bg);
  box-shadow: 0 0 0 1px var(--primary-bg-strong) inset;
}
.repo-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.recommended-badge {
  display: inline-block;
  padding: 2px 9px;
  border-radius: 999px;
  font-family: var(--font-mono);
  font-size: 9.5px;
  font-weight: 600;
  letter-spacing: 0.10em;
  text-transform: uppercase;
  background: var(--primary-bg-strong);
  color: var(--primary-hover);
  border: 1px solid var(--primary);
}
.jarvis-repo-card .desc strong {
  color: var(--text);
}
.jarvis-repo-card .desc em {
  font-style: italic;
  color: var(--text);
}
</style>
