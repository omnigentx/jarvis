<script setup>
/**
 * Settings → Voice tab.
 *
 * Renders the provider split (chat vs stories) up front, then registry
 * driven engine cards for chat TTS, the locked Edge stories engine, and
 * the STT + wake-word section. All data is wired through the same
 * /api/voice/* endpoints as before — only the styling is new.
 *
 *   - Chat & Notification voice  → /api/voice/active.tts_chat
 *   - Stories voice              → /api/voice/active.tts_stories  (locked Edge)
 *   - Speech recognition         → /api/voice/active.stt + wake word
 *
 * Secrets per engine (ElevenLabs / OpenAI / Azure API keys) are managed
 * inline via /api/voice/secrets — never displayed in plaintext, status
 * surfaced via "stored · hidden" / "not set" pills.
 */
import { onMounted, ref, computed } from 'vue'
import { apiFetch } from '../../api.js'

const loading = ref(true)
const saving = ref(false)
const saveSuccess = ref(false)
const error = ref('')
const engines = ref({ tts: {}, stt: {} })
const active = ref({ tts_chat: null, tts_stories: null, stt: null })
const secretsStatus = ref({})
const requirements = ref({})
// Feature-flag allowlist from GET /api/voice/backends. Drives the
// engine/backend dropdowns: disabled options are hidden so a user can't
// pick something the backend will RuntimeError on at save. See
// services/stt_backends/__init__.py + services/tts_backends/__init__.py.
const enabledFlags = ref({
  stt: { enabled: [], known: [] },
  tts: { enabled: [], known: [] },
})
function isSttBackendEnabled(id) {
  return enabledFlags.value.stt.enabled.includes(id)
}
function isTtsEngineEnabled(id) {
  return enabledFlags.value.tts.enabled.includes(id)
}

const previewing = ref(null)  // 'chat' | 'stories' | null
const previewError = ref('')
const sttTesting = ref(false)
const sttResult = ref('')
const voiceCatalog = ref({})

const chatEngineSpec = computed(() => {
  const id = active.value.tts_chat?.engine
  return id ? engines.value.tts?.[id] : null
})
const storiesEngineSpec = computed(() => engines.value.tts?.edge || null)
const sttBackendId = computed(() => active.value.stt?.backend || 'faster_whisper')
const sttSpec = computed(() => engines.value.stt?.[sttBackendId.value] || null)
const wakeBackends = computed(() => sttSpec.value?.wake_word_backends || {})

function changeSttBackend(id) {
  const spec = engines.value.stt?.[id]
  if (!spec) return
  const defaults = {}
  for (const p of spec.params || []) if (p.default !== undefined) defaults[p.key] = p.default
  active.value.stt = {
    backend: id,
    params: defaults,
    wake_word: { backend: 'off', params: {} },
  }
}
const activeWakeBackend = computed({
  get: () => active.value.stt?.wake_word?.backend || 'off',
  set: (v) => {
    if (!active.value.stt.wake_word) active.value.stt.wake_word = { backend: 'off', params: {} }
    active.value.stt.wake_word.backend = v
    active.value.stt.wake_word.params = {}
  },
})

onMounted(async () => {
  try {
    const [reg, cur, sec, flags] = await Promise.all([
      apiFetch('/api/voice/engines'),
      apiFetch('/api/voice/active'),
      apiFetch('/api/voice/secrets'),
      // Feature-flag allowlist (STT_BACKENDS_ENABLED / TTS_BACKENDS_ENABLED).
      // Drives dropdown filtering + fallback-on-disabled below.
      apiFetch('/api/voice/backends'),
    ])
    engines.value = reg
    active.value = cur
    secretsStatus.value = sec.engines || {}
    enabledFlags.value = flags || { stt: { enabled: [], known: [] }, tts: { enabled: [], known: [] } }
    // Fallback when the saved preference is now disabled (operator
    // changed env between sessions). Pick the first enabled option of
    // the same kind so the UI never shows an unselectable engine.
    const sttSel = active.value.stt?.backend
    if (sttSel && !enabledFlags.value.stt.enabled.includes(sttSel)) {
      const fallback = enabledFlags.value.stt.enabled[0]
      if (fallback) {
        console.warn(`[voice] STT backend "${sttSel}" disabled — falling back to "${fallback}"`)
        active.value.stt.backend = fallback
      }
    }
    const ttsSel = active.value.tts_chat?.engine
    if (ttsSel && !enabledFlags.value.tts.enabled.includes(ttsSel)) {
      const fallback = enabledFlags.value.tts.enabled[0]
      if (fallback) {
        console.warn(`[voice] TTS engine "${ttsSel}" disabled — falling back to "${fallback}"`)
        active.value.tts_chat.engine = fallback
      }
    }
    // Probe requirements for every engine that declares either binaries or
    // secrets — TTS and STT alike, so cloud STT backends (Soniox, ...) get
    // the same "ready / missing key" badge as paid TTS engines.
    const probeIds = new Set()
    for (const [id, spec] of Object.entries(reg.tts || {})) {
      if (spec.requires?.length || spec.secrets?.length) probeIds.add(id)
    }
    for (const [id, spec] of Object.entries(reg.stt || {})) {
      if (spec.requires?.length || spec.secrets?.length) probeIds.add(id)
    }
    const checks = Array.from(probeIds).map(
      async (id) => [id, await apiFetch(`/api/voice/requirements/${id}`)]
    )
    for (const [id, req] of await Promise.all(checks)) requirements.value[id] = req
  } catch (e) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
})

