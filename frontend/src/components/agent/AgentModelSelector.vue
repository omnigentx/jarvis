<script setup>
import { onMounted, ref, watch } from 'vue'
import { apiFetch } from '../../api'

const props = defineProps({ agent: { type: Object, required: true } })
const emit = defineEmits(['updated'])
const modelId = ref(props.agent.model || '')
const saving = ref(false)
const error = ref('')
const message = ref('')
const models = ref([])

onMounted(async () => {
  try {
    const result = await apiFetch('/api/agents/model-catalog')
    models.value = result.models || []
  } catch (exc) {
    error.value = exc?.body?.detail || exc?.message || String(exc)
  }
})

watch(() => props.agent.model, value => { modelId.value = value || '' })

async function save() {
  error.value = ''
  message.value = ''
  if (saving.value || !props.agent.run_id) return
  saving.value = true
  try {
    const result = await apiFetch(`/api/agents/${encodeURIComponent(props.agent.name)}/model`, {
      method: 'PUT',
      body: JSON.stringify({
        run_id: props.agent.run_id,
        model_id: modelId.value.trim(),
        expected_revision: props.agent.model_revision ?? 0,
      }),
    })
    message.value = result.changed ? 'Applies from the next model call.' : 'Model is unchanged.'
    emit('updated')
  } catch (exc) {
    error.value = exc?.body?.detail || exc?.message || String(exc)
    if (exc?.status === 409) emit('updated')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="rounded-xl border border-[#1a1d2e] bg-[#0c0e15] p-4" aria-label="Model selection">
    <label class="mb-2 block text-sm font-semibold text-[#f0f2f5]" for="agent-model-id">Model</label>
    <div class="flex flex-wrap gap-2">
      <input
        id="agent-model-id"
        v-model="modelId"
        list="available-agent-models"
        class="min-w-[15rem] flex-1 rounded-lg border border-[#1e2030] bg-[#111318] px-3 py-2 text-[#f0f2f5]"
        placeholder="openai.coding-agent"
        autocomplete="off"
        :disabled="saving"
        @keydown.enter="save"
      />
      <datalist id="available-agent-models">
        <option v-for="item in models" :key="item" :value="item" />
      </datalist>
      <button class="rounded-lg bg-[#3b82f6] px-4 py-2 font-semibold text-white disabled:opacity-50"
        :disabled="saving || !modelId.trim() || modelId.trim() === agent.model" @click="save">
        {{ saving ? 'Saving…' : 'Apply model' }}
      </button>
    </div>
    <p class="mt-2 text-xs text-[#8b8fa3]">OpenAI-compatible model ID. Changes take effect on the next LLM call; current calls finish on their captured model.</p>
    <p class="mt-2 text-xs text-[#c4c8d4]">Configured: {{ agent.configured_model || agent.model || 'default' }} · Active: {{ agent.active_model || 'none' }}<span v-if="agent.active_revision != null"> (revision {{ agent.active_revision }})</span></p>
    <p v-if="message" class="mt-2 text-sm text-[#10b981]" role="status">{{ message }}</p>
    <p v-if="error" class="mt-2 text-sm text-[#ef4444]" role="alert">{{ error }}</p>
  </section>
</template>
