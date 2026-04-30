<script setup>
/**
 * Settings → LLM Provider.
 *
 * Per-provider storage: each of the three slots (OpenAI / Anthropic / Custom)
 * has its own api_key and base_url in the DB, stored under keys
 * ``llm.{slot}_api_key`` and ``llm.{slot}_base_url``.  Switching tabs
 * hydrates that slot's state independently — saving one slot never touches
 * another slot's credentials.
 *
 * UI ↔ DB naming
 * --------------
 * The UI exposes three labels (OpenAI, Anthropic, Generic / Local).  The
 * first two map 1:1 to fast-agent's provider names; "Generic / Local" maps
 * to fast-agent's ``generic`` slot — the canonical slot for self-hosted
 * models behind non-OpenAI-wire endpoints (Ollama raw, llama.cpp, LM Studio).
 * OpenAI-wire proxies (CLIProxyAPI, 9router, LiteLLM) should use the
 * OpenAI tab since they speak OpenAI's protocol on the wire.  This
 * translation happens in the backend too, so the DB only stores
 * ``openai`` / ``anthropic`` / ``generic``.
 *
 * Secrets are always masked on read — the backend returns ``has_value: true``
 * plus a null ``value`` so we can render a "stored · hidden" badge without
 * ever exposing the ciphertext to the browser.
 */
import { ref, computed, onMounted, watch } from 'vue'
import { useSettingsStore } from '../../stores/settings'
import { useConfirm } from '../../composables/useConfirm'
import { apiFetch } from '../../api'

const store = useSettingsStore()
const { confirm } = useConfirm()

const PROVIDERS = [
  {
    ui: 'openai',
    slot: 'openai',
    label: 'OpenAI',
    sub: 'Official API or OpenAI-compatible proxy',
    defaultModel: 'gpt-4o',
    defaultBase: '',
    keyPrefix: 'sk-…',
    hint: 'Pick this for api.openai.com OR any OpenAI-wire proxy (9router, CLIProxyAPI, LiteLLM). Models are addressed as openai.{model}.',
  },
  {
    ui: 'anthropic',
    slot: 'anthropic',
    label: 'Anthropic',
    sub: 'Claude 3.5 / 4',
    defaultModel: 'claude-sonnet-4-20250514',
    defaultBase: '',
    keyPrefix: 'sk-ant-api03-…',
    hint: 'Official Anthropic API. Models are addressed as anthropic.{model}.',
  },
  {
    ui: 'custom',
    // Fast-agent's "generic" slot is reserved for self-hosted models that
    // don't speak OpenAI wire format — keeping it separate from "openai"
    // means a user running 9router on :20128 can still point the openai
    // slot there without clobbering a genuine api.openai.com setup.
    slot: 'generic',
    label: 'Generic / Local',
    sub: 'Ollama, llama.cpp, LM Studio',
    defaultModel: '',
    defaultBase: 'http://localhost:11434/v1',
    keyPrefix: '(often empty for local)',
    hint: 'Self-hosted models addressed as generic.{model}. Remember to update default_model in fastagent.config.yaml if you want this slot to boot by default.',
  },
]
const SLOT_BY_UI = Object.fromEntries(PROVIDERS.map((p) => [p.ui, p.slot]))
const META_BY_UI = Object.fromEntries(PROVIDERS.map((p) => [p.ui, p]))

const activeUi = ref('anthropic')
const model = ref('')
const initialModel = ref('')

// Per-provider buffers: each slot holds its own pending edits, hasStoredKey
// flag, and reveal state so switching tabs feels like moving between
// independent mini-forms.
function blankBuffers() {
  return Object.fromEntries(
    PROVIDERS.map((p) => [
      p.ui,
      { apiKey: '', baseUrl: '', hasStoredKey: false, reveal: false },
    ]),
  )
}
const buffers = ref(blankBuffers())
const initialBaseUrl = ref({})
const savingSlot = ref(null)
const error = ref('')
const success = ref('')
// LLM credentials are baked into fast-agent's OpenAI/Anthropic clients at
// process boot — env/YAML updates don't refresh the in-memory client, so
// a fresh save only takes effect after a backend restart.  This flag makes
// that fact visible without forcing an unconfirmed restart.
const restartPending = ref(false)
const restarting = ref(false)

