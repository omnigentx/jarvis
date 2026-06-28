<script setup>
/**
 * Settings → Messaging Gateways.
 *
 * One card per registered platform (Telegram, Zalo, …). The platform LIST is
 * driven by `GET /api/gateways` — add a gateway in the backend and its card
 * appears here automatically, no frontend change needed.
 *
 * Config (enabled / token / allow-list / agent) is written through the generic
 * `/api/settings/bulk` endpoint under category `gateways`; the backend
 * GatewayManager subscribes to those changes and live-reloads (no restart), so
 * after Save we re-poll status to reflect "Connected as @bot".
 */
import { ref, reactive, onMounted } from 'vue'
import { apiFetch } from '../../api'
import { useLang } from '../../composables/useLang'

const { t } = useLang()

// Display metadata per platform. Proper nouns + doc links — not translated.
// Unknown platforms fall back to a generic label so the card still renders.
const META = {
  telegram: { label: 'Telegram', tokenHelp: 'https://t.me/BotFather', tokenHint: '@BotFather → /newbot' },
  zalo: { label: 'Zalo', tokenHelp: 'https://bot.zaloplatforms.com', tokenHint: 'bot.zaloplatforms.com' },
}
function metaFor(id) {
  return META[id] || { label: id, tokenHelp: '', tokenHint: '' }
}

const loading = ref(true)
const loadError = ref('')
const agents = ref([])
// Per-platform reactive draft + runtime state, keyed by platform id.
const rows = reactive({})

function blankRow() {
  return {
    enabled: false,
    hasToken: false,
    tokenInput: '',
    allowFrom: '', // comma-separated user ids (or *)
    agent: 'Jarvis',
    // runtime status
    running: false,
    connected: false,
    botUsername: null,
    lastError: null,
    // ui flags
    saving: false,
    saved: false,
    error: '',
    testing: false,
    testResult: null, // { ok, name|error }
  }
}

async function loadAgents() {
  try {
    const list = await apiFetch('/api/agents')
    agents.value = Array.isArray(list) ? list : (list?.items || [])
  } catch {
    agents.value = []
  }
}

async function loadConfig() {
  // Editable values (enabled / allow_from / agent / token-present) live in
  // the settings category; secret token comes back masked (has_value only).
  const resp = await apiFetch('/api/settings/gateways').catch(() => ({ items: [] }))
  const items = resp?.items || []
  const byKey = Object.fromEntries(items.map((i) => [i.key, i]))
  for (const id of Object.keys(rows)) {
    const r = rows[id]
    r.enabled = byKey[`${id}_enabled`]?.value === 'true'
    r.hasToken = !!byKey[`${id}_token`]?.has_value
    r.agent = byKey[`${id}_agent`]?.value || 'Jarvis'
    let allow = []
    try { allow = JSON.parse(byKey[`${id}_allow_from`]?.value || '[]') } catch { allow = [] }
    r.allowFrom = Array.isArray(allow) ? allow.join(', ') : ''
  }
}

async function loadStatus() {
  const resp = await apiFetch('/api/gateways').catch(() => ({ gateways: [] }))
  for (const s of resp?.gateways || []) {
    if (!rows[s.platform]) rows[s.platform] = blankRow()
    const r = rows[s.platform]
    r.running = s.running
    r.connected = s.connected
    r.botUsername = s.bot_username
    r.lastError = s.last_error
  }
}

async function reload() {
  loading.value = true
  loadError.value = ''
  try {
    // Status first: it enumerates every registered platform → seeds the cards.
    await loadStatus()
    // Guarantee at least the known platforms render even if status is empty.
    for (const id of Object.keys(META)) if (!rows[id]) rows[id] = blankRow()
    await Promise.all([loadConfig(), loadAgents()])
  } catch (err) {
    loadError.value = err?.body?.detail || err?.message || String(err)
  } finally {
    loading.value = false
  }
}

function parseAllowFrom(text) {
  return text
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
}

// "*" = allow everyone — dangerous (the bot exposes the agent's shell/Gmail/IoT
// tools to anyone who messages it). Drives the inline warning.
function allowsEveryone(r) {
  return parseAllowFrom(r.allowFrom).includes('*')
}