function paramValue(target, key, fallback) {
  return target?.params?.[key] ?? fallback
}
function setParam(target, key, value) {
  if (!target.params) target.params = {}
  target.params[key] = value
}
function changeChatEngine(id) {
  const spec = engines.value.tts?.[id]
  if (!spec) return
  const defaults = {}
  for (const p of spec.params || []) if (p.default !== undefined) defaults[p.key] = p.default
  active.value.tts_chat = { engine: id, params: defaults }
}

async function refreshVoices(engineId) {
  try {
    const r = await apiFetch(`/api/voice/engines/${engineId}/voices`)
    voiceCatalog.value[engineId] = r.voices || []
  } catch (e) {
    console.warn('[voice] refresh voices failed', e)
  }
}
function voiceOptionsFor(engineId, fallback) {
  const live = voiceCatalog.value[engineId]
  return live?.length ? live.map(v => v.id) : (fallback || [])
}

async function save() {
  saving.value = true
  saveSuccess.value = false
  error.value = ''
  try {
    await apiFetch('/api/voice/active', {
      method: 'POST',
      body: JSON.stringify({
        tts_chat: active.value.tts_chat,
        tts_stories: active.value.tts_stories,
        stt: active.value.stt,
      }),
    })
    saveSuccess.value = true
    setTimeout(() => (saveSuccess.value = false), 2000)
  } catch (e) {
    error.value = e?.message || String(e)
  } finally {
    saving.value = false
  }
}

async function preview(scope) {
  previewError.value = ''
  previewing.value = scope
  try {
    // TODO(i18n): VN+EN preview string intentionally kept — exercises multilingual TTS output
    const body = { text: 'Xin chào, đây là bản xem trước. Hello, this is a preview.' }
    if (scope === 'chat') {
      body.engine = active.value.tts_chat?.engine
      body.params = active.value.tts_chat?.params || {}
    } else if (scope === 'stories') {
      body.engine = 'edge'
      body.params = active.value.tts_stories || {}
    }
    // Session cookie + CSRF header — same auth shape as apiFetch (the
    // preview endpoint streams audio bytes so we hand-roll fetch
    // instead of using apiFetch, but the auth still rides on cookie).
    const csrf = (document.cookie.split('; ')
      .find((c) => c.startsWith('jarvis_csrf='))?.split('=', 2)[1]) || ''
    const res = await fetch('/api/voice/test/tts', {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
      },
      body: JSON.stringify(body),
    })
    if (!res.ok) throw new Error(`Preview failed: ${res.status}`)
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const audio = new Audio(url)
    audio.onended = () => URL.revokeObjectURL(url)
    await audio.play()
  } catch (e) {
    previewError.value = e?.message || String(e)
  } finally {
    previewing.value = null
  }
}

async function testStt() {
  sttTesting.value = true
  sttResult.value = ''
  try {
    const r = await apiFetch('/api/voice/test/stt', { method: 'POST' })
    sttResult.value = r.transcript || '(empty transcript)'
  } catch (e) {
    sttResult.value = `error: ${e?.message || e}`
  } finally {
    sttTesting.value = false
  }
}

const secretInput = ref({})
const secretReveal = ref({})
function _hasReqOrSecrets(engine) {
  const tts = engines.value.tts?.[engine]
  const stt = engines.value.stt?.[engine]
  return !!(tts?.requires?.length || tts?.secrets?.length
         || stt?.requires?.length || stt?.secrets?.length)
}
async function setSecret(engine, slot) {
  const key = `${engine}.${slot}`
  const val = secretInput.value[key]
  if (!val) return
  await apiFetch(`/api/voice/secrets/${engine}/${slot}`, {
    method: 'POST',
    body: JSON.stringify({ value: val }),
  })
  secretInput.value[key] = ''
  const sec = await apiFetch('/api/voice/secrets')
  secretsStatus.value = sec.engines || {}
  if (_hasReqOrSecrets(engine)) {
    requirements.value[engine] = await apiFetch(`/api/voice/requirements/${engine}`)
  }
}
async function clearSecret(engine, slot) {
  await apiFetch(`/api/voice/secrets/${engine}/${slot}`, { method: 'DELETE' })
  const sec = await apiFetch('/api/voice/secrets')
  secretsStatus.value = sec.engines || {}
  if (_hasReqOrSecrets(engine)) {
    requirements.value[engine] = await apiFetch(`/api/voice/requirements/${engine}`)
  }
}
function reqBadgeFor(engineId) {
  const r = requirements.value[engineId]
  if (!r) return null
  if (r.ok) return { text: 'ready', tone: 'ok' }
  const missing = []
  if (r.missing_binaries?.length) missing.push(`needs ${r.missing_binaries.join(', ')}`)
  const noKeys = Object.entries(r.secrets_present || {}).filter(([, v]) => !v).map(([k]) => k)
  if (noKeys.length) missing.push(`set ${noKeys.join(', ')}`)
  return { text: missing.join(' · '), tone: 'warn' }
}
function badgesFor(spec) {
  return spec?.badges || []
}

