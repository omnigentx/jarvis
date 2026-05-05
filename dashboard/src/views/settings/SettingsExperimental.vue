<script setup>
/**
 * Settings → Experimental.
 *
 * Single grouped place for experimental / preview features. The intent is:
 *  - Each feature is a one-line toggle backed by `experimental/<KEY>` in the
 *    config DB.
 *  - Every toggle reads/writes via the existing `/api/settings/experimental/...`
 *    routes — no special-case backend.
 *  - The page makes the experimental nature loud: a banner up top + per-row
 *    "Restart required" pills where the change can't hot-reload.
 *
 * v1 ships one feature: self-improving Jarvis (skill_server tool group).
 */
import { onMounted, ref } from 'vue'
import { apiFetch, ApiError } from '../../api'

const CATEGORY = 'experimental'

const features = ref([
  {
    key: 'SELF_IMPROVING_ENABLED',
    label: 'Self-improving Jarvis',
    description:
      'Lets Jarvis author, edit, attach, and delete skills at runtime via the skill_server MCP tools. ' +
      'Toggle takes effect on the next tool call — no restart required. ' +
      'Every state change writes a notification so you can review what the agent did.',
    requiresRestart: false,
    value: false,
    saving: false,
    error: '',
  },
])

const loading = ref(false)
const loadError = ref('')

async function loadAll() {
  loading.value = true
  loadError.value = ''
  try {
    for (const f of features.value) {
      try {
        const res = await apiFetch(
          `/api/settings/${CATEGORY}/${f.key}`,
        )
        f.value = _coerceBool(res?.value)
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          f.value = false // no row yet → default off
        } else {
          throw err
        }
      }
    }
  } catch (err) {
    loadError.value = _friendly(err)
  } finally {
    loading.value = false
  }
}

async function toggle(feature) {
  feature.saving = true
  feature.error = ''
  const next = !feature.value
  try {
    await apiFetch(`/api/settings/${CATEGORY}/${feature.key}`, {
      method: 'PUT',
      body: JSON.stringify({ value: next ? 'true' : 'false', is_secret: false }),
    })
    feature.value = next
  } catch (err) {
    feature.error = _friendly(err)
  } finally {
    feature.saving = false
  }
}

function _coerceBool(v) {
  if (typeof v === 'boolean') return v
  if (v == null) return false
  return ['1', 'true', 'yes', 'on'].includes(String(v).trim().toLowerCase())
}

function _friendly(err) {
  if (err instanceof ApiError && err.body && typeof err.body === 'object') {
    const detail = err.body.detail
    if (detail && typeof detail === 'object') return detail.message || 'Request failed.'
    if (typeof detail === 'string') return detail
  }
  return err?.message || String(err)
}

onMounted(loadAll)
</script>

<template>
  <div class="exp">
    <div class="warn-banner">
      <strong>⚠ Experimental features.</strong>
      These flags expose preview functionality that may change, regress, or be
      removed without notice. Enable only if you're comfortable with that.
    </div>

    <p v-if="loading" class="muted">Loading…</p>
    <p v-if="loadError" class="error">{{ loadError }}</p>

    <div class="feature-list">
      <div v-for="f in features" :key="f.key" class="feature-row">
        <div class="feature-info">
          <div class="feature-title-row">
            <h3>{{ f.label }}</h3>
            <span v-if="f.requiresRestart" class="pill">Requires Restart</span>
          </div>
          <p class="feature-desc">{{ f.description }}</p>
          <p v-if="f.error" class="error">{{ f.error }}</p>
        </div>
        <button
          class="toggle"
          :class="{ on: f.value, saving: f.saving }"
          :disabled="f.saving"
          @click="toggle(f)"
          :aria-pressed="f.value"
        >
          <span class="knob"></span>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.exp {
  display: flex;
  flex-direction: column;
  gap: 18px;
  max-width: 800px;
}
.warn-banner {
  padding: 12px 16px;
  background: rgba(245, 158, 11, 0.08);
  border: 1px solid rgba(245, 158, 11, 0.25);
  border-radius: 10px;
  color: #fbbf24;
  font-size: 13px;
  line-height: 1.6;
}
.warn-banner strong { color: #fbbf24; }

.muted { color: #555872; font-size: 13px; }
.error { color: #f87171; font-size: 13px; margin: 4px 0 0; }

.feature-list { display: flex; flex-direction: column; gap: 12px; }
.feature-row {
  display: flex;
  align-items: flex-start;
  gap: 18px;
  padding: 18px;
  background: var(--bg-card, #111318);
  border: 1px solid var(--border, #1e2030);
  border-radius: 12px;
}
.feature-info { flex: 1; min-width: 0; }
.feature-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}
.feature-info h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary, #f0f2f5);
}
.pill {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(245, 158, 11, 0.12);
  color: #fbbf24;
  border: 1px solid rgba(245, 158, 11, 0.25);
}
.feature-desc {
  margin: 0;
  font-size: 13px;
  color: var(--text-nav, #8b8fa3);
  line-height: 1.6;
}

.toggle {
  flex-shrink: 0;
  width: 44px;
  height: 24px;
  border-radius: 24px;
  background: #1e2233;
  border: 1px solid #2a3556;
  cursor: pointer;
  position: relative;
  transition: background 0.2s, border-color 0.2s;
}
.toggle:hover:not(:disabled) { border-color: #3b82f6; }
.toggle.on {
  background: #3b82f6;
  border-color: #3b82f6;
}
.toggle.saving { opacity: 0.6; cursor: not-allowed; }
.toggle:disabled { cursor: not-allowed; }
.knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  background: white;
  border-radius: 50%;
  transition: left 0.2s;
}
.toggle.on .knob { left: 22px; }
</style>