async function testConnection(id) {
  const r = rows[id]
  const token = r.tokenInput.trim()
  // Need either a freshly-typed token or one already saved to test against.
  if (!token && !r.hasToken) {
    r.testResult = { ok: false, error: t('settings.gateways.needTokenToTest') }
    return
  }
  r.testing = true
  r.testResult = null
  try {
    // Omit the token to test the already-saved one (backend reads it from DB).
    const res = await apiFetch(`/api/gateways/${id}/test`, {
      method: 'POST',
      body: JSON.stringify(token ? { token } : {}),
    })
    r.testResult = res
  } catch (err) {
    r.testResult = { ok: false, error: err?.body?.detail || err?.message || String(err) }
  } finally {
    r.testing = false
  }
}

// After a save the backend live-reloads (a token change briefly reconnects).
// Re-poll status a few times so the chip transitions to "Connected" on its own
// instead of showing a stale mid-reload state.
function pollStatusAfterSave() {
  let n = 0
  const tick = () => {
    loadStatus()
    loadConfig()
    if (++n < 6) setTimeout(tick, 2000)
  }
  setTimeout(tick, 800)
}

async function save(id) {
  const r = rows[id]
  r.saving = true
  r.saved = false
  r.error = ''
  r.testResult = null  // a fresh save supersedes any stale test result
  // Only send the token when the user typed a new one — leaving it blank keeps
  // the previously-saved secret intact.
  const items = [
    { category: 'gateways', key: `${id}_enabled`, value: r.enabled ? 'true' : 'false', is_secret: false },
    { category: 'gateways', key: `${id}_allow_from`, value: JSON.stringify(parseAllowFrom(r.allowFrom)), is_secret: false },
    { category: 'gateways', key: `${id}_agent`, value: r.agent || 'Jarvis', is_secret: false },
  ]
  const token = r.tokenInput.trim()
  if (token) items.push({ category: 'gateways', key: `${id}_token`, value: token, is_secret: true })

  try {
    await apiFetch('/api/settings/bulk', {
      method: 'POST',
      body: JSON.stringify({ items }),
    })
    r.saved = true
    r.tokenInput = ''
    pollStatusAfterSave()
  } catch (err) {
    r.error = err?.body?.detail || err?.message || String(err)
  } finally {
    r.saving = false
  }
}

// Status chip text/role: connected (green), enabled-but-not-connected (amber),
// off (muted). Mirrors the running/error semantics from manager.status().
function statusRole(r) {
  if (r.enabled && r.connected) return 'on'
  if (r.enabled && r.lastError) return 'err'
  if (r.enabled) return 'warn'
  return 'off'
}
function statusText(r) {
  if (r.enabled && r.connected) {
    return r.botUsername
      ? t('settings.gateways.connectedAs', { name: r.botUsername })
      : t('settings.gateways.connected')
  }
  if (r.enabled && r.lastError) return t('settings.gateways.statusError')
  if (r.enabled) return t('settings.gateways.connecting')
  return t('settings.gateways.disabled')
}

onMounted(reload)
</script>

