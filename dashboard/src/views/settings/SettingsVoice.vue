<script setup>
/**
 * Settings → Voice tab.
 *
 * Renders the registry returned by GET /api/voice/engines as a generic
 * form so that adding a new TTS engine on the backend is purely additive
 * (no UI change needed). Two independent panels:
 *
 *   - Chat & Notifications voice  → /api/voice/active.tts_chat
 *     User-configurable engine (Edge default, paid options opt-in).
 *   - Stories voice               → /api/voice/active.tts_stories
 *     Locked to Edge — only voice id + rate are editable. The intent is
 *     spelled out in feedback_stories_tts_separation: stories burn TTS
 *     quota fast, so we never let a paid engine here even by mistake.
 *
 * The "Preview" button POSTs to /api/voice/test/tts and plays the MP3
 * back through a transient <audio> element so users hear the engine
 * before saving.
 */
import { onMounted, ref, computed } from 'vue'
import { apiFetch, getApiKey } from '../../api.js'

const loading = ref(true)
const saving = ref(false)
const error = ref('')
const engines = ref({ tts: {}, stt: {} })
const active = ref({ tts_chat: null, tts_stories: null, stt: null })
const previewing = ref(false)
const previewError = ref('')

const chatEngineSpec = computed(() => {
  const id = active.value.tts_chat?.engine
  return id ? engines.value.tts?.[id] : null
})
const storiesEngineSpec = computed(() => engines.value.tts?.edge || null)

onMounted(async () => {
  try {
    const [reg, cur] = await Promise.all([
      apiFetch('/api/voice/engines'),
      apiFetch('/api/voice/active'),
    ])
    engines.value = reg
    active.value = cur
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
</script>

<template>
  <div class="voice-settings">
    <p v-if="loading" class="loading">Loading…</p>
    <p v-else-if="error" class="error">{{ error }}</p>
    <template v-else>
      <!-- Chat / notifications voice -->
      <section class="card">
        <header>
          <h2>Chat &amp; Notification Voice</h2>
          <p class="hint">
            Used by interactive chat replies and cron notifications. Default is Edge (free, no API key).
            Paid engines (ElevenLabs, OpenAI, Azure) require a secret — set them via Settings → Services.
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
            <select
              v-if="p.type === 'select'"
              :value="paramValue(active.tts_chat, p.key, p.default)"
              @change="setParam(active.tts_chat, p.key, $event.target.value)"
            >
              <option v-for="o in p.options" :key="o" :value="o">{{ o }}</option>
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
          </div>
          <p v-if="chatEngineSpec.secrets?.length" class="secret-note">
            Required secret(s): {{ chatEngineSpec.secrets.join(', ') }} — set via API/Services tab.
          </p>
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
            <select
              v-if="p.type === 'select'"
              :value="active.tts_stories?.[p.key] ?? p.default"
              @change="active.tts_stories = { ...active.tts_stories, [p.key]: $event.target.value }"
            >
              <option v-for="o in p.options" :key="o" :value="o">{{ o }}</option>
            </select>
            <input
              v-else
              type="text"
              :value="active.tts_stories?.[p.key] ?? p.default"
              :placeholder="p.help || ''"
              @input="active.tts_stories = { ...active.tts_stories, [p.key]: $event.target.value }"
            />
          </div>
        </div>

        <div class="actions">
          <button type="button" :disabled="previewing" @click="preview({ params: active.tts_stories }, 'edge')">
            {{ previewing ? 'Playing…' : 'Preview' }}
          </button>
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
.card header h2 { font-size: 17px; font-weight: 600; }
.hint { font-size: 13px; color: var(--text-nav, #8b8fa3); margin-top: 4px; }
.desc { font-size: 13px; color: var(--text-secondary, #c4c8d4); margin: 0 0 6px; }
.row { display: grid; grid-template-columns: 200px 1fr; align-items: center; gap: 12px; }
label { font-size: 13px; color: var(--text-secondary, #c4c8d4); }
select, input[type="text"], input[type="number"] {
  background: var(--surface-2, #0e1020); border: 1px solid var(--border, #1e2030);
  color: var(--text-primary, #f0f2f5); padding: 8px 10px; border-radius: 6px; font: inherit;
}
.actions { display: flex; gap: 10px; padding-top: 4px; }
button {
  background: transparent; border: 1px solid var(--border, #1e2030);
  color: var(--text-primary, #f0f2f5); padding: 8px 14px; border-radius: 6px; cursor: pointer;
}
button:disabled { opacity: 0.5; cursor: progress; }
button.primary { background: var(--accent-blue, #3b82f6); border-color: var(--accent-blue, #3b82f6); }
.secret-note { font-size: 12px; color: var(--accent-yellow, #d97706); margin-top: 4px; }
.locked-badge {
  display: inline-block; margin-left: 8px; padding: 2px 8px;
  background: var(--surface-2, #0e1020); border: 1px solid var(--border, #1e2030);
  border-radius: 999px; font-size: 11px; color: var(--text-nav, #8b8fa3);
}
.error { color: var(--accent-red, #ef4444); font-size: 13px; }
.loading { color: var(--text-nav, #8b8fa3); }
.footer { display: flex; justify-content: flex-end; }
</style>
