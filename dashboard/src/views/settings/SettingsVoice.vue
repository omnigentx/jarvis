<script setup>
/**
 * Settings → Voice tab.
 *
 * Renders the GET /api/voice/engines registry as generic forms so adding
 * a new TTS engine or STT backend on the backend is purely additive — the
 * UI auto-grows. Three independent panels:
 *
 *   - Chat & Notifications voice  → /api/voice/active.tts_chat
 *     User-configurable engine (Edge default, paid options opt-in).
 *   - Stories voice               → /api/voice/active.tts_stories
 *     Locked to Edge (see feedback_stories_tts_separation): stories burn
 *     TTS quota fast, so we never let a paid engine here even by mistake.
 *   - Speech recognition + wake word → /api/voice/active.stt
 *
 * Secrets (API keys for ElevenLabs / OpenAI / Azure) are managed inline
 * via /api/voice/secrets — set / clear, never displayed in plaintext.
 *
 * Preview buttons hit /api/voice/test/tts (chat/stories) and /test/stt
 * (warmup audio) so the user can validate without leaving the page.
 */
import { onMounted, ref, computed } from 'vue'
import { apiFetch, getApiKey } from '../../api.js'

const loading = ref(true)
const saving = ref(false)
const error = ref('')
const engines = ref({ tts: {}, stt: {} })
const active = ref({ tts_chat: null, tts_stories: null, stt: null })
const secretsStatus = ref({})  // { engine: { slot: bool } }
const requirements = ref({})    // { engine: { ok, missing_binaries, secrets_present } }
const previewing = ref(false)
const previewError = ref('')
const sttTestingResult = ref('')
const sttTesting = ref(false)
const voicesByEngine = ref({})  // refreshed catalog per engine