// ─── Provider split summary (top-of-page glance card) ──────────────────
// Surfaces the two TTS provider slots side-by-side so the user can see at
// a glance what chat vs stories will use, before scrolling down into the
// per-engine knobs. Stories is hard-locked to Edge to protect paid quota.
const chatProviderLabel = computed(() => {
  const id = active.value.tts_chat?.engine
  if (!id) return '—'
  return (engines.value.tts?.[id]?.label || id).toUpperCase()
})
</script>

<template>
  <div class="voice-sections">
    <p v-if="loading" class="loading">Loading…</p>
    <p v-else-if="error" class="error-msg">{{ error }}</p>

    <template v-else>
      <!-- ─── Provider split summary ─────────────────────────────────── -->
      <section class="hud-card provider-split">
        <div class="hud-corner" />
        <div class="mono-label split-label">● PROVIDER SPLIT</div>
        <div class="split-grid">
          <div class="split-cell">
            <div class="split-cell-head">
              <span class="split-cell-key">tts_chat_provider</span>
              <span class="chip chip-success">{{ chatProviderLabel }}</span>
            </div>
            <div class="mono-label split-cell-sub">/CHAT · /CHAT-STREAM · /WS/VOICE · CRON</div>
          </div>
          <div class="split-cell">
            <div class="split-cell-head">
              <span class="split-cell-key">tts_stories_provider</span>
              <span class="chip chip-muted">🔒 LOCKED EDGE</span>
            </div>
            <div class="mono-label split-cell-sub">STORIES · LIBRARY · PREGEN</div>
          </div>
        </div>
      </section>

      <!-- ─── Chat & Notification voice ─────────────────────────────────── -->
      <section class="panel-card">
        <header>
          <div class="icon-circle">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M3 18v-6a9 9 0 0 1 18 0v6" />
              <path d="M21 19a2 2 0 0 1-2 2h-1v-6h3z" />
              <path d="M3 19a2 2 0 0 0 2 2h1v-6H3z" />
            </svg>
          </div>
          <div>
            <h2><span class="mono-num">01 /</span> Chat &amp; Notification Voice</h2>
            <p>
              Used by interactive chat replies and cron notifications. Default is Edge — free, no API key.
              Paid engines require a key (set inline below). Stories use a separate <strong>locked Edge</strong>
              provider so paid engines never burn long-form quota.
            </p>
          </div>
        </header>

        <div class="provider-grid">
          <template v-for="(spec, id) in engines.tts" :key="id">
            <button
              v-if="isTtsEngineEnabled(id)"
              type="button"
              class="provider-card"
              :class="{ selected: active.tts_chat?.engine === id }"
              @click="changeChatEngine(id)"
            >
              <span class="provider-title">
                {{ spec.label }}
                <span v-if="(secretsStatus[id]?.api_key) || (spec.secrets?.length === 0)" class="mini-dot" title="Ready"></span>
              </span>
              <span class="provider-sub">{{ badgesFor(spec).join(' · ') || spec.description }}</span>
            </button>
          </template>
        </div>
        <p v-if="enabledFlags.tts.known.length > enabledFlags.tts.enabled.length" class="provider-flag-hint">
          {{ enabledFlags.tts.known.length - enabledFlags.tts.enabled.length }} TTS engine(s) hidden by
          <code>TTS_BACKENDS_ENABLED</code> in <code>backend/.env</code>.
        </p>

        <div v-if="chatEngineSpec" class="provider-hint">
          <strong>{{ chatEngineSpec.label }}</strong> — {{ chatEngineSpec.description }}
          <span v-if="reqBadgeFor(active.tts_chat?.engine)" class="key-status" :class="reqBadgeFor(active.tts_chat?.engine).tone === 'ok' ? 'stored' : 'missing'">
            {{ reqBadgeFor(active.tts_chat?.engine).text }}
          </span>
        </div>

        <div v-for="p in (chatEngineSpec?.params || [])" :key="p.key" class="field">
          <label :for="`chat-${p.key}`">{{ p.label }}</label>
          <div v-if="p.key === 'voice'" class="input-group">
            <select :id="`chat-${p.key}`" class="text-input" :value="paramValue(active.tts_chat, p.key, p.default)" @change="setParam(active.tts_chat, p.key, $event.target.value)">
              <option v-for="o in voiceOptionsFor(active.tts_chat.engine, p.options || [p.default])" :key="o" :value="o">{{ o }}</option>
            </select>
            <button type="button" class="btn ghost small" @click="refreshVoices(active.tts_chat.engine)">Refresh from server</button>
          </div>
          <select v-else-if="p.type === 'select'" :id="`chat-${p.key}`" class="text-input" :value="paramValue(active.tts_chat, p.key, p.default)" @change="setParam(active.tts_chat, p.key, $event.target.value)">
            <option v-for="o in (p.options || [])" :key="o" :value="o">{{ o }}</option>
          </select>
          <input
            v-else-if="p.type === 'number'"
            :id="`chat-${p.key}`"
            class="text-input"
            type="number"
            :value="paramValue(active.tts_chat, p.key, p.default)"
            :min="p.min" :max="p.max" :step="p.step || 1"
            @input="setParam(active.tts_chat, p.key, Number($event.target.value))"
          />
          <input
            v-else
            :id="`chat-${p.key}`"
            class="text-input"
            type="text"
            :value="paramValue(active.tts_chat, p.key, p.default)"
            :placeholder="p.help || ''"
            @input="setParam(active.tts_chat, p.key, $event.target.value)"
          />
          <span v-if="p.help" class="hint">{{ p.help }}</span>
        </div>

        <!-- Per-engine secrets -->
        <div v-for="slot in (chatEngineSpec?.secrets || [])" :key="slot" class="field">
          <label :for="`secret-${active.tts_chat.engine}-${slot}`">
            Secret · {{ slot }}
            <span v-if="(secretsStatus[active.tts_chat.engine] || {})[slot]" class="key-status stored">stored · hidden</span>
            <span v-else class="key-status missing">not set</span>
          </label>
          <div class="input-group">
            <input
              :id="`secret-${active.tts_chat.engine}-${slot}`"
              class="pwd-input"
              :type="secretReveal[`${active.tts_chat.engine}.${slot}`] ? 'text' : 'password'"
              autocomplete="off"
              :placeholder="(secretsStatus[active.tts_chat.engine] || {})[slot] ? 'Leave blank to keep current' : 'Paste API key'"
              :value="secretInput[`${active.tts_chat.engine}.${slot}`] || ''"
              @input="secretInput[`${active.tts_chat.engine}.${slot}`] = $event.target.value"
            />
            <button type="button" class="icon-btn" @click="secretReveal[`${active.tts_chat.engine}.${slot}`] = !secretReveal[`${active.tts_chat.engine}.${slot}`]">
              <svg v-if="secretReveal[`${active.tts_chat.engine}.${slot}`]" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                <line x1="1" y1="1" x2="23" y2="23" />
              </svg>
              <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                <circle cx="12" cy="12" r="3" />
              </svg>
            </button>
          </div>
          <div class="action-row" style="margin-top: 8px;">
            <button type="button" class="btn ghost" :disabled="!(secretsStatus[active.tts_chat.engine] || {})[slot]" @click="clearSecret(active.tts_chat.engine, slot)">Clear</button>
            <button type="button" class="btn primary" :disabled="!secretInput[`${active.tts_chat.engine}.${slot}`]" @click="setSecret(active.tts_chat.engine, slot)">Save key</button>
          </div>
        </div>

        <div class="action-row">
          <button type="button" class="btn ghost" :disabled="previewing === 'chat'" @click="preview('chat')">
            {{ previewing === 'chat' ? 'Playing…' : 'Preview' }}
          </button>
        </div>
      </section>

      <!-- ─── Stories voice (locked Edge) ──────────────────────────────── -->
      <section class="panel-card">
        <header>
          <div class="icon-circle">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" />
              <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
            </svg>
          </div>
          <div>
            <h2>
              <span class="mono-num">02 /</span> Stories Voice
              <span class="key-status stored">Edge only</span>
            </h2>
            <p>
              Long-form audiobook narration. Always uses the free Edge engine — paid engines burn
              quota fast on book-length text. Only voice id and rate are editable.
            </p>
          </div>
        </header>

        <div v-for="p in (storiesEngineSpec?.params || [])" :key="p.key" class="field">
          <label :for="`stories-${p.key}`">{{ p.label }}</label>
          <div v-if="p.key === 'voice'" class="input-group">
            <select :id="`stories-${p.key}`" class="text-input" :value="active.tts_stories?.[p.key] ?? p.default" @change="active.tts_stories = { ...active.tts_stories, [p.key]: $event.target.value }">
              <option v-for="o in voiceOptionsFor('edge', p.options || [p.default])" :key="o" :value="o">{{ o }}</option>
            </select>
            <button type="button" class="btn ghost small" @click="refreshVoices('edge')">Refresh from server</button>
          </div>
          <input
            v-else
            :id="`stories-${p.key}`"
            class="text-input"
            type="text"
            :value="active.tts_stories?.[p.key] ?? p.default"
            :placeholder="p.help || ''"
            @input="active.tts_stories = { ...active.tts_stories, [p.key]: $event.target.value }"
          />
          <span v-if="p.help" class="hint">{{ p.help }}</span>
        </div>

        <div class="action-row">
          <button type="button" class="btn ghost" :disabled="previewing === 'stories'" @click="preview('stories')">
            {{ previewing === 'stories' ? 'Playing…' : 'Preview' }}
          </button>
        </div>
      </section>

      <!-- ─── STT + wake word ──────────────────────────────────────────── -->
      <section class="panel-card">
        <header>
          <div class="icon-circle">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
              <path d="M19 10v1a7 7 0 0 1-14 0v-1" />
              <line x1="12" y1="18" x2="12" y2="22" />
              <line x1="8" y1="22" x2="16" y2="22" />
            </svg>
          </div>
          <div>
            <h2><span class="mono-num">03 /</span> Speech recognition</h2>
            <p>
              Pick a backend below. <strong>faster-whisper</strong> is multilingual (vi+en
              code-switching). <strong>Gipformer</strong> is Vietnamese-only but gives better
              accents on Vietnamese audio.
            </p>
          </div>
        </header>

        <div class="provider-grid">
          <template v-for="(spec, id) in engines.stt" :key="id">
            <button
              v-if="isSttBackendEnabled(id)"
              type="button"
              class="provider-card"
              :class="{ selected: sttBackendId === id }"
              @click="changeSttBackend(id)"
            >
              <span class="provider-title">{{ spec.label }}</span>
              <span class="provider-sub">{{ (spec.badges || []).join(' · ') || spec.description }}</span>
            </button>
          </template>
        </div>
        <p v-if="enabledFlags.stt.known.length > enabledFlags.stt.enabled.length" class="provider-flag-hint">
          {{ enabledFlags.stt.known.length - enabledFlags.stt.enabled.length }} STT backend(s) hidden by
          <code>STT_BACKENDS_ENABLED</code> in <code>backend/.env</code>.
        </p>

        <div v-if="sttSpec" class="provider-hint">
          <strong>{{ sttSpec.label }}</strong> — {{ sttSpec.description }}
          <span v-if="reqBadgeFor(sttBackendId)" class="key-status" :class="reqBadgeFor(sttBackendId).tone === 'ok' ? 'stored' : 'missing'">
            {{ reqBadgeFor(sttBackendId).text }}
          </span>
          <span v-if="sttSpec.language_locked" class="key-status stored">
            language locked: {{ sttSpec.language_locked }}
          </span>
        </div>

        <template v-if="sttSpec && active.stt">
          <div v-for="p in (sttSpec.params || [])" :key="p.key" class="field">
            <label :for="`stt-${p.key}`">
              {{ p.label }}
              <span v-if="p.type === 'slider' || p.type === 'number'" class="range-value">{{ paramValue(active.stt, p.key, p.default) }}</span>
            </label>
            <select v-if="p.type === 'select'" :id="`stt-${p.key}`" class="text-input" :value="paramValue(active.stt, p.key, p.default)" @change="setParam(active.stt, p.key, $event.target.value)">
              <option v-for="o in (p.options || [])" :key="o" :value="o">{{ o }}</option>
            </select>
            <input v-else-if="p.type === 'slider'" :id="`stt-${p.key}`" type="range"
              :min="p.min" :max="p.max" :step="p.step || 0.05"
              :value="paramValue(active.stt, p.key, p.default)"
              @input="setParam(active.stt, p.key, Number($event.target.value))"
            />
            <label v-else-if="p.type === 'toggle'" class="toggle-row">
              <input type="checkbox" :checked="paramValue(active.stt, p.key, p.default)" @change="setParam(active.stt, p.key, $event.target.checked)" />
              <span>{{ p.help || 'Enabled' }}</span>
            </label>
            <input v-else-if="p.type === 'number'" :id="`stt-${p.key}`" class="text-input" type="number"
              :value="paramValue(active.stt, p.key, p.default)"
              :min="p.min" :max="p.max" :step="p.step || 0.1"
              @input="setParam(active.stt, p.key, Number($event.target.value))"
            />
            <input v-else :id="`stt-${p.key}`" class="text-input" type="text"
              :value="paramValue(active.stt, p.key, p.default)"
              @input="setParam(active.stt, p.key, $event.target.value)"
            />
            <span v-if="p.help && p.type !== 'toggle'" class="hint">{{ p.help }}</span>
          </div>

          <div class="field">
            <label for="wake-backend">Wake word</label>
            <select id="wake-backend" class="text-input" v-model="activeWakeBackend">
              <option v-for="(spec, id) in wakeBackends" :key="id" :value="id">{{ spec.label || id }}</option>
            </select>
            <span class="hint">Disabled by default — turn on to require "jarvis" before transcription kicks in.</span>
          </div>

          <template v-if="activeWakeBackend !== 'off'">
            <div v-for="p in (wakeBackends[activeWakeBackend]?.params || [])" :key="p.key" class="field">
              <label :for="`wake-${p.key}`">
                {{ p.label }}
                <span v-if="p.type === 'slider'" class="range-value">{{ active.stt.wake_word.params[p.key] ?? p.default }}</span>
              </label>
              <input v-if="p.type === 'slider'" :id="`wake-${p.key}`" type="range"
                :min="p.min" :max="p.max" :step="p.step || 0.05"
                :value="active.stt.wake_word.params[p.key] ?? p.default"
                @input="active.stt.wake_word.params[p.key] = Number($event.target.value)"
              />
              <input v-else :id="`wake-${p.key}`" class="text-input" type="text"
                :value="active.stt.wake_word.params[p.key] ?? p.default"
                :placeholder="p.help || ''"
                @input="active.stt.wake_word.params[p.key] = $event.target.value"
              />
            </div>
          </template>

          <!-- Per-backend secrets (cloud STT API keys, e.g. Soniox). The
               TTS panel renders the same shape; UI is duplicated rather
               than extracted because the param/secret form is the only
               non-trivial bit and refactoring two of them under a single
               component is more churn than it's worth right now. -->
          <div v-for="slot in (sttSpec?.secrets || [])" :key="slot" class="field">
            <label :for="`stt-secret-${sttBackendId}-${slot}`">
              Secret · {{ slot }}
              <span v-if="(secretsStatus[sttBackendId] || {})[slot]" class="key-status stored">stored · hidden</span>
              <span v-else class="key-status missing">not set</span>
            </label>
            <div class="input-group">
              <input
                :id="`stt-secret-${sttBackendId}-${slot}`"
                class="pwd-input"
                :type="secretReveal[`${sttBackendId}.${slot}`] ? 'text' : 'password'"
                autocomplete="off"
                :placeholder="(secretsStatus[sttBackendId] || {})[slot] ? 'Leave blank to keep current' : 'Paste API key'"
                :value="secretInput[`${sttBackendId}.${slot}`] || ''"
                @input="secretInput[`${sttBackendId}.${slot}`] = $event.target.value"
              />
              <button type="button" class="icon-btn" @click="secretReveal[`${sttBackendId}.${slot}`] = !secretReveal[`${sttBackendId}.${slot}`]">
                <svg v-if="secretReveal[`${sttBackendId}.${slot}`]" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                  <line x1="1" y1="1" x2="23" y2="23" />
                </svg>
                <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                  <circle cx="12" cy="12" r="3" />
                </svg>
              </button>
            </div>
            <div class="action-row" style="margin-top: 8px;">
              <button type="button" class="btn ghost" :disabled="!(secretsStatus[sttBackendId] || {})[slot]" @click="clearSecret(sttBackendId, slot)">Clear</button>
              <button type="button" class="btn primary" :disabled="!secretInput[`${sttBackendId}.${slot}`]" @click="setSecret(sttBackendId, slot)">Save key</button>
            </div>
          </div>
        </template>

        <div class="action-row">
          <span v-if="sttResult" class="stt-result" :title="sttResult">"{{ sttResult }}"</span>
          <button type="button" class="btn ghost" :disabled="sttTesting" @click="testStt">
            {{ sttTesting ? 'Testing…' : 'Test STT (warmup audio)' }}
          </button>
        </div>
      </section>

      <p v-if="previewError" class="error-msg">{{ previewError }}</p>

      <div class="footer-row">
        <span v-if="saveSuccess" class="success-msg">Saved · live now (no restart needed)</span>
        <button class="btn primary" type="button" :disabled="saving" @click="save">
          {{ saving ? 'Saving…' : 'Save changes' }}
        </button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.voice-sections { display: flex; flex-direction: column; gap: 18px; }