const meta = computed(() => META_BY_UI[activeUi.value] || META_BY_UI.custom)
const activeBuf = computed(() => buffers.value[activeUi.value])

const dirty = computed(() => {
  const buf = activeBuf.value
  const slotDirty =
    buf.apiKey.trim().length > 0 ||
    buf.baseUrl.trim() !== (initialBaseUrl.value[activeUi.value] || '')
  const providerDirty = activeUi.value !== (store.getValue('llm', 'provider') || '')
  const modelDirty = model.value.trim() !== initialModel.value
  return slotDirty || providerDirty || modelDirty
})

const canSave = computed(() => {
  if (savingSlot.value) return false
  if (!dirty.value) return false
  if (!model.value.trim()) return false
  const buf = activeBuf.value
  // Require a key only when the slot has nothing stored yet; otherwise the
  // user may be rotating just the base URL or changing the default model.
  if (!buf.hasStoredKey && !buf.apiKey.trim()) return false
  return true
})

async function refresh() {
  await store.fetchAll().catch(() => {})

  const storedProvider = store.getValue('llm', 'provider') || ''
  const storedModel = store.getValue('llm', 'model') || ''

  // Hydrate each slot independently from its namespaced keys.
  const next = blankBuffers()
  const nextInitialBase = {}
  for (const p of PROVIDERS) {
    const apiKeyEntry = store.getEntry('llm', `${p.slot}_api_key`)
    const baseUrl = store.getValue('llm', `${p.slot}_base_url`) || ''
    next[p.ui] = {
      apiKey: '',
      baseUrl,
      hasStoredKey: Boolean(apiKeyEntry?.has_value),
      reveal: false,
    }
    nextInitialBase[p.ui] = baseUrl
  }
  buffers.value = next
  initialBaseUrl.value = nextInitialBase

  if (storedProvider && META_BY_UI[storedProvider]) {
    activeUi.value = storedProvider
  }
  model.value = storedModel
  initialModel.value = storedModel
}

function pickProvider(ui) {
  activeUi.value = ui
  // Fill model default when switching to a slot that's never had a model
  // persisted to the shared ``llm.model`` field — but never overwrite a
  // user's custom string.
  const m = META_BY_UI[ui]
  if (!model.value.trim() && m.defaultModel) {
    model.value = m.defaultModel
  }
  // Same idea for base URL: only seed when empty for this slot.
  if (!buffers.value[ui].baseUrl && m.defaultBase) {
    buffers.value[ui].baseUrl = m.defaultBase
  }
}

async function onSave() {
  if (!canSave.value) return
  const ui = activeUi.value
  const slot = SLOT_BY_UI[ui]
  const buf = buffers.value[ui]
  savingSlot.value = slot
  error.value = ''
  success.value = ''

  try {
    const items = [
      // Active provider + default model are shared fields — updating them
      // reflects the user's intent to make this slot the boot default.
      { category: 'llm', key: 'provider', value: ui, is_secret: false },
      { category: 'llm', key: 'model', value: model.value.trim(), is_secret: false },
      {
        category: 'llm',
        key: `${slot}_base_url`,
        value: buf.baseUrl.trim() || null,
        is_secret: false,
      },
    ]
    if (buf.apiKey.trim()) {
      items.push({
        category: 'llm',
        key: `${slot}_api_key`,
        value: buf.apiKey.trim(),
        is_secret: true,
      })
    }
    await apiFetch('/api/settings/bulk', {
      method: 'POST',
      body: JSON.stringify({ items }),
    })
    await refresh()
    success.value = `${META_BY_UI[ui].label} settings saved.`
    restartPending.value = true
  } catch (err) {
    error.value = err?.body?.detail || err?.message || String(err)
  } finally {
    savingSlot.value = null
  }
}

