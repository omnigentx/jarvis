<script setup>
/**
 * Settings → Agent Memory.
 *
 * Typed form over GET/PATCH /api/memory/settings (config DB category
 * `memory`). Validation lives on the backend; the PATCH 422 is the authority.
 * Save sends ONLY changed keys. The curator API key is write-only.
 *
 * Styling follows the design tokens (assets/tokens.css) and mirrors the LLM
 * Provider screen: `.field` labelled inputs, `.text-input`/`.pwd-input`, the
 * key eye-toggle + stored/not-set badge, `--primary` toggles and buttons.
 * Copy via i18n (useLang().t) — English base, Vietnamese overlay.
 */
import { computed, onMounted, ref } from 'vue'
import { apiFetch, ApiError } from '../../api'
import { useLang } from '../../composables/useLang.js'

const { t } = useLang()

const loading = ref(true)
const saving = ref(false)
const error = ref('')
const saveSuccess = ref(false)
const showKey = ref(false)

const config = ref(null)
const draft = ref(null)
const apiKeyInput = ref('')   // write-only; never populated from server
const indexStatus = ref(null) // Qdrant reachability + index counts

const MODES = ['economical', 'balanced', 'deep']
const POLICIES = ['manual', 'auto_low_risk']

const NUMBER_FIELDS = [
  { key: 'pinned_token_budget', labelKey: 'settings.memory.pinnedBudget', min: 0, max: 100000, step: 100 },
  { key: 'evidence_token_budget', labelKey: 'settings.memory.evidenceBudget', min: 0, max: 100000, step: 100 },
  { key: 'retention_episodic_days', labelKey: 'settings.memory.retentionEpisodic', min: 1, max: 3650, step: 1 },
  { key: 'retention_retrieval_runs_days', labelKey: 'settings.memory.retentionRuns', min: 1, max: 365, step: 1 },
  { key: 'recall_min_similarity', labelKey: 'settings.memory.recallMinSim', min: 0, max: 1, step: 0.01 },
  { key: 'graph_max_hops', labelKey: 'settings.memory.graphMaxHops', min: 1, max: 3, step: 1 },
]

// Curator provider is a known choice (mirrors the LLM Provider screen), not
// free text. Empty = inherit the system default.
const CURATOR_PROVIDERS = [
  { value: '', labelKey: 'settings.memory.providerDefault' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'generic', label: 'Generic / Local' },
]

// Always-shown text fields (independent of the curator provider choice).
const REST_FIELDS = [
  { key: 'embedding_model', labelKey: 'settings.memory.embeddingModel' },
  { key: 'qdrant_url', labelKey: 'settings.memory.qdrantUrl' },
]

const dirtyKeys = computed(() => {
  if (!config.value || !draft.value) return []
  return Object.keys(draft.value).filter(
    (k) => String(draft.value[k]) !== String(config.value[k]),
  )
})

const hasChanges = computed(() => dirtyKeys.value.length > 0 || apiKeyInput.value.length > 0)

async function load() {
  loading.value = true
  error.value = ''
  try {
    config.value = await apiFetch('/api/memory/settings')
    draft.value = { ...config.value }
    try { indexStatus.value = await apiFetch('/api/memory/index-status') } catch { /* non-fatal */ }
  } catch (err) {
    error.value = _friendly(err)
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!hasChanges.value || saving.value) return
  saving.value = true
  error.value = ''
  saveSuccess.value = false
  try {
    const patch = {}
    for (const k of dirtyKeys.value) {
      if (k === 'curator_api_key_set') continue
      const cur = config.value[k]
      patch[k] = typeof cur === 'boolean' ? Boolean(draft.value[k])
        : typeof cur === 'number' ? Number(draft.value[k])
          : String(draft.value[k] ?? '').trim()
    }
    if (apiKeyInput.value.length > 0) patch.curator_api_key = apiKeyInput.value
    config.value = await apiFetch('/api/memory/settings', {
      method: 'PATCH', body: JSON.stringify(patch),
    })
    draft.value = { ...config.value }
    apiKeyInput.value = ''
    saveSuccess.value = true
    setTimeout(() => { saveSuccess.value = false }, 2500)
  } catch (err) {
    error.value = _friendly(err)
  } finally {
    saving.value = false
  }
}

