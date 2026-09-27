<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { apiFetch } from '../../api'
import { useLang } from '../../composables/useLang'

const props = defineProps({
  agent: { type: Object, required: true },
  statusEvent: { type: Object, default: null },
})
const emit = defineEmits(['updated'])
const { t } = useLang()
const modelId = ref(props.agent.model || '')
const editing = ref(false)
const editRevision = ref(props.agent.model_revision ?? 0)
const saving = ref(false)
const error = ref('')
const catalogError = ref(false)
const message = ref('')
const models = ref([])
const showSuggestions = ref(false)
const suggestionIndex = ref(-1)
const configuredModel = computed(() => props.agent.configured_model || props.agent.model || props.agent.base_model || '—')
const stale = computed(() => editing.value && editRevision.value !== (props.agent.model_revision ?? 0))
const suggestions = computed(() => {
  const query = modelId.value.trim().toLowerCase()
  if (query.length < 2) return []
  return models.value.filter(item => item.toLowerCase().includes(query)).slice(0, 8)
})
const remoteStatus = computed(() => {
  const event = props.statusEvent
  if (!event) return ''
  const data = event.data || {}
  if (event.event_type === 'model_change_requested')
    return t('agentDetail.modelRemoteRequested', { actor: data.actor || t('agentDetail.modelAgent') })
  if (event.event_type === 'model_change_failed')
    return t('agentDetail.modelRemoteFailed', { error: data.error || t('agentDetail.modelUnknownError') })
  if (event.event_type === 'model_changed')
    return t('agentDetail.modelRemoteChanged', { model: data.model || t('agentDetail.modelDefault') })
  return ''
})

async function loadCatalog() {
  catalogError.value = false
  try {
    const result = await apiFetch('/api/agents/model-catalog')
    models.value = result.models || []
  } catch {
    catalogError.value = true
  }
}

onMounted(loadCatalog)

watch(() => props.agent.model, value => { if (!editing.value) modelId.value = value || '' })
watch(() => props.agent.name, () => { editing.value = false })

function startEditing() {
  modelId.value = configuredModel.value === '—' ? '' : configuredModel.value
  editRevision.value = props.agent.model_revision ?? 0
  error.value = ''
  message.value = ''
  showSuggestions.value = false
  suggestionIndex.value = -1
  editing.value = true
}

function cancelEditing() {
  editing.value = false
  showSuggestions.value = false
  error.value = ''
}

function chooseModel(value) {
  modelId.value = value
  showSuggestions.value = false
  suggestionIndex.value = -1
}

function onModelKeydown(event) {
  if (event.key === 'Escape') {
    showSuggestions.value = false
    suggestionIndex.value = -1
  } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    if (!suggestions.value.length) return
    event.preventDefault()
    showSuggestions.value = true
    suggestionIndex.value = event.key === 'ArrowDown'
      ? (suggestionIndex.value + 1) % suggestions.value.length
      : (suggestionIndex.value + suggestions.value.length) % suggestions.value.length
  } else if (event.key === 'Enter' && showSuggestions.value && suggestionIndex.value >= 0) {
    event.preventDefault()
    chooseModel(suggestions.value[suggestionIndex.value])
  }
}

async function submit(requestedModel) {
  error.value = ''
  message.value = ''
  if (saving.value || stale.value) return
  saving.value = true
  try {
    const result = await apiFetch(`/api/agents/${encodeURIComponent(props.agent.name)}/model`, {
      method: 'PUT',
      body: JSON.stringify({
        run_id: props.agent.run_id || '',
        model_id: requestedModel,
        expected_revision: editRevision.value,
      }),
    })
    editing.value = false
    message.value = t(result.changed ? 'agentDetail.modelSaved' : 'agentDetail.modelUnchanged')
    emit('updated')
  } catch (exc) {
    error.value = exc?.body?.detail || exc?.message || String(exc)
    if (exc?.status === 409) emit('updated')
  } finally {
    saving.value = false
  }
}

function save() { return submit(modelId.value.trim()) }
function reset() { return submit('') }
</script>