.loading { color: var(--text-muted); font-size: 13px; }

/* ── Provider split (HUD glance card) ─────────────────────────────── */
.hud-card {
  position: relative;
  padding: 16px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
}
.hud-corner {
  position: absolute; right: 0; bottom: 0;
  width: 18px; height: 18px;
  border-right: 1.5px solid var(--accent);
  border-bottom: 1.5px solid var(--accent);
  opacity: 0.55;
  border-bottom-right-radius: var(--r-md);
}
.mono-label {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-muted);
}
.split-label { color: var(--accent); margin-bottom: 10px; }
.split-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.split-cell {
  padding: 12px;
  background: var(--bg-1);
  border-radius: var(--r-sm);
  border: 1px solid var(--border);
}
.split-cell-head {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 6px;
  gap: 8px;
  flex-wrap: wrap;
}
.split-cell-key {
  font-size: 12.5px; font-weight: 500; color: var(--text);
  font-family: var(--font-mono);
}
.split-cell-sub { font-size: 9.5px; }
.chip {
  height: 20px; padding: 0 8px;
  display: inline-flex; align-items: center;
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.06em;
  border-radius: 999px;
}
.chip-success { background: var(--success-bg); color: var(--success); }
.chip-muted   { background: rgba(255,255,255,0.05); color: var(--text-muted); }