async function onClearKey() {
  const ui = activeUi.value
  const slot = SLOT_BY_UI[ui]
  const label = META_BY_UI[ui].label
  if (
    !(await confirm({
      title: `Clear ${label} API key`,
      message: `Remove the stored ${label} API key? Jarvis will fail to reach ${label} until you save a new one.`,
      confirmText: 'Clear Key',
      variant: 'danger',
    }))
  ) {
    return
  }
  savingSlot.value = slot
  error.value = ''
  success.value = ''
  try {
    await apiFetch(`/api/settings/llm/${slot}_api_key`, { method: 'DELETE' })
    await refresh()
    success.value = `${label} API key removed.`
  } catch (err) {
    error.value = err?.body?.detail || err?.message || String(err)
  } finally {
    savingSlot.value = null
  }
}

async function onRestart() {
  if (restarting.value) return
  if (
    !(await confirm({
      title: 'Restart backend',
      message:
        'Any in-progress chats will be interrupted. Your new LLM credentials take effect when the process comes back (~5–10s under docker-compose).',
      confirmText: 'Restart Now',
      variant: 'warning',
    }))
  ) {
    return
  }
  restarting.value = true
  error.value = ''
  try {
    await store.restartBackend()
    // We won't actually see a clean response — the backend sends SIGTERM to
    // itself immediately. Treat network errors here as expected.
    success.value = 'Restart requested — backend will be back in a few seconds.'
    restartPending.value = false
  } catch (err) {
    // ``/api/system/restart`` returns before the SIGTERM fires, so a
    // successful call may *or may not* reach us depending on timing.  A
    // failure here means either the POST genuinely failed (auth, network)
    // or the server died cleanly mid-response — surface both kindly.
    const msg = err?.body?.detail || err?.message || String(err)
    if (/NetworkError|Failed to fetch|ECONNREFUSED|aborted/i.test(msg)) {
      success.value = 'Restart signal sent — backend going down now.'
      restartPending.value = false
    } else {
      error.value = `Restart failed: ${msg}`
    }
  } finally {
    restarting.value = false
  }
}

// Clear stale success/error toasts when switching tabs — they belong to
// the previous slot's context. We keep ``restartPending`` across tabs on
// purpose: if the user saved OpenAI then switched to Anthropic, the
// OpenAI restart is still needed.
watch(activeUi, () => {
  error.value = ''
  success.value = ''
})

onMounted(refresh)
</script>