const chatEngineSpec = computed(() => {
  const id = active.value.tts_chat?.engine
  return id ? engines.value.tts?.[id] : null
})
const storiesEngineSpec = computed(() => engines.value.tts?.edge || null)
const sttSpec = computed(() => engines.value.stt?.faster_whisper || null)
const wakeWordBackends = computed(() => sttSpec.value?.wake_word_backends || {})
const activeWakeBackend = computed({
  get: () => active.value.stt?.wake_word?.backend || 'off',
  set: (v) => {
    if (!active.value.stt.wake_word) active.value.stt.wake_word = { backend: 'off', params: {} }
    active.value.stt.wake_word.backend = v
    // Reset wake_word.params so we don't smuggle stale keys when switching backends.
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
    // Eager-load requirements only for engines that declare any — Edge has
    // no deps so skipping it saves a round trip.
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

async function save() {
  saving.value = true
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
  } catch (e) {
    error.value = e?.message || String(e)
  } finally {
    saving.value = false
  }
}

async function preview(target, engineOverride = null) {
  previewError.value = ''
  previewing.value = true
  try {
    const body = {
      text: 'Xin chào, đây là bản xem trước giọng nói. Hello, this is a voice preview.',
    }
    if (engineOverride) {
      body.engine = engineOverride
      body.params = target.params || {}
    }
    const apiKey = getApiKey()
    const res = await fetch('/api/voice/test/tts', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
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
    previewing.value = false
  }
}

async function testStt() {
  sttTesting.value = true
  sttTestingResult.value = ''
  try {
    const r = await apiFetch('/api/voice/test/stt', { method: 'POST' })
    sttTestingResult.value = r.transcript || '(empty transcript)'
  } catch (e) {
    sttTestingResult.value = `error: ${e?.message || e}`
  } finally {
    sttTesting.value = false
  }
}

function changeChatEngine(id) {
  const spec = engines.value.tts?.[id]
  if (!spec) return
  // Re-seed params from spec defaults so the user starts from a valid baseline.
  const defaults = {}
  for (const p of spec.params || []) {
    if (p.default !== undefined) defaults[p.key] = p.default
  }
  active.value.tts_chat = { engine: id, params: defaults }
}

async function refreshVoices(engineId) {
  try {
    const r = await apiFetch(`/api/voice/engines/${engineId}/voices`)
    voicesByEngine.value[engineId] = r.voices || []
  } catch (e) {
    console.warn('[voice] refresh voices failed', e)
  }
}

const secretInput = ref({})  // { 'engine.slot': value }
async function setSecret(engine, slot) {
  const key = `${engine}.${slot}`
  const val = secretInput.value[key]
  if (!val) return
  await apiFetch(`/api/voice/secrets/${engine}/${slot}`, {
    method: 'POST',
    body: JSON.stringify({ value: val }),
  })
  secretInput.value[key] = ''
  // Refresh both the secrets-status map and the requirements card for this engine.
  const sec = await apiFetch('/api/voice/secrets')
  secretsStatus.value = sec.engines || {}
  requirements.value[engine] = await apiFetch(`/api/voice/requirements/${engine}`)
}
async function clearSecret(engine, slot) {
  await apiFetch(`/api/voice/secrets/${engine}/${slot}`, { method: 'DELETE' })
  const sec = await apiFetch('/api/voice/secrets')
  secretsStatus.value = sec.engines || {}
  requirements.value[engine] = await apiFetch(`/api/voice/requirements/${engine}`)
}

function reqBadgeFor(engineId) {
  const r = requirements.value[engineId]
  if (!r) return null
  if (r.ok) return { text: 'Ready', tone: 'ok' }
  const missing = []
  if (r.missing_binaries?.length) missing.push(`needs ${r.missing_binaries.join(', ')}`)
  const noKeys = Object.entries(r.secrets_present || {}).filter(([, v]) => !v).map(([k]) => k)
  if (noKeys.length) missing.push(`set ${noKeys.join(', ')}`)
  return { text: missing.join(' · '), tone: 'warn' }
}

function voiceOptionsFor(engineId, defaultOptions) {
  // Refreshed catalog overrides the static options if available.
  const live = voicesByEngine.value[engineId]
  if (live?.length) return live.map(v => v.id)
  return defaultOptions
}
</script>

<template>
  <div class="voice-settings">
    <p v-if="loading" class="loading">Loading…</p>
    <p v-else-if="error" class="error">{{ error }}</p>
    <template v-else>
      <!-- Chat / notifications voice -->
      <section class="card">
        <header>
          <h2>
            Chat &amp; Notification Voice
            <span v-if="chatEngineSpec && reqBadgeFor(active.tts_chat?.engine)" class="req-badge" :class="reqBadgeFor(active.tts_chat?.engine).tone">
              {{ reqBadgeFor(active.tts_chat?.engine).text }}
            </span>
          </h2>
          <p class="hint">
            Used by interactive chat replies and cron notifications. Default is Edge (free, no API key).
            Paid engines (ElevenLabs, OpenAI, Azure) require an API key — set it below.
          </p>
        </header>

        <div class="row">
          <label>Engine</label>
          <select :value="active.tts_chat?.engine" @change="changeChatEngine($event.target.value)">
            <option v-for="(spec, id) in engines.tts" :key="id" :value="id">
              {{ spec.label }}
              <template v-if="spec.badges?.length">— {{ spec.badges.join(', ') }}</template>
            </option>
          </select>
        </div>

        <div v-if="chatEngineSpec" class="params">
          <p class="desc">{{ chatEngineSpec.description }}</p>
          <div v-for="p in chatEngineSpec.params" :key="p.key" class="row">
            <label>{{ p.label }}</label>
            <div class="ctrl">
              <select
                v-if="p.type === 'select'"
                :value="paramValue(active.tts_chat, p.key, p.default)"
                @change="setParam(active.tts_chat, p.key, $event.target.value)"
              >
                <option v-for="o in voiceOptionsFor(active.tts_chat.engine, p.options || [])" :key="o" :value="o">{{ o }}</option>
              </select>
              <input
                v-else-if="p.type === 'number'"
                type="number"
                :value="paramValue(active.tts_chat, p.key, p.default)"
                :min="p.min"
                :max="p.max"
                :step="p.step || 1"
                @input="setParam(active.tts_chat, p.key, Number($event.target.value))"
              />
              <input
                v-else
                type="text"
                :value="paramValue(active.tts_chat, p.key, p.default)"
                :placeholder="p.help || ''"
                @input="setParam(active.tts_chat, p.key, $event.target.value)"
              />
              <button v-if="p.key === 'voice'" type="button" class="action small" @click="refreshVoices(active.tts_chat.engine)">Refresh from server</button>
            </div>
          </div>
          <div v-for="slot in (chatEngineSpec.secrets || [])" :key="slot" class="row">
            <label>Secret · {{ slot }}</label>
            <div class="ctrl">
              <input type="password" placeholder="paste new value" :value="secretInput[`${active.tts_chat.engine}.${slot}`] || ''" @input="secretInput[`${active.tts_chat.engine}.${slot}`] = $event.target.value" />
              <button type="button" class="action small" @click="setSecret(active.tts_chat.engine, slot)">Save</button>
              <button type="button" class="action small" @click="clearSecret(active.tts_chat.engine, slot)">Clear</button>
              <span class="status-pill" :class="(secretsStatus[active.tts_chat.engine] || {})[slot] ? 'ok' : 'warn'">
                {{ (secretsStatus[active.tts_chat.engine] || {})[slot] ? 'Set' : 'Not set' }}
              </span>
            </div>
          </div>
        </div>

        <div class="actions">
          <button type="button" :disabled="previewing" @click="preview(active.tts_chat, active.tts_chat?.engine)">
            {{ previewing ? 'Playing…' : 'Preview' }}
          </button>
        </div>
      </section>

      <!-- Stories voice (Edge locked) -->
      <section class="card">
        <header>
          <h2>Stories Voice <span class="locked-badge">Edge only</span></h2>
          <p class="hint">
            Long-form audiobook narration always uses the free Edge engine — paid engines burn quota fast on book-length text.
            Only voice id and rate are editable here.
          </p>
        </header>

        <div v-if="storiesEngineSpec" class="params">
          <div v-for="p in storiesEngineSpec.params" :key="p.key" class="row">
            <label>{{ p.label }}</label>
            <div class="ctrl">
              <select
                v-if="p.type === 'select'"
                :value="active.tts_stories?.[p.key] ?? p.default"
                @change="active.tts_stories = { ...active.tts_stories, [p.key]: $event.target.value }"
              >
                <option v-for="o in voiceOptionsFor('edge', p.options || [])" :key="o" :value="o">{{ o }}</option>
              </select>
              <input
                v-else
                type="text"
                :value="active.tts_stories?.[p.key] ?? p.default"
                :placeholder="p.help || ''"
                @input="active.tts_stories = { ...active.tts_stories, [p.key]: $event.target.value }"
              />
              <button v-if="p.key === 'voice'" type="button" class="action small" @click="refreshVoices('edge')">Refresh from server</button>
            </div>
          </div>
        </div>

        <div class="actions">
          <button type="button" :disabled="previewing" @click="preview({ params: active.tts_stories }, 'edge')">
            {{ previewing ? 'Playing…' : 'Preview' }}
          </button>
        </div>
      </section>

      <!-- STT + wake word -->
      <section class="card">
        <header>
          <h2>Speech recognition</h2>
          <p class="hint">
            Local faster-whisper for transcription. <code>language: auto</code> handles vi+en code-switching;
            pin to a specific language only if you hit detection mistakes in noisy rooms.
          </p>
        </header>

        <div v-if="sttSpec && active.stt" class="params">
          <div v-for="p in sttSpec.params" :key="p.key" class="row">
            <label>{{ p.label }}</label>
            <div class="ctrl">
              <select
                v-if="p.type === 'select'"
                :value="paramValue(active.stt, p.key, p.default)"
                @change="setParam(active.stt, p.key, $event.target.value)"
              >
                <option v-for="o in p.options" :key="o" :value="o">{{ o }}</option>
              </select>
              <input
                v-else-if="p.type === 'slider'"
                type="range"
                :min="p.min" :max="p.max" :step="p.step || 0.05"
                :value="paramValue(active.stt, p.key, p.default)"
                @input="setParam(active.stt, p.key, Number($event.target.value))"
              />
              <input
                v-else-if="p.type === 'toggle'"
                type="checkbox"
                :checked="paramValue(active.stt, p.key, p.default)"
                @change="setParam(active.stt, p.key, $event.target.checked)"
              />
              <input
                v-else-if="p.type === 'number'"
                type="number"
                :value="paramValue(active.stt, p.key, p.default)"
                :min="p.min" :max="p.max" :step="p.step || 0.1"
                @input="setParam(active.stt, p.key, Number($event.target.value))"
              />
              <input
                v-else
                type="text"
                :value="paramValue(active.stt, p.key, p.default)"
                @input="setParam(active.stt, p.key, $event.target.value)"
              />
              <span v-if="p.type === 'slider' || p.type === 'number'" class="range-value">{{ paramValue(active.stt, p.key, p.default) }}</span>
            </div>
          </div>

          <div class="row">
            <label>Wake word</label>
            <div class="ctrl">
              <select v-model="activeWakeBackend">
                <option v-for="(spec, id) in wakeWordBackends" :key="id" :value="id">{{ spec.label || id }}</option>
              </select>
            </div>
          </div>
          <template v-if="activeWakeBackend !== 'off'">
            <div v-for="p in (wakeWordBackends[activeWakeBackend]?.params || [])" :key="p.key" class="row">
              <label>{{ p.label }}</label>
              <div class="ctrl">
                <input
                  v-if="p.type === 'slider'"
                  type="range"
                  :min="p.min" :max="p.max" :step="p.step || 0.05"
                  :value="active.stt.wake_word.params[p.key] ?? p.default"
                  @input="active.stt.wake_word.params[p.key] = Number($event.target.value)"
                />
                <input
                  v-else
                  type="text"
                  :value="active.stt.wake_word.params[p.key] ?? p.default"
                  @input="active.stt.wake_word.params[p.key] = $event.target.value"
                />
                <span v-if="p.type === 'slider'" class="range-value">{{ active.stt.wake_word.params[p.key] ?? p.default }}</span>
              </div>
            </div>
          </template>
        </div>

        <div class="actions">
          <button type="button" :disabled="sttTesting" @click="testStt">{{ sttTesting ? 'Testing…' : 'Test STT (warmup audio)' }}</button>
          <span v-if="sttTestingResult" class="stt-result">"{{ sttTestingResult }}"</span>
        </div>
      </section>

      <p v-if="previewError" class="error">{{ previewError }}</p>

      <div class="footer">
        <button class="primary" type="button" :disabled="saving" @click="save">
          {{ saving ? 'Saving…' : 'Save changes' }}
        </button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.voice-settings { display: flex; flex-direction: column; gap: 24px; }
.card {
  background: var(--surface, #161826);
  border: 1px solid var(--border, #1e2030);
  border-radius: 10px;
  padding: 20px 22px;
  display: flex; flex-direction: column; gap: 14px;
}
.card header h2 { font-size: 17px; font-weight: 600; display: flex; align-items: center; gap: 10px; }
.hint { font-size: 13px; color: var(--text-nav, #8b8fa3); margin-top: 4px; }
.desc { font-size: 13px; color: var(--text-secondary, #c4c8d4); margin: 0 0 6px; }
.row { display: grid; grid-template-columns: 200px 1fr; align-items: center; gap: 12px; }
.ctrl { display: flex; align-items: center; gap: 8px; }
label { font-size: 13px; color: var(--text-secondary, #c4c8d4); }
select, input[type="text"], input[type="number"], input[type="password"] {
  background: var(--surface-2, #0e1020); border: 1px solid var(--border, #1e2030);
  color: var(--text-primary, #f0f2f5); padding: 8px 10px; border-radius: 6px; font: inherit;
  flex: 1; min-width: 0;
}
input[type="range"] { flex: 1; }
.range-value { font-size: 12px; color: var(--text-nav, #8b8fa3); min-width: 40px; text-align: right; }
.actions { display: flex; gap: 10px; padding-top: 4px; align-items: center; }
button {
  background: transparent; border: 1px solid var(--border, #1e2030);
  color: var(--text-primary, #f0f2f5); padding: 8px 14px; border-radius: 6px; cursor: pointer; font: inherit;
}
button:disabled { opacity: 0.5; cursor: progress; }
button.primary { background: var(--accent-blue, #3b82f6); border-color: var(--accent-blue, #3b82f6); }
button.action.small { padding: 5px 10px; font-size: 12px; }
.locked-badge {
  display: inline-block; margin-left: 8px; padding: 2px 8px;
  background: var(--surface-2, #0e1020); border: 1px solid var(--border, #1e2030);
  border-radius: 999px; font-size: 11px; color: var(--text-nav, #8b8fa3);
}
.req-badge { padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 500; }
.req-badge.ok { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }
.req-badge.warn { background: rgba(217, 119, 6, 0.15); color: #f59e0b; border: 1px solid rgba(217, 119, 6, 0.4); }
.status-pill { padding: 3px 9px; border-radius: 999px; font-size: 11px; }
.status-pill.ok { background: rgba(16, 185, 129, 0.15); color: #34d399; }
.status-pill.warn { background: rgba(148, 163, 184, 0.15); color: #94a3b8; }
.stt-result { font-size: 12px; color: var(--text-secondary, #c4c8d4); font-style: italic; }
.error { color: var(--accent-red, #ef4444); font-size: 13px; }
.loading { color: var(--text-nav, #8b8fa3); }
.footer { display: flex; justify-content: flex-end; }
code { background: var(--surface-2, #0e1020); padding: 1px 5px; border-radius: 4px; font-size: 12px; }
</style>
