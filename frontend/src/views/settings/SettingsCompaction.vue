<script setup>
/**
 * Settings → Context Compaction.
 *
 * Typed form over GET/PATCH /api/context-compaction/settings (backed by
 * the config DB category `context_compaction`). Range validation lives
 * on the backend (services.context_compaction.validate_config_updates);
 * the inputs only carry min/max hints so the browser nudges the user
 * toward valid values — the PATCH 422 message is the authority.
 *
 * Save sends ONLY the changed keys (PATCH semantics) so the audit
 * history in config_history reflects what the user actually touched.
 */
import { computed, onMounted, ref } from 'vue'
import { apiFetch, ApiError } from '../../api'
import { useLang } from '../../composables/useLang'

const { t } = useLang()

const loading = ref(true)
const saving = ref(false)
const error = ref('')
const saveSuccess = ref(false)

const config = ref(null) // server truth
const draft = ref(null)  // form state

const NUMBER_FIELDS = computed(() => [
  {
    key: 'compact_at_ratio',
    label: t('settings.compaction.compactAtRatioLabel'),
    hint: t('settings.compaction.compactAtRatioHint'),
    min: 0.3, max: 0.95, step: 0.05,
  },
  {
    key: 'max_context_tokens',
    label: t('settings.compaction.maxContextTokensLabel'),
    hint: t('settings.compaction.maxContextTokensHint'),
    min: 0, max: 2000000, step: 1000,
  },
  {
    key: 'keep_recent_messages',
    label: t('settings.compaction.keepRecentLabel'),
    hint: t('settings.compaction.keepRecentHint'),
    min: 2, max: 100, step: 1,
  },
  {
    key: 'max_tool_result_tokens_in_context',
    label: t('settings.compaction.maxToolResultLabel'),
    hint: t('settings.compaction.maxToolResultHint'),
    min: 100, max: 100000, step: 100,
  },
  {
    key: 'min_savings_ratio',
    label: t('settings.compaction.minSavingsLabel'),
    hint: t('settings.compaction.minSavingsHint'),
    min: 0, max: 0.9, step: 0.01,
  },
  {
    key: 'compactor_input_ratio',
    label: t('settings.compaction.compactorInputLabel'),
    hint: t('settings.compaction.compactorInputHint'),
    min: 0.1, max: 0.9, step: 0.05,
  },
  {
    key: 'snapshot_versions_visible',
    label: t('settings.compaction.snapshotVersionsLabel'),
    hint: t('settings.compaction.snapshotVersionsHint'),
    min: 1, max: 50, step: 1,
  },
])

const dirtyKeys = computed(() => {
  if (!config.value || !draft.value) return []
  return Object.keys(draft.value).filter(
    (k) => String(draft.value[k]) !== String(config.value[k]),
  )
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    config.value = await apiFetch('/api/context-compaction/settings')
    draft.value = { ...config.value }
  } catch (err) {
    error.value = _friendly(err)
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!dirtyKeys.value.length || saving.value) return
  saving.value = true
  error.value = ''
  saveSuccess.value = false
  try {
    const patch = {}
    for (const k of dirtyKeys.value) {
      patch[k] = typeof config.value[k] === 'boolean'
        ? Boolean(draft.value[k])
        : typeof config.value[k] === 'number'
          ? Number(draft.value[k])
          : String(draft.value[k] ?? '').trim()
    }
    config.value = await apiFetch('/api/context-compaction/settings', {
      method: 'PATCH',
      body: JSON.stringify(patch),
    })
    draft.value = { ...config.value }
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
    if (detail && typeof detail === 'object') return detail.message || t('settings.compaction.requestFailed')
  }
  return err?.message || String(err)
}

onMounted(load)
</script>

