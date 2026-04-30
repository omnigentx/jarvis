<script setup>
/**
 * Step 3 — External Services.  Non-critical: user can skip entirely.
 *
 * Phase 1f ships with a static list of well-known integrations.  Per service
 * the user expands the card and fills the relevant key/secret pairs; on
 * Continue we call POST /api/setup/services with whatever they configured
 * (may be empty) so the wizard can advance.
 *
 * Google OAuth lives in Phase 3b and appears disabled here — surfaced so
 * the user sees what's coming without being blocked.
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useSetupStore } from '../../stores/setup'
import GoogleOAuthCard from '../../components/GoogleOAuthCard.vue'
import WizardCard from './WizardCard.vue'
import './wizard.css'

const router = useRouter()
const store = useSetupStore()

// Google OAuth uses a dedicated card component (shared with Settings →
// Services) that handles client.json paste, desktop paste-URL, web popup,
// connected state, and the API-enable checklist all inline. Wizards
// shouldn't punt to Settings since /settings is gated mid-setup.
//
// Roborock + GitHub keep the simple field-based mode — those are just
// secret pairs the wizard collects and POSTs at submit time.
const SERVICES = [
  {
    id: 'roborock',
    label: 'Roborock (Vacuum)',
    desc: 'Control Roborock vacuums via the Roborock cloud. Uses your Roborock account email + password (same credentials as the Roborock mobile app).',
    mode: 'fields',
    fields: [
      { key: 'ROBOROCK_USERNAME', label: 'Account email', secret: false },
      { key: 'ROBOROCK_PASSWORD', label: 'Account password', secret: true },
    ],
  },
  {
    id: 'github',
    label: 'GitHub & Git',
    desc: 'Personal access token so dev agents can git clone/push/pull private repos (and drive the GitHub MCP). Name + email are used as the commit identity. Create a token at https://github.com/settings/tokens with the "repo" scope.',
    mode: 'fields',
    // All three fields are required together — enforced by requireAllIfAny below.
    fields: [
      { key: 'personal_access_token', label: 'Personal Access Token', secret: true },
      { key: 'user_name', label: 'Git user name (for commits)', secret: false },
      { key: 'user_email', label: 'Git user email (for commits)', secret: false },
    ],
    requireAllIfAny: true,
  },
]

// { serviceId: { fieldKey: value } }
const values = ref({})
const expanded = ref({})
const submitting = ref(false)

onMounted(() => {
  // Restore any draft the user typed earlier in this browser session. Auto-
  // expand the cards that already have values so they see their entries.
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
// { "<serviceId>:<fieldKey>": true } when the password is revealed as plain
// text.  Keyed by a composite string so two services can independently toggle
// their own secret fields.
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
  // Mirror into the Pinia store immediately so a user who types then hits
  // Back (without Save) still sees their draft on return.
  store.setServiceDraft(values.value)
}
function getField(serviceId, fieldKey) {
  return values.value[serviceId]?.[fieldKey] ?? ''
}

const payload = computed(() => {
  // Only include services where the user filled *something*; skip empty rows
  // to avoid spurious DB writes.
  const out = {}
  for (const svc of SERVICES) {
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

// Services flagged with ``requireAllIfAny`` must have every field filled once
// the user touches any of them — e.g. GitHub needs token + name + email
// together because a half-configured identity leads to commits authored as
// ``root@hostname``. Returns a user-facing message or '' if valid.
const validationError = computed(() => {
  for (const svc of SERVICES) {
    if (svc.mode !== 'fields' || !svc.requireAllIfAny) continue
    const raw = values.value[svc.id] || {}
    const filled = svc.fields.filter((f) => raw[f.key] && String(raw[f.key]).trim())
    if (filled.length === 0) continue // fully blank — card skipped, OK
    if (filled.length === svc.fields.length) continue // fully filled — OK
    const missing = svc.fields
      .filter((f) => !(raw[f.key] && String(raw[f.key]).trim()))
      .map((f) => f.label)
      .join(', ')
    return `${svc.label}: please also fill ${missing}.`
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
    title="External Services"
    subtitle="Connect optional services to extend Jarvis capabilities. These can be configured later from Settings."
    step-label="Step 3 of 5  ·  Optional"
    width="820px"
  >
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
                ? 'Will configure'
                : 'Not configured'
            }}
          </span>
          <button
            v-if="svc.mode === 'fields'"
            type="button"
            class="wizard-btn ghost"
            @click="toggle(svc.id)"
          >
            {{ isExpanded(svc.id) ? 'Close' : 'Configure' }}
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
              :title="isRevealed(svc.id, f.key) ? 'Hide' : 'Reveal'"
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
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="19" y1="12" x2="5" y2="12" />
          <polyline points="12 19 5 12 12 5" />
        </svg>
        Back
      </button>
    </template>
    <template #footer-right>
      <button type="button" class="wizard-btn ghost" :disabled="submitting" @click="onSkip">
        Skip
      </button>
      <button
        type="button"
        class="wizard-btn primary"
        :disabled="submitting || !hasAny || !!validationError"
        @click="onSubmit"
      >
        {{ submitting ? 'Saving...' : hasAny ? 'Save & Continue' : 'Nothing to save' }}
        <svg v-if="!submitting && hasAny" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
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
  border-top: 1px solid #243244;
  margin-top: 4px;
}

.google-feedback {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
}
.google-feedback .hint {
  font-size: 12.5px;
  color: var(--text-nav, #8b8fa3);
  line-height: 1.5;
}
.google-feedback .hint strong { color: var(--text-primary, #f0f2f5); }

.wizard-success {
  background: rgba(34, 197, 94, 0.08);
  border: 1px solid rgba(34, 197, 94, 0.3);
  color: #22c55e;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
}
</style>