<template>
  <div class="gw-sections">
    <p class="intro muted">{{ t('settings.gateways.intro') }}</p>

    <div v-if="loading" class="muted">{{ t('common.loading') }}</div>
    <div v-else-if="loadError" class="error-msg">{{ loadError }}</div>

    <section
      v-for="(r, id) in rows"
      v-else
      :key="id"
      class="service-card"
    >
      <div class="hud-corner" />
      <header class="card-header">
        <div class="icon-circle">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
          </svg>
        </div>
        <div class="card-title-block">
          <h2>{{ metaFor(id).label }}</h2>
          <p>{{ t('settings.gateways.cardDesc', { name: metaFor(id).label }) }}</p>
        </div>
        <span class="status-chip" :class="statusRole(r)">
          <span class="dot" />
          {{ statusText(r) }}
        </span>
      </header>

      <div class="card-body">
        <!-- Enable -->
        <label class="switch-row">
          <input type="checkbox" v-model="r.enabled" />
          <span class="switch-track"><span class="switch-thumb" /></span>
          <span class="switch-label">{{ t('settings.gateways.enable', { name: metaFor(id).label }) }}</span>
        </label>

        <!-- Token -->
        <div class="field">
          <label class="field-label">{{ t('settings.gateways.tokenLabel') }}</label>
          <input
            class="text-input mono"
            type="password"
            autocomplete="new-password"
            :placeholder="r.hasToken ? t('settings.gateways.tokenSavedPlaceholder') : t('settings.gateways.tokenPlaceholder')"
            v-model="r.tokenInput"
          />
          <p class="hint">
            {{ t('settings.gateways.tokenHint') }}
            <a v-if="metaFor(id).tokenHelp" :href="metaFor(id).tokenHelp" target="_blank" rel="noopener">{{ metaFor(id).tokenHint }}</a>
            <span v-else>{{ metaFor(id).tokenHint }}</span>
          </p>
        </div>

        <!-- Allow-list -->
        <div class="field">
          <label class="field-label">{{ t('settings.gateways.allowLabel') }}</label>
          <input
            class="text-input mono"
            :class="{ danger: allowsEveryone(r) }"
            placeholder="123456789, 987654321"
            v-model="r.allowFrom"
          />
          <!-- Loud warning when the list is "*": anyone on the platform can then
               drive the agent (which has shell/Gmail/IoT tools). -->
          <p v-if="allowsEveryone(r)" class="allow-danger">
            ⚠️ {{ t('settings.gateways.allowAllWarning') }}
          </p>
          <p class="hint">{{ t('settings.gateways.allowHint') }}</p>
        </div>

        <!-- Agent -->
        <div class="field">
          <label class="field-label">{{ t('settings.gateways.agentLabel') }}</label>
          <select class="text-input" v-model="r.agent">
            <option v-for="a in agents" :key="a.name" :value="a.name">{{ a.name }}</option>
            <option v-if="!agents.length" value="Jarvis">Jarvis</option>
          </select>
        </div>

        <!-- Test result -->
        <div v-if="r.testResult" :class="r.testResult.ok ? 'success-msg' : 'error-msg'">
          <template v-if="r.testResult.ok">
            {{ t('settings.gateways.testOk', { name: r.testResult.name }) }}
          </template>
          <template v-else>
            {{ t('settings.gateways.testFail') }}: {{ r.testResult.error }}
          </template>
        </div>

        <!-- Last runtime error (when enabled & failing) -->
        <div v-if="r.enabled && r.lastError && !r.connected" class="error-msg">
          {{ t('settings.gateways.runtimeError') }}: {{ r.lastError }}
        </div>

        <div class="action-row">
          <button
            type="button"
            class="btn"
            :disabled="r.testing"
            @click="testConnection(id)"
          >{{ r.testing ? t('settings.gateways.testing') : t('settings.gateways.testConnection') }}</button>
          <button
            type="button"
            class="btn primary"
            :disabled="r.saving"
            @click="save(id)"
          >{{ r.saving ? t('settings.common.saving') : t('common.save') }}</button>
        </div>
        <div v-if="r.error" class="error-msg">{{ r.error }}</div>
        <div v-if="r.saved" class="success-msg">{{ t('settings.gateways.savedMsg') }}</div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.gw-sections { display: flex; flex-direction: column; gap: 14px; }
.intro { margin: 0 0 2px; }

.service-card {
  position: relative;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 18px 22px;
}
.hud-corner {
  position: absolute; right: 0; bottom: 0;
  width: 16px; height: 16px;
  border-right: 1.5px solid var(--accent);
  border-bottom: 1.5px solid var(--accent);
  opacity: 0.4;
  border-bottom-right-radius: var(--r-md);
  pointer-events: none;
}
.card-header { display: flex; align-items: flex-start; gap: 12px; margin-bottom: 14px; }
.card-title-block { flex: 1; min-width: 0; }
.card-title-block h2 { font-size: 15px; font-weight: 600; color: var(--text); margin: 0 0 2px; }
.card-title-block p { margin: 0; font-size: 12.5px; color: var(--text-dim); line-height: 1.5; }
.icon-circle {
  flex-shrink: 0;
  width: 36px; height: 36px;
  border-radius: var(--r-sm);
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  color: var(--accent);
  display: grid; place-items: center;
}