function _friendly(err) {
  if (err instanceof ApiError && err.body && typeof err.body === 'object') {
    const detail = err.body.detail
    if (typeof detail === 'string') return detail
    if (detail && typeof detail === 'object') return detail.message || 'Request failed.'
  }
  return err?.message || String(err)
}

onMounted(load)
</script>

<template>
  <div class="memory-settings">
    <div class="intro">{{ t('settings.memory.intro') }}</div>

    <p v-if="loading" class="muted">{{ t('common.loading') }}</p>
    <p v-if="error" class="error">{{ error }}</p>

    <div v-if="indexStatus" class="status-card">
      <div class="status-line">
        <span class="dot" :class="indexStatus.qdrant?.reachable ? 'ok' : 'down'"></span>
        <strong>{{ t('settings.memory.qdrant') }}</strong>
        <span v-if="indexStatus.qdrant?.reachable" class="muted">
          {{ t('settings.memory.qdrantConnected') }} · {{ t('settings.memory.qdrantPoints', { points: indexStatus.qdrant.points ?? 0 }) }}
        </span>
        <span v-else class="warn">
          {{ t('settings.memory.qdrantDownPrefix') }} <code>{{ indexStatus.qdrant?.configured_url }}</code>.
        </span>
      </div>
      <div class="status-line muted" v-if="indexStatus.outbox">
        {{ t('settings.memory.indexQueue') }}:
        {{ t('settings.memory.queuePending', { n: indexStatus.outbox.pending || 0 }) }}<span v-if="indexStatus.outbox.dead">, <span class="warn">{{ t('settings.memory.queueDead', { n: indexStatus.outbox.dead }) }}</span></span>
      </div>
    </div>

    <template v-if="draft">
      <!-- Toggles + enum selects (compact rows) -->
      <div class="row">
        <div class="row-info">
          <h3>{{ t('settings.memory.enable') }}</h3>
          <p class="desc">{{ t('settings.memory.enableDesc') }}</p>
        </div>
        <button class="toggle" :class="{ on: draft.enabled }" :aria-pressed="draft.enabled"
                @click="draft.enabled = !draft.enabled"><span class="knob"></span></button>
      </div>

      <div class="row">
        <div class="row-info"><h3>{{ t('settings.memory.mode') }}</h3>
          <p class="desc">{{ t('settings.memory.modeDesc') }}</p></div>
        <select v-model="draft.mode" class="select">
          <option v-for="m in MODES" :key="m" :value="m">{{ m }}</option>
        </select>
      </div>

      <div class="row">
        <div class="row-info"><h3>{{ t('settings.memory.autoCapture') }}</h3>
          <p class="desc">{{ t('settings.memory.autoCaptureDesc') }}</p></div>
        <button class="toggle" :class="{ on: draft.auto_capture_preferences }" :aria-pressed="draft.auto_capture_preferences"
                @click="draft.auto_capture_preferences = !draft.auto_capture_preferences"><span class="knob"></span></button>
      </div>

      <div class="row">
        <div class="row-info"><h3>{{ t('settings.memory.approvalPolicy') }}</h3>
          <p class="desc">{{ t('settings.memory.approvalPolicyDesc') }}</p></div>
        <select v-model="draft.approval_policy" class="select">
          <option v-for="p in POLICIES" :key="p" :value="p">{{ p }}</option>
        </select>
      </div>

      <div class="row">
        <div class="row-info"><h3>{{ t('settings.memory.reranker') }}</h3>
          <p class="desc">{{ t('settings.memory.rerankerDesc') }}</p></div>
        <button class="toggle" :class="{ on: draft.reranker_enabled }" :aria-pressed="draft.reranker_enabled"
                @click="draft.reranker_enabled = !draft.reranker_enabled"><span class="knob"></span></button>
      </div>

      <div class="row">
        <div class="row-info"><h3>{{ t(NUMBER_FIELDS[0].labelKey) }}</h3></div>
        <input v-model="draft.pinned_token_budget" class="num-input" type="number" min="0" max="100000" step="100" />
      </div>
      <div class="row">
        <div class="row-info"><h3>{{ t(NUMBER_FIELDS[1].labelKey) }}</h3></div>
        <input v-model="draft.evidence_token_budget" class="num-input" type="number" min="0" max="100000" step="100" />
      </div>
      <div class="row">
        <div class="row-info"><h3>{{ t(NUMBER_FIELDS[2].labelKey) }}</h3></div>
        <input v-model="draft.retention_episodic_days" class="num-input" type="number" min="1" max="3650" step="1" />
      </div>
      <div class="row">
        <div class="row-info"><h3>{{ t(NUMBER_FIELDS[3].labelKey) }}</h3></div>
        <input v-model="draft.retention_retrieval_runs_days" class="num-input" type="number" min="1" max="365" step="1" />
      </div>
      <div class="row">
        <div class="row-info"><h3>{{ t(NUMBER_FIELDS[4].labelKey) }}</h3>
          <p class="hint">{{ t('settings.memory.recallMinSimHint') }}</p></div>
        <input v-model="draft.recall_min_similarity" class="num-input" type="number" min="0" max="1" step="0.01" />
      </div>
      <div class="row">
        <div class="row-info"><h3>{{ t(NUMBER_FIELDS[5].labelKey) }}</h3>
          <p class="hint">{{ t('settings.memory.graphMaxHopsHint') }}</p></div>
        <input v-model="draft.graph_max_hops" class="num-input" type="number" min="1" max="3" step="1" />
      </div>

      <!-- Curator / embedding / Qdrant — labelled fields, like the LLM Provider screen -->
      <div class="fieldset">
        <div class="field">
          <label>{{ t('settings.memory.curatorModel') }}</label>
          <input v-model="draft.curator_model" class="text-input" type="text"
                 :placeholder="t('settings.memory.modelInherit')" />
        </div>

        <div class="field">
          <label>{{ t('settings.memory.curatorProvider') }}</label>
          <select v-model="draft.curator_provider" class="text-input select-input">
            <option v-for="p in CURATOR_PROVIDERS" :key="p.value" :value="p.value">
              {{ p.labelKey ? t(p.labelKey) : p.label }}
            </option>
          </select>
        </div>

        <!-- Base URL + API key only matter when NOT inheriting (a specific
             provider is chosen); when inheriting they come from LLM Provider. -->
        <template v-if="draft.curator_provider">
          <div class="field">
            <label>{{ t('settings.memory.curatorBaseUrl') }}</label>
            <input v-model="draft.curator_base_url" class="text-input" type="text" />
          </div>

          <div class="field">
            <label>
              {{ t('settings.memory.curatorApiKey') }}
              <span v-if="draft.curator_api_key_set" class="key-status stored">{{ t('settings.memory.keyStored') }}</span>
              <span v-else class="key-status missing">{{ t('settings.memory.keyNotSet') }}</span>
            </label>
            <div class="input-group">
              <input v-model="apiKeyInput" class="pwd-input" :type="showKey ? 'text' : 'password'"
                     :placeholder="t('settings.memory.apiKeyPlaceholder')" />
              <button type="button" class="icon-btn" @click="showKey = !showKey" :aria-label="showKey ? 'Hide' : 'Show'">
                {{ showKey ? '🙈' : '👁' }}
              </button>
            </div>
            <span class="hint">
              {{ draft.curator_api_key_set ? t('settings.memory.curatorApiKeySet') : t('settings.memory.curatorApiKeyUnset') }}
            </span>
          </div>
        </template>

        <div v-for="f in REST_FIELDS" :key="f.key" class="field">
          <label>{{ t(f.labelKey) }}</label>
          <input v-model="draft[f.key]" class="text-input" type="text" />
        </div>
      </div>

      <div class="actions">
        <button class="btn primary" :disabled="!hasChanges || saving" @click="save">
          {{ saving ? t('common.saving') : t('common.save') }}
        </button>
        <span v-if="saveSuccess" class="ok">✓ {{ t('common.saved') }}</span>
        <span v-if="dirtyKeys.length || apiKeyInput" class="dirty">{{ dirtyKeys.length + (apiKeyInput ? 1 : 0) }} {{ t('common.changed') }}</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.memory-settings { max-width: 760px; }
