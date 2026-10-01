<script setup>
import { onMounted, ref } from 'vue'
import { apiFetch } from '../../api'
import { useLang } from '../../composables/useLang'

const props = defineProps({ plugin: { type: Object, required: true } })
const emit = defineEmits(['saved'])
const { t } = useLang()
const image = ref('')
const credentials = ref({})
const busy = ref(false)
const error = ref('')
const configured = ref(false)
const available = ref(false)
const storedSlots = ref([])
onMounted(async () => {
  try {
    const [profile, policy] = await Promise.all([
      apiFetch('/api/plugins/runtime-profile'),
      apiFetch(`/api/plugins/${encodeURIComponent(props.plugin.id)}/policy`),
    ])
    image.value = policy.image || profile.image || ''
    available.value = profile.runtime_available
    configured.value = Boolean(policy.image)
    storedSlots.value = policy.credential_slots
  } catch (cause) { error.value = cause.message }
})
async function save() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const result = await apiFetch(`/api/plugins/${encodeURIComponent(props.plugin.id)}/policy`, {
      method: 'PUT', body: JSON.stringify({ image: image.value.trim(), credentials: credentials.value }),
    })
    configured.value = result.configured
    storedSlots.value = result.credential_slots
    credentials.value = {}
    emit('saved')
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
</script>

<template>
  <details class="execution-policy">
    <summary>{{ t('settings.plugins.execution') }} · {{ configured ? t('settings.plugins.configured') : t('settings.plugins.notConfigured') }}</summary>
    <p>{{ t('settings.plugins.sandboxDescription') }}</p>
    <p v-if="!available" role="status">{{ t('settings.plugins.runtimeUnavailable') }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <form @submit.prevent="save">
      <label>{{ t('settings.plugins.image') }}<input v-model="image" required maxlength="512" autocomplete="off" placeholder="sha256:…" /></label>
      <label v-for="slot in plugin.credential_slots" :key="slot">{{ slot }}<input v-model="credentials[slot]" type="password" required autocomplete="new-password" maxlength="8192" /></label>
      <small v-if="storedSlots.length">{{ t('settings.plugins.storedCredentials') }}: {{ storedSlots.join(', ') }}</small>
      <button type="submit" :disabled="busy || !available">{{ t('settings.plugins.savePolicy') }}</button>
    </form>
  </details>
</template>
