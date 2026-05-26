<script setup>
/**
 * Settings → Voice tab.
 *
 * Renders three panels driven by GET /api/voice/engines so adding a new
 * TTS engine or STT param on the backend is purely additive — UI grows
 * automatically.
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
    const [reg, cur, sec] = await Promise.all([
      apiFetch('/api/voice/engines'),
      apiFetch('/api/voice/active'),
      apiFetch('/api/voice/secrets'),
    ])
    engines.value = reg
    active.value = cur
    secretsStatus.value = sec.engines || {}
    const checks = Object.entries(reg.tts || {})
      .filter(([_, spec]) => (spec.requires?.length || spec.secrets?.length))
      .map(async ([id]) => [id, await apiFetch(`/api/voice/requirements/${id}`)])
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
  if ((engines.value.tts[engine]?.requires?.length || engines.value.tts[engine]?.secrets?.length)) {
    requirements.value[engine] = await apiFetch(`/api/voice/requirements/${engine}`)
  }
}
async function clearSecret(engine, slot) {
  await apiFetch(`/api/voice/secrets/${engine}/${slot}`, { method: 'DELETE' })
  const sec = await apiFetch('/api/voice/secrets')
  secretsStatus.value = sec.engines || {}
  if ((engines.value.tts[engine]?.requires?.length || engines.value.tts[engine]?.secrets?.length)) {
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
</script>

<template>
  <div class="gen-sections">
    <p v-if="loading" class="loading">Loading…</p>
    <p v-else-if="error" class="error-msg">{{ error }}</p>

    <template v-else>
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
            <h2>Chat &amp; Notification Voice</h2>
            <p>
              Used by interactive chat replies and cron notifications. Default is Edge — free, no API key.
              Paid engines require a key (set inline below). Stories use a separate <strong>locked Edge</strong>
              provider so paid engines never burn long-form quota.
            </p>
          </div>
        </header>

        <div class="provider-grid">
          <button
            v-for="(spec, id) in engines.tts"
            :key="id"
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
        </div>

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
              Stories Voice
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
            <h2>Speech recognition</h2>
            <p>
              Pick a backend below. <strong>faster-whisper</strong> is multilingual (vi+en
              code-switching). <strong>Gipformer</strong> is Vietnamese-only but gives better
              accents on Vietnamese audio.
            </p>
          </div>
        </header>

        <div class="provider-grid">
          <button
            v-for="(spec, id) in engines.stt"
            :key="id"
            type="button"
            class="provider-card"
            :class="{ selected: sttBackendId === id }"
            @click="changeSttBackend(id)"
          >
            <span class="provider-title">{{ spec.label }}</span>
            <span class="provider-sub">{{ (spec.badges || []).join(' · ') || spec.description }}</span>
          </button>
        </div>

        <div v-if="sttSpec" class="provider-hint">
          <strong>{{ sttSpec.label }}</strong> — {{ sttSpec.description }}
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
/* Mirror SettingsLLM tokens so the Voice tab inherits the same look & feel. */
.gen-sections { display: flex; flex-direction: column; gap: 20px; }
.loading { color: var(--text-nav, #8b8fa3); }

.panel-card {
  background: var(--bg-card, #111318);
  border: 1px solid var(--border, #1e2030);
  border-radius: 12px;
  padding: 28px;
}
.panel-card > header {
  display: flex; gap: 14px; align-items: flex-start; margin-bottom: 24px;
}
.panel-card h2 {
  font-size: 16px; font-weight: 600;
  color: var(--text-primary, #f0f2f5);
  display: inline-flex; align-items: center; gap: 10px;
}
.panel-card header p {
  margin-top: 4px; font-size: 13px; line-height: 1.5;
  color: var(--text-nav, #8b8fa3);
}
.panel-card header strong { color: var(--text-primary, #f0f2f5); }
.icon-circle {
  flex-shrink: 0; width: 36px; height: 36px;
  border-radius: 10px;
  background: rgba(59, 130, 246, 0.12);
  color: var(--accent-blue, #3b82f6);
  display: grid; place-items: center;
}

.provider-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px; margin-bottom: 18px;
}
.provider-card {
  display: flex; flex-direction: column; gap: 4px;
  text-align: left; padding: 14px 16px;
  border: 1px solid var(--border, #1e2030);
  border-radius: 10px;
  background: var(--bg-input, #0f172a);
  color: var(--text-primary, #f0f2f5);
  cursor: pointer; transition: all 0.15s; font-family: inherit;
}
.provider-card:hover { border-color: rgba(59, 130, 246, 0.4); }
.provider-card.selected {
  border-color: var(--accent-blue, #3b82f6);
  background: rgba(59, 130, 246, 0.08);
}
.provider-title {
  font-size: 14px; font-weight: 600;
  display: flex; align-items: center; gap: 6px;
}
.provider-sub { font-size: 12px; color: var(--text-nav, #8b8fa3); }
.mini-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: #22c55e; display: inline-block;
}

.provider-hint {
  margin: 4px 0 18px; padding: 10px 12px;
  background: rgba(99, 102, 241, 0.08);
  border-left: 3px solid rgba(99, 102, 241, 0.6);
  border-radius: 4px;
  font-size: 13px; line-height: 1.45;
  color: var(--text-primary, #c8cad7);
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
}

.field { margin-top: 14px; }
.field > label {
  display: flex; gap: 8px; align-items: center;
  font-size: 12px; font-weight: 500;
  color: var(--text-nav, #8b8fa3);
  margin-bottom: 8px;
}
.key-status {
  font-weight: 500; padding: 1px 8px;
  border-radius: 999px; font-size: 11px;
  text-transform: lowercase;
}
.key-status.stored { background: rgba(34, 197, 94, 0.12); color: #22c55e; }
.key-status.missing { background: rgba(245, 158, 11, 0.12); color: #f59e0b; }
.range-value {
  font-weight: 400; font-size: 11px; color: var(--text-sub, #6b7280);
  margin-left: auto;
}

.text-input,
.pwd-input,
select.text-input {
  width: 100%;
  background: var(--bg-input, #0f172a);
  border: 1px solid var(--border-input, #1e2030);
  border-radius: 8px;
  padding: 11px 14px;
  color: var(--text-primary, #f0f2f5);
  font-family: inherit; font-size: 14px;
}
.pwd-input { padding-right: 40px; }
/* Native <select> chevron sits flush against the right edge across browsers
   (Chrome/Safari render their own arrow inside the padding box). Strip the
   native arrow and draw our own SVG so spacing is consistent. */
select.text-input {
  appearance: none;
  -webkit-appearance: none;
  -moz-appearance: none;
  padding-right: 36px;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238b8fa3' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'><polyline points='6 9 12 15 18 9'/></svg>");
  background-repeat: no-repeat;
  background-position: right 12px center;
  background-size: 12px 12px;
}
select.text-input:hover { border-color: var(--border-input-hover, #2a3556); }
.text-input:focus,
.pwd-input:focus,
select.text-input:focus {
  outline: none;
  border-color: var(--accent-blue, #3b82f6);
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
}

input[type="range"] {
  width: 100%; accent-color: var(--accent-blue, #3b82f6);
}
.toggle-row { display: flex; align-items: center; gap: 10px; color: var(--text-secondary, #c4c8d4); font-size: 13px; }

.input-group { position: relative; display: flex; align-items: stretch; gap: 8px; }
.icon-btn {
  position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
  background: transparent; border: none;
  color: var(--text-nav, #8b8fa3);
  cursor: pointer; padding: 6px; border-radius: 6px;
}
.icon-btn:hover { color: var(--text-primary, #f0f2f5); background: rgba(255, 255, 255, 0.04); }

.hint {
  display: block; margin-top: 6px;
  font-size: 12px; color: var(--text-sub, #555872);
  line-height: 1.4;
}
.hint code, p code {
  background: rgba(255, 255, 255, 0.05);
  padding: 1px 5px; border-radius: 4px; font-size: 11.5px;
}

.action-row {
  display: flex; justify-content: flex-end;
  gap: 10px; margin-top: 18px; align-items: center;
}
.btn {
  padding: 9px 16px;
  font-family: inherit; font-size: 13px; font-weight: 600;
  border-radius: 8px; border: 1px solid transparent;
  background: transparent; color: var(--text-nav, #8b8fa3);
  cursor: pointer; transition: all 0.15s;
}
.btn.small { padding: 7px 12px; font-size: 12px; }
.btn.ghost {
  border-color: var(--border, #1e2030); color: var(--text-secondary, #c4c8d4);
}
.btn.ghost:hover:not([disabled]) {
  color: var(--text-primary, #f0f2f5);
  background: rgba(255, 255, 255, 0.04);
}
.btn.primary {
  background: var(--accent-blue, #3b82f6); color: #ffffff;
  border-color: var(--accent-blue, #3b82f6);
}
.btn.primary:hover:not([disabled]) { background: #2f6cdc; border-color: #2f6cdc; }
.btn[disabled] { opacity: 0.5; cursor: not-allowed; }

.stt-result {
  flex: 1; font-size: 12px; color: var(--text-secondary, #c4c8d4);
  font-style: italic; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

.success-msg {
  font-size: 12px; color: #22c55e;
  background: rgba(34, 197, 94, 0.08);
  padding: 6px 12px; border-radius: 6px;
}
.error-msg {
  margin-top: 14px; padding: 10px 14px;
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: 8px; color: #ef4444; font-size: 13px;
}

.footer-row {
  display: flex; justify-content: flex-end; align-items: center;
  gap: 12px; margin-top: 4px;
}
</style>