.intro { color: var(--text-dim); font-size: 13px; line-height: 1.6; margin-bottom: 18px; }
.muted { color: var(--text-dim); }
.error { color: var(--danger); font-size: 13px; }

.status-card { background: var(--bg-2); border: 1px solid var(--border); border-radius: var(--r-md);
  padding: 12px 14px; margin-bottom: 18px; }
.status-line { display: flex; align-items: center; gap: 8px; font-size: 13px; flex-wrap: wrap; }
.status-line + .status-line { margin-top: 6px; }
.dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.dot.ok { background: var(--success); }
.dot.down { background: var(--danger); }
.warn { color: var(--warning); }
code { background: rgba(255,255,255,0.05); padding: 1px 5px; border-radius: 3px;
  font-family: var(--font-mono); font-size: 11.5px; color: var(--accent); }

/* Compact rows for toggles / selects / numbers */
.row { display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 14px 0; border-bottom: 1px solid var(--border); }
.row-info { flex: 1; min-width: 0; }
.row-info h3 { margin: 0 0 2px; font-size: 14px; font-weight: 600; color: var(--text); }
.desc { margin: 0; color: var(--text-dim); font-size: 12px; line-height: 1.5; }
.num-input, .select { background: var(--bg-4); color: var(--text);
  border: 1px solid var(--border-strong); border-radius: var(--r-md); padding: 9px 12px; font-size: 13px; }