<template>
  <div class="compaction">
    <div class="intro">
      {{ t('settings.compaction.intro') }}
    </div>

    <p v-if="loading" class="muted">{{ t('settings.compaction.loading') }}</p>
    <p v-if="error" class="error">{{ error }}</p>

    <template v-if="draft">
      <div class="row">
        <div class="row-info">
          <h3>{{ t('settings.compaction.enableTitle') }}</h3>
          <p class="desc">
            {{ t('settings.compaction.enableDesc') }}
          </p>
        </div>
        <button
          class="toggle"
          :class="{ on: draft.enabled }"
          :aria-pressed="draft.enabled"
          @click="draft.enabled = !draft.enabled"
        >
          <span class="knob"></span>
        </button>
      </div>

      <div class="row">
        <div class="row-info">
          <h3>{{ t('settings.compaction.liveStatusTitle') }}</h3>
          <p class="desc">
            {{ t('settings.compaction.liveStatusDesc') }}
          </p>
        </div>
        <button
          class="toggle"
          :class="{ on: draft.emit_live_status }"
          :aria-pressed="draft.emit_live_status"
          @click="draft.emit_live_status = !draft.emit_live_status"
        >
          <span class="knob"></span>
        </button>
      </div>

      <div v-for="f in NUMBER_FIELDS" :key="f.key" class="row">
        <div class="row-info">
          <h3>{{ f.label }}</h3>
          <p class="desc">{{ f.hint }}</p>
        </div>
        <input
          v-model="draft[f.key]"
          class="num-input"
          type="number"
          :min="f.min"
          :max="f.max"
          :step="f.step"
        />
      </div>

      <div class="row">
        <div class="row-info">
          <h3>{{ t('settings.compaction.compactorModelTitle') }}</h3>
          <p class="desc">
            {{ t('settings.compaction.compactorModelDesc') }}
          </p>
        </div>
        <input
          v-model="draft.compactor_model"
          class="text-input"
          type="text"
          placeholder="e.g. kr/gemini-2.5-pro"
        />
      </div>

      <div class="actions">
        <button class="save-btn" :disabled="!dirtyKeys.length || saving" @click="save">
          {{ saving ? t('settings.compaction.saving') : t('common.saveChanges') }}
        </button>
        <span v-if="saveSuccess" class="success-msg">{{ t('settings.compaction.savedLive') }}</span>
        <span v-else-if="dirtyKeys.length" class="muted">{{ t('settings.compaction.unsavedChanges', { n: dirtyKeys.length }) }}</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.compaction {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 800px;
}
.intro {
  padding: 12px 16px;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  color: var(--text-dim);
  font-size: 13px;
  line-height: 1.6;
}
.muted { color: var(--text-muted); font-size: 13px; }
.error { color: var(--danger); font-size: 13px; margin: 4px 0 0; }
.row {
  display: flex;
  align-items: flex-start;
  gap: 18px;
  padding: 16px 18px;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
}
.row-info { flex: 1; min-width: 0; }
.row-info h3 {
  margin: 0 0 4px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}
.desc {
  margin: 0;
  font-size: 12px;
  color: var(--text-dim);
  line-height: 1.5;
}
.num-input {
  flex-shrink: 0;
  width: 130px;
  padding: 8px 10px;
  background: var(--bg-4);
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  color: var(--text);
  font-size: 13px;
  font-family: var(--font-mono);
}
.text-input {
  flex-shrink: 0;
  width: 220px;
  padding: 8px 10px;
  background: var(--bg-4);
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  color: var(--text);
  font-size: 13px;
  font-family: var(--font-mono);
}
.text-input:focus {
  outline: none;
  border-color: var(--accent);
}
.num-input:focus {
  outline: none;
  border-color: var(--primary);
}
.toggle {
  flex-shrink: 0;
  width: 44px;
  height: 24px;
  border-radius: 24px;
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  cursor: pointer;
  position: relative;
  transition: background 0.2s, border-color 0.2s;
}
.toggle:hover { border-color: var(--primary); }
.toggle.on {
  background: var(--primary);
  border-color: var(--primary);
}
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
.actions {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-top: 4px;
}
.save-btn {
  padding: 9px 18px;
  background: var(--primary);
  color: white;
  border: none;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}
.save-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.success-msg { color: var(--success); font-size: 13px; }

@media (max-width: 768px) {
  .row { flex-direction: column; gap: 10px; }
  .num-input { width: 100%; }
  .text-input { width: 100%; }
  .toggle::before {
    content: '';
    position: absolute;
    inset: -10px -6px;
  }
}
</style>