/* ── Panel card ───────────────────────────────────────────────────── */
.panel-card {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 24px 26px;
}
.panel-card > header {
  display: flex; gap: 14px; align-items: flex-start;
  margin-bottom: 18px;
}
.panel-card h2 {
  font-size: 15px; font-weight: 600;
  color: var(--text);
  display: inline-flex; align-items: center; gap: 10px;
}
.mono-num {
  font-family: var(--font-mono);
  color: var(--primary-hover);
  font-size: 11.5px;
  font-weight: 500;
}
.panel-card header p {
  margin-top: 4px; font-size: 13px; line-height: 1.5;
  color: var(--text-dim);
}
.panel-card header strong { color: var(--text); }
.icon-circle {
  flex-shrink: 0; width: 36px; height: 36px;
  border-radius: var(--r-md);
  background: var(--primary-bg);
  color: var(--primary-hover);
  display: grid; place-items: center;
}

/* ── Provider cards ───────────────────────────────────────────────── */
.provider-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px; margin-bottom: 18px;
}
.provider-card {
  display: flex; flex-direction: column; gap: 4px;
  text-align: left; padding: 12px 14px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-2);
  color: var(--text);
  cursor: pointer; transition: all 0.15s; font-family: inherit;
}
.provider-card:hover { border-color: var(--primary); }
.provider-card.selected {
  border-color: var(--primary);
  background: var(--primary-bg);
}
.provider-title {
  font-size: 13px; font-weight: 500;
  display: flex; align-items: center; gap: 6px;
}
.provider-sub {
  font-size: 9.5px;
  color: var(--text-muted);
  font-family: var(--font-mono);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.mini-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--success); display: inline-block;
}