.num-input { width: 120px; }
.select { width: 200px; }
.num-input:focus, .select:focus { outline: none; border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-bg-strong); }

/* Toggle — design-system primary (indigo) */
.toggle { flex-shrink: 0; width: 44px; height: 24px; border-radius: 24px; background: var(--bg-3);
  border: 1px solid var(--border-strong); cursor: pointer; position: relative; transition: background .2s, border-color .2s; }
.toggle:hover { border-color: var(--primary); }
.toggle.on { background: var(--primary); border-color: var(--primary); }
.knob { position: absolute; top: 2px; left: 2px; width: 18px; height: 18px; background: #fff;
  border-radius: 50%; transition: left .2s; }
.toggle.on .knob { left: 22px; }

/* Labelled fields — mirrors the LLM Provider screen */
.fieldset { margin-top: 8px; }
.field { margin-top: 14px; }
.field label { display: flex; gap: 8px; align-items: center; font-family: var(--font-mono);
  font-size: 10px; font-weight: 500; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--text-muted); margin-bottom: 6px; }
.key-status { font-family: var(--font-mono); font-weight: 500; padding: 1px 8px; border-radius: 999px;
  font-size: 10px; letter-spacing: 0.04em; text-transform: lowercase; }
.key-status.stored { background: var(--success-bg); color: var(--success); }
.key-status.missing { background: var(--warning-bg); color: var(--warning); }
.text-input, .pwd-input { width: 100%; background: var(--bg-4); border: 1px solid var(--border-strong);
  border-radius: var(--r-md); padding: 10px 14px; color: var(--text); font-family: inherit; font-size: 13px; }
.pwd-input { padding-right: 44px; }
.text-input:focus, .pwd-input:focus { outline: none; border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-bg-strong); }
.select-input { cursor: pointer; appearance: none; padding-right: 36px;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%237B8094' d='M3 4.5L6 7.5L9 4.5'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 14px center; }
.input-group { position: relative; display: flex; align-items: center; }
.icon-btn { position: absolute; right: 8px; background: transparent; border: none; color: var(--text-muted);
  cursor: pointer; padding: 6px; border-radius: var(--r-sm); font-size: 14px; }
.icon-btn:hover { color: var(--text); background: rgba(255,255,255,0.04); }
.hint { display: block; margin-top: 6px; font-size: 12px; color: var(--text-subtle); line-height: 1.4; }

/* Primary action button */
.actions { display: flex; align-items: center; gap: 12px; margin-top: 24px; }
.btn { padding: 9px 16px; font-family: inherit; font-size: 13px; font-weight: 500;
  border-radius: var(--r-md); border: 1px solid var(--border-strong); background: transparent;
  color: var(--text-dim); cursor: pointer; transition: all .15s; }
.btn.primary { background: var(--primary); color: #fff; border-color: var(--primary); }
.btn.primary:hover:not([disabled]) { background: var(--primary-hover); border-color: var(--primary-hover); }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.ok { color: var(--success); font-size: 13px; }
.dirty { color: var(--text-dim); font-size: 12px; }

@media (max-width: 768px) {
  .row { flex-direction: column; align-items: stretch; }
  .num-input, .select { width: 100%; }
}
</style>