<template>
  <div class="gen-sections">
    <section class="panel-card">
      <header>
        <div class="icon-circle">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 2a4 4 0 0 0-4 4v2a4 4 0 0 0 8 0V6a4 4 0 0 0-4-4z" />
            <path d="M4 12h16" />
            <path d="M6 20h12a2 2 0 0 0 2-2v-6H4v6a2 2 0 0 0 2 2z" />
          </svg>
        </div>
        <div>
          <h2>LLM Provider</h2>
          <p>
            Each provider has its own credentials — switching tabs swaps the stored key and
            base URL independently. The <strong>active</strong> provider (highlighted) is the
            one Jarvis boots with; other providers stay configured and can be referenced by
            fully-qualified model aliases (e.g. <code>openai.gpt-4o</code>).
          </p>
        </div>
      </header>

      <div class="provider-grid">
        <button
          v-for="p in PROVIDERS"
          :key="p.ui"
          type="button"
          class="provider-card"
          :class="{ selected: activeUi === p.ui }"
          @click="pickProvider(p.ui)"
        >
          <span class="provider-title">
            {{ p.label }}
            <span v-if="buffers[p.ui].hasStoredKey" class="mini-dot" title="Key stored"></span>
          </span>
          <span class="provider-sub">{{ p.sub }}</span>
        </button>
      </div>

      <div v-if="meta.hint" class="provider-hint">{{ meta.hint }}</div>

      <div class="field">
        <label for="llm-model">Default Model</label>
        <input
          id="llm-model"
          class="text-input"
          type="text"
          :placeholder="meta.defaultModel || 'e.g. gpt-4o'"
          v-model="model"
        />
        <span class="hint">
          FastAgent format: <code>provider.model[.effort]</code> or a model alias (e.g.
          <code>sonnet</code>, <code>gpt-4o</code>, <code>openai.coding-agent</code>).
        </span>
      </div>

      <div class="field">
        <label :for="`llm-base-${activeUi}`">{{ meta.label }} Base URL</label>
        <input
          :id="`llm-base-${activeUi}`"
          class="text-input"
          type="url"
          :placeholder="meta.defaultBase || 'Leave blank to use provider default'"
          v-model="activeBuf.baseUrl"
        />
        <span class="hint">
          Required when pointing at a proxy or local endpoint. Leave blank to use the provider
          default. Stored under <code>llm.{{ SLOT_BY_UI[activeUi] }}_base_url</code>.
        </span>
      </div>

      <div class="field">
        <label :for="`llm-key-${activeUi}`">
          {{ meta.label }} API Key
          <span v-if="activeBuf.hasStoredKey" class="key-status stored">stored · hidden</span>
          <span v-else class="key-status missing">not set</span>
        </label>
        <div class="input-group">
          <input
            :id="`llm-key-${activeUi}`"
            class="pwd-input"
            :type="activeBuf.reveal ? 'text' : 'password'"
            autocomplete="off"
            :placeholder="activeBuf.hasStoredKey ? 'Leave blank to keep current key' : meta.keyPrefix || 'Paste API key'"
            v-model="activeBuf.apiKey"
          />
          <button
            type="button"
            class="icon-btn"
            @click="activeBuf.reveal = !activeBuf.reveal"
            :title="activeBuf.reveal ? 'Hide' : 'Reveal'"
          >
            <svg v-if="activeBuf.reveal" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
              <line x1="1" y1="1" x2="23" y2="23" />
            </svg>
            <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
              <circle cx="12" cy="12" r="3" />
            </svg>
          </button>
        </div>
        <span class="hint">
          Keys are encrypted with <code>JARVIS_API_KEY</code> before touching disk and synced
          to <code>fastagent.secrets.yaml</code> so fast-agent subprocesses pick them up.
        </span>
      </div>

      <div class="action-row">
        <button
          v-if="activeBuf.hasStoredKey"
          type="button"
          class="btn ghost"
          :disabled="savingSlot !== null"
          @click="onClearKey"
        >
          Clear Stored Key
        </button>
        <button type="button" class="btn primary" :disabled="!canSave" @click="onSave">
          {{ savingSlot === SLOT_BY_UI[activeUi] ? 'Saving…' : `Save ${meta.label}` }}
        </button>
      </div>

      <div v-if="error" class="error-msg">{{ error }}</div>
      <div v-if="success" class="success-msg">{{ success }}</div>

      <div v-if="restartPending" class="restart-banner">
        <div class="restart-copy">
          <strong>Restart required</strong>
          <span>
            LLM credentials are cached inside fast-agent's in-memory clients at boot, so your
            changes won't reach the model until the backend process restarts.
          </span>
        </div>
        <button
          type="button"
          class="btn primary small"
          :disabled="restarting"
          @click="onRestart"
        >
          {{ restarting ? 'Restarting…' : 'Restart Backend Now' }}
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.gen-sections { display: flex; flex-direction: column; gap: 20px; }
.panel-card {
  background: var(--bg-card, #111318);
  border: 1px solid var(--border, #1e2030);
  border-radius: 12px;
  padding: 28px;
}
.panel-card > header {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  margin-bottom: 24px;
}
.panel-card h2 { font-size: 16px; font-weight: 600; color: var(--text-primary, #f0f2f5); }
.panel-card header p {
  margin-top: 4px;
  font-size: 13px;
  color: var(--text-nav, #8b8fa3);
  line-height: 1.5;
}
.panel-card header strong { color: var(--text-primary, #f0f2f5); }
.icon-circle {
  flex-shrink: 0;
  width: 36px; height: 36px;
  border-radius: 10px;
  background: rgba(59, 130, 246, 0.12);
  color: var(--accent-blue, #3b82f6);
  display: grid; place-items: center;
}

.provider-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
  margin-bottom: 18px;
}
.provider-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  text-align: left;
  padding: 14px 16px;
  border: 1px solid var(--border, #1e2030);
  border-radius: 10px;
  background: var(--bg-input, #0f172a);
  color: var(--text-primary, #f0f2f5);
  cursor: pointer;
  transition: all 0.15s;
  font-family: inherit;
}
.provider-card:hover { border-color: rgba(59, 130, 246, 0.4); }
.provider-card.selected {
  border-color: var(--accent-blue, #3b82f6);
  background: rgba(59, 130, 246, 0.08);
}
.provider-title {
  font-size: 14px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 6px;
}
.provider-sub { font-size: 12px; color: var(--text-nav, #8b8fa3); }
.mini-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #22c55e;
  display: inline-block;
}

.provider-hint {
  margin: 4px 0 18px;
  padding: 10px 12px;
  background: rgba(99, 102, 241, 0.08);
  border-left: 3px solid rgba(99, 102, 241, 0.6);
  border-radius: 4px;
  font-size: 13px;
  line-height: 1.45;
  color: var(--text-primary, #c8cad7);
}

.field { margin-top: 14px; }
.field label {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-nav, #8b8fa3);
  margin-bottom: 8px;
}
.key-status {
  font-weight: 500;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
  text-transform: lowercase;
}
.key-status.stored { background: rgba(34, 197, 94, 0.12); color: #22c55e; }
.key-status.missing { background: rgba(245, 158, 11, 0.12); color: #f59e0b; }

.text-input,
.pwd-input {
  width: 100%;
  background: var(--bg-input, #0f172a);
  border: 1px solid var(--border-input, #1e2030);
  border-radius: 8px;
  padding: 11px 14px;
  color: var(--text-primary, #f0f2f5);
  font-family: inherit;
  font-size: 14px;
}
.pwd-input { padding-right: 40px; }
.text-input:focus,
.pwd-input:focus {
  outline: none;
  border-color: var(--accent-blue, #3b82f6);
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
}

.input-group { position: relative; display: flex; align-items: center; }
.icon-btn {
  position: absolute;
  right: 8px;
  background: transparent;
  border: none;
  color: var(--text-nav, #8b8fa3);
  cursor: pointer;
  padding: 6px;
  border-radius: 6px;
}
.icon-btn:hover { color: var(--text-primary, #f0f2f5); background: rgba(255, 255, 255, 0.04); }

.hint {
  display: block;
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-sub, #555872);
  line-height: 1.4;
}
.hint code {
  background: rgba(255, 255, 255, 0.05);
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 11.5px;
}

.action-row {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 18px;
}
.btn {
  padding: 9px 16px;
  font-family: inherit;
  font-size: 13px;
  font-weight: 600;
  border-radius: 8px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-nav, #8b8fa3);
  cursor: pointer;
  transition: all 0.15s;
}
.btn.ghost:hover:not([disabled]) { color: var(--text-primary, #f0f2f5); background: rgba(255, 255, 255, 0.04); }
.btn.primary {
  background: var(--accent-blue, #3b82f6);
  color: #ffffff;
  border-color: var(--accent-blue, #3b82f6);
}
.btn.primary:hover:not([disabled]) { background: #2f6cdc; border-color: #2f6cdc; }
.btn[disabled] { opacity: 0.5; cursor: not-allowed; }

.error-msg {
  margin-top: 14px;
  padding: 10px 14px;
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: 8px;
  color: #ef4444;
  font-size: 13px;
  line-height: 1.4;
}
.success-msg {
  margin-top: 14px;
  padding: 10px 14px;
  background: rgba(34, 197, 94, 0.08);
  border: 1px solid rgba(34, 197, 94, 0.3);
  border-radius: 8px;
  color: #22c55e;
  font-size: 13px;
}

.restart-banner {
  margin-top: 14px;
  padding: 12px 14px;
  display: flex;
  align-items: center;
  gap: 16px;
  background: rgba(245, 158, 11, 0.08);
  border: 1px solid rgba(245, 158, 11, 0.3);
  border-radius: 8px;
  color: #f59e0b;
  font-size: 13px;
  line-height: 1.45;
}
.restart-copy {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.restart-copy strong { color: #fbbf24; font-size: 13px; font-weight: 600; }
.restart-copy span { color: var(--text-nav, #8b8fa3); }
.btn.primary.small { padding: 7px 12px; font-size: 12px; }
</style>