.status-chip {
  display: inline-flex; align-items: center; gap: 6px;
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.08em;
  color: var(--text-muted);
  margin-left: 8px;
  white-space: nowrap;
  align-self: flex-start;
}
.status-chip .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.status-chip.on { color: var(--success); }
.status-chip.warn { color: var(--warning); }
.status-chip.err { color: var(--danger); }
.status-chip.off { color: var(--text-muted); }

.card-body {
  display: flex; flex-direction: column; gap: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}
.field { display: flex; flex-direction: column; gap: 4px; }
.field-label {
  display: block;
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-muted);
}
.text-input {
  width: 100%;
  background: var(--bg-4);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  padding: 9px 12px;
  color: var(--text);
  font-family: inherit;
  font-size: 13px;
}
.text-input.mono { font-family: var(--font-mono); font-size: 12px; }
.text-input:focus {
  outline: none;
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-bg-strong);
}
select.text-input { cursor: pointer; }

.hint { margin: 0; font-size: 11.5px; color: var(--text-muted); line-height: 1.5; }
.hint a { color: var(--accent); }

.text-input.danger { border-color: var(--danger); }
.text-input.danger:focus { box-shadow: 0 0 0 3px var(--danger-bg); }
.allow-danger {
  margin: 0;
  padding: 7px 10px;
  background: var(--danger-bg);
  border: 1px solid var(--danger);
  border-radius: var(--r-sm);
  color: var(--danger);
  font-size: 11.5px;
  line-height: 1.45;
}

/* ── Toggle switch ────────────────────────────────────────────────── */
.switch-row { display: flex; align-items: center; gap: 10px; cursor: pointer; user-select: none; }
.switch-row input { position: absolute; opacity: 0; width: 0; height: 0; }
.switch-track {
  width: 38px; height: 22px; flex-shrink: 0;
  background: var(--bg-4);
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  position: relative;
  transition: background 0.15s, border-color 0.15s;
}
.switch-thumb {
  position: absolute; top: 2px; left: 2px;
  width: 16px; height: 16px; border-radius: 50%;
  background: var(--text-muted);
  transition: transform 0.15s, background 0.15s;
}
.switch-row input:checked + .switch-track { background: var(--primary); border-color: var(--primary); }
.switch-row input:checked + .switch-track .switch-thumb { transform: translateX(16px); background: #fff; }
.switch-row input:focus-visible + .switch-track { box-shadow: 0 0 0 3px var(--primary-bg-strong); }
.switch-label { font-size: 13px; color: var(--text); font-weight: 500; }

.action-row { display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; margin-top: 4px; }
.btn {
  padding: 8px 14px;
  font-family: inherit; font-size: 13px; font-weight: 500;
  border-radius: var(--r-md);
  border: 1px solid var(--border-strong);
  background: transparent; color: var(--text-dim);
  cursor: pointer; transition: all 0.15s;
}
.btn:hover:not([disabled]) { color: var(--text); background: rgba(255, 255, 255, 0.04); }
.btn.primary { background: var(--primary); color: #fff; border-color: var(--primary); }
.btn.primary:hover:not([disabled]) { background: var(--primary-active); border-color: var(--primary-active); }
.btn[disabled] { opacity: 0.5; cursor: not-allowed; }

.muted { color: var(--text-dim); font-size: 13px; line-height: 1.5; }
.error-msg {
  padding: 8px 12px;
  background: var(--danger-bg);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: var(--r-md);
  color: var(--danger);
  font-size: 13px;
}
.success-msg {
  padding: 8px 12px;
  background: var(--success-bg);
  border: 1px solid rgba(16, 185, 129, 0.3);
  border-radius: var(--r-md);
  color: var(--success);
  font-size: 13px;
}

@media (max-width: 768px) {
  .service-card { padding: 16px; }
  /* Header: icon + title on row 1, status chip drops to its own left-aligned
     row instead of being squeezed against the title. */
  .card-header { flex-wrap: wrap; gap: 10px; }
  .card-title-block { flex: 1 1 60%; min-width: 0; }
  .status-chip { margin-left: 0; width: 100%; }
  /* Full-width tap targets for the action buttons. */
  .action-row { gap: 8px; }
  .action-row > .btn { flex: 1; min-width: 0; }
  /* Token field is monospace + long — keep it readable, never overflow. */
  .text-input.mono { font-size: 11.5px; }
}
</style>