.provider-hint {
  margin: 4px 0 14px;
  padding: 10px 12px;
  background: var(--primary-bg);
  border-left: 3px solid var(--primary);
  border-radius: var(--r-sm);
  font-size: 13px; line-height: 1.45;
  color: var(--text);
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
}

.provider-flag-hint {
  margin: 6px 0 0;
  font-size: 11.5px;
  color: var(--text-muted);
  line-height: 1.45;
}
.provider-flag-hint code {
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 1px 5px;
  background: var(--bg-3);
  border-radius: var(--r-xs);
  color: var(--text-dim);
}

/* ── Form rows ────────────────────────────────────────────────────── */
.field { margin-top: 14px; }
.field > label {
  display: flex; gap: 8px; align-items: center;
  font-family: var(--font-mono);
  font-size: 10px; font-weight: 500;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-muted);
  margin-bottom: 6px;
}
.key-status {
  font-family: var(--font-mono);
  font-weight: 500; padding: 1px 8px;
  border-radius: 999px; font-size: 10px;
  letter-spacing: 0.04em;
  text-transform: lowercase;
}
.key-status.stored { background: var(--success-bg); color: var(--success); }
.key-status.missing { background: var(--warning-bg); color: var(--warning); }
.range-value {
  font-weight: 400; font-size: 11px; color: var(--text-subtle);
  margin-left: auto; font-family: var(--font-mono);
}