<template>
  <section class="model-panel" :aria-label="t('agentDetail.modelSelection')">
    <div class="model-header">
      <div class="model-summary">
        <div class="model-title-row">
          <h2>{{ t('agentDetail.statModel') }}</h2>
          <span class="model-origin">{{ t(agent.overridden ? 'agentDetail.modelCustom' : 'agentDetail.modelInherited') }}</span>
        </div>
        <p class="model-name">{{ configuredModel }}</p>
        <p v-if="agent.active_model && agent.active_model !== configuredModel" class="model-help">
          {{ t('agentDetail.modelCurrentCall', { model: agent.active_model }) }}
        </p>
      </div>
      <button v-if="!editing" class="model-edit" type="button" @click="startEditing">{{ t('agentDetail.modelEdit') }}</button>
    </div>
    <form v-if="editing" class="model-editor" @submit.prevent="save">
      <label for="agent-model-id">{{ t('agentDetail.modelId') }}</label>
      <div class="model-input-wrap">
        <input
          id="agent-model-id"
          v-model="modelId"
          role="combobox"
          aria-autocomplete="list"
          :aria-controls="showSuggestions && suggestions.length ? 'available-agent-models' : undefined"
          :aria-expanded="showSuggestions && suggestions.length > 0"
          :aria-activedescendant="showSuggestions && suggestionIndex >= 0 ? `agent-model-option-${suggestionIndex}` : undefined"
          placeholder="openai.coding-agent"
          autocomplete="off"
          :disabled="saving"
          :aria-invalid="!!error"
          :aria-describedby="error ? 'model-change-error' : 'model-search-help'"
          @focus="showSuggestions = true"
          @blur="showSuggestions = false"
          @input="showSuggestions = true; suggestionIndex = -1"
          @keydown="onModelKeydown"
        />
        <ul v-if="showSuggestions && suggestions.length" id="available-agent-models" class="model-suggestions" role="listbox"
          :aria-label="t('agentDetail.modelSuggestions')">
          <li v-for="(item, index) in suggestions" :id="`agent-model-option-${index}`" :key="item" role="option"
            :aria-selected="index === suggestionIndex" :class="{ active: index === suggestionIndex }"
            @pointerdown.prevent="chooseModel(item)" @click="chooseModel(item)">{{ item }}</li>
        </ul>
      </div>
      <p v-if="error" id="model-change-error" class="model-error" role="alert">{{ error }}</p>
      <p v-if="!error && models.length" id="model-search-help" class="model-help">
        {{ t('agentDetail.modelSearchHint', { n: models.length }) }}
      </p>
      <p v-if="!error" class="model-help">{{ t('agentDetail.modelNextCall') }}</p>
      <p v-if="catalogError" class="model-warning">
        {{ t('agentDetail.modelCatalogUnavailable') }}
        <button type="button" @click="loadCatalog">{{ t('agentDetail.modelRetry') }}</button>
      </p>
      <p v-if="stale" class="model-warning" role="alert">{{ t('agentDetail.modelChangedElsewhere') }}</p>
      <div class="model-actions" :class="{ 'has-reset': agent.overridden }">
        <button class="model-save" type="submit"
          :disabled="saving || stale || !modelId.trim() || modelId.trim() === configuredModel">
          {{ saving ? t('agentDetail.modelSaving') : t('agentDetail.modelApply') }}
        </button>
        <button v-if="agent.overridden" class="model-reset" type="button" :disabled="saving || stale" @click="reset">
          {{ t('agentDetail.modelUseDefault') }}
        </button>
        <button class="model-cancel" type="button" :disabled="saving" @click="stale ? startEditing() : cancelEditing()">
          {{ t(stale ? 'agentDetail.modelLoadLatest' : 'agentDetail.modelCancel') }}
        </button>
      </div>
    </form>
    <p v-if="message" class="model-success" role="status">{{ message }}</p>
    <p v-if="remoteStatus" class="model-event" :class="{ 'model-error': statusEvent?.event_type === 'model_change_failed' }"
      :role="statusEvent?.event_type === 'model_change_failed' ? 'alert' : 'status'">{{ remoteStatus }}</p>
  </section>
</template>

<style scoped>
.model-panel { min-width: 0; margin-bottom: 16px; padding: 18px 20px; border: 1px solid var(--border); border-radius: var(--r-md); background: var(--bg-1); }
.model-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.model-summary { min-width: 0; flex: 1; }
.model-title-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.model-title-row h2 { font-size: 15px; font-weight: 650; color: var(--text); }
.model-origin { border: 1px solid var(--border-strong); border-radius: 999px; padding: 2px 8px; color: var(--text-dim); font-size: 11px; }
.model-name { overflow-wrap: anywhere; margin-top: 6px; color: var(--text); font-family: var(--font-mono, monospace); font-size: 14px; }
.model-help { margin-top: 7px; color: var(--text-muted); font-size: 12px; }
.model-edit, .model-save, .model-reset, .model-cancel { min-height: 36px; border-radius: 8px; padding: 7px 12px; font-size: 13px; font-weight: 600; cursor: pointer; }
.model-edit { flex-shrink: 0; border: 1px solid var(--border-strong); background: var(--bg-3); color: var(--text); }
.model-editor { display: grid; gap: 8px; margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--border); }
.model-editor label { color: var(--text-dim); font-size: 12px; font-weight: 600; }
.model-input-wrap { position: relative; min-width: 0; }
.model-editor input { width: 100%; min-width: 0; min-height: 42px; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--bg-2); color: var(--text); padding: 8px 11px; font-family: var(--font-mono, monospace); font-size: 13px; }
.model-suggestions { position: absolute; z-index: 10; top: calc(100% + 4px); left: 0; right: 0; max-height: 240px; overflow-y: auto; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--bg-2); box-shadow: var(--shadow-md); list-style: none; }
.model-suggestions li { overflow-wrap: anywhere; padding: 8px 11px; color: var(--text); font-family: var(--font-mono, monospace); font-size: 12px; cursor: pointer; }
.model-suggestions li:hover, .model-suggestions li.active { background: var(--primary-bg-strong); }
.model-editor input:focus-visible, .model-panel button:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.model-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
.model-save { border: 1px solid var(--primary); background: var(--primary); color: #fff; }
.model-reset, .model-cancel { border: 1px solid var(--border-strong); background: transparent; color: var(--text-dim); }
.model-panel button:disabled { cursor: not-allowed; opacity: .48; }
.model-success, .model-error, .model-event, .model-warning { overflow-wrap: anywhere; margin-top: 10px; font-size: 12px; }
.model-success { color: var(--success); }
.model-error { color: var(--danger); }
.model-event { color: var(--text-dim); }
.model-warning { color: var(--warning); }
.model-warning button { margin-left: 4px; color: inherit; text-decoration: underline; }
@media (max-width: 767px) {
  .model-panel { margin: 12px 14px; padding: 14px; }
  .model-header { gap: 8px; }
  .model-edit, .model-save, .model-reset, .model-cancel { min-height: 44px; }
  .model-edit { min-width: 64px; }
  .model-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .model-actions.has-reset .model-save { grid-column: 1 / -1; }
}
</style>