.text-input,
.pwd-input,
select.text-input {
  width: 100%;
  background: var(--bg-4);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  padding: 10px 14px;
  color: var(--text);
  font-family: inherit; font-size: 13px;
}
.pwd-input { padding-right: 40px; }
select.text-input {
  appearance: none;
  -webkit-appearance: none;
  -moz-appearance: none;
  padding-right: 36px;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%237B8094' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'><polyline points='6 9 12 15 18 9'/></svg>");
  background-repeat: no-repeat;
  background-position: right 12px center;
  background-size: 12px 12px;
}
.text-input:focus,
.pwd-input:focus,
select.text-input:focus {
  outline: none;
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-bg-strong);
}

input[type="range"] {
  width: 100%; accent-color: var(--primary);
}
.toggle-row { display: flex; align-items: center; gap: 10px; color: var(--text-dim); font-size: 13px; }

.input-group { position: relative; display: flex; align-items: stretch; gap: 8px; }
.icon-btn {
  position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
  background: transparent; border: none;
  color: var(--text-muted);
  cursor: pointer; padding: 6px; border-radius: var(--r-sm);
}
.icon-btn:hover { color: var(--text); background: rgba(255, 255, 255, 0.04); }

.hint {
  display: block; margin-top: 6px;
  font-size: 12px; color: var(--text-subtle);
  line-height: 1.4;
}
.hint code, p code {
  background: rgba(255, 255, 255, 0.05);
  padding: 1px 5px; border-radius: 4px; font-size: 11.5px;
}

/* ── Buttons ──────────────────────────────────────────────────────── */
.action-row {
  display: flex; justify-content: flex-end;
  gap: 10px; margin-top: 16px; align-items: center;
}
.btn {
  padding: 9px 16px;
  font-family: inherit; font-size: 13px; font-weight: 500;
  border-radius: var(--r-md); border: 1px solid transparent;
  background: transparent; color: var(--text-dim);
  cursor: pointer; transition: all 0.15s;
}
.btn.small { padding: 7px 12px; font-size: 12px; }
.btn.ghost {
  border-color: var(--border-strong); color: var(--text-dim);
}
.btn.ghost:hover:not([disabled]) {
  color: var(--text);
  background: rgba(255, 255, 255, 0.04);
}
.btn.primary {
  background: var(--primary); color: #ffffff;
  border-color: var(--primary);
}
.btn.primary:hover:not([disabled]) { background: var(--primary-active); border-color: var(--primary-active); }
.btn[disabled] { opacity: 0.5; cursor: not-allowed; }

.stt-result {
  flex: 1; font-size: 12px; color: var(--text-dim);
  font-style: italic; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

.success-msg {
  font-size: 12px; color: var(--success);
  background: var(--success-bg);
  padding: 6px 12px; border-radius: var(--r-sm);
}
.error-msg {
  margin-top: 14px; padding: 10px 14px;
  background: var(--danger-bg);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: var(--r-md); color: var(--danger); font-size: 13px;
}

.footer-row {
  display: flex; justify-content: flex-end; align-items: center;
  gap: 12px;
  /* Sticky so "Save changes" is always reachable — without this, a long
     panel (engine picker → params → secret form) pushes the button below
     the viewport on first visit, leaving the user staring at form fields
     with no obvious way to commit them. The sticky band rides the bottom
     of the scroll container. Faded top edge so the panel content below
     it doesn't visibly cut off through transparent space. */
  position: sticky;
  /* iOS Safari's soft keyboard sits on top of the visual viewport and
     shrinks the layout viewport; ``bottom: 0`` alone collides with the
     keyboard. ``env(safe-area-inset-bottom)`` adds the home-indicator
     gutter; the padding-bottom hardens the gap on iOS without
     re-measuring on every keystroke. Desktop browsers ignore the env()
     fallback (resolves to 0) so this is iOS-only insurance. */
  bottom: 0;
  padding-bottom: max(4px, env(safe-area-inset-bottom));
  margin-top: 8px;
  padding-top: 14px;
  background: linear-gradient(180deg, transparent 0%, var(--bg-1, #0b1020) 35%, var(--bg-1, #0b1020) 100%);
  /* Bumped from 5 → 30 so native popovers that float over the panel
     (voice-name <select> dropdown, reveal-eye tooltip, paid-engine
     hint pill) don't render UNDER the sticky band. 30 is the same tier
     SettingsGeneral.vue uses for its own sticky save row — keeps the
     two settings tabs visually consistent. */
  z-index: 30;
}

@media (max-width: 768px) {
  .split-grid { grid-template-columns: 1fr; }
  .panel-card { padding: 18px 16px; }
  /* Split-cell head: tts_chat_provider mono key + chip "MICROSOFT EDGE
     TTS" together overflow a ~257px cell. Stack — key full-width row 1,
     chip left-aligned row 2 — and let the chip itself wrap its label so
     even longer engine names (e.g. "VIETTEL VPA · ENTERPRISE") stay
     readable. */
  .split-cell-head { flex-direction: column; align-items: flex-start; gap: 6px; }
  .split-cell-key { word-break: break-all; }
  .split-cell-head .chip { height: auto; padding: 3px 8px; white-space: normal; line-height: 1.3; }
  .hud-card { padding: 14px; }
  /* Provider grid stack: same fragility as LLM at narrow widths. */
  .provider-grid { grid-template-columns: 1fr; }
  /* Input + "Refresh from server" / Test STT row was overflowing at
     <380px because nothing wrapped. Wrap + grow button to full width
     so primary affordance stays reachable. */
  .input-group { flex-wrap: wrap; gap: 8px; }
  .input-group > .btn { flex: 1; min-width: 0; }
  /* STT result: ellipsis hid the transcript on mobile. Word-break wraps
     so the user can read what they actually said. */
  .stt-result {
    white-space: normal;
    word-break: break-word;
    overflow: visible;
    text-overflow: clip;
  }
  .action-row { flex-wrap: wrap; gap: 8px; }
  .action-row > .btn { flex: 1; min-width: 0; }
  /* Reveal-password icon button to 40px touch floor. */
  .icon-btn { width: 40px; height: 40px; }
}
</style>
