<script setup>
import { ref } from 'vue'
import { apiFetch } from '../../api'
import { useLang } from '../../composables/useLang'

const props = defineProps({ plugin: { type: Object, required: true } })
const { t } = useLang()
const files = ref(null)
const selected = ref('')
const content = ref('')
const error = ref('')
const busy = ref(false)
async function load(path) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const suffix = path ? `?path=${encodeURIComponent(path)}` : ''
    const result = await apiFetch(`/api/plugins/${encodeURIComponent(props.plugin.id)}/files${suffix}`)
    if (result.digest !== props.plugin.digest) throw new Error(t('settings.plugins.reviewChanged'))
    if (path) { selected.value = path; content.value = result.content }
    else files.value = result.files
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
</script>

<template>
  <div class="package-review">
    <button :disabled="busy" @click="files ? files = null : load()">{{ t(files ? 'common.close' : 'settings.plugins.reviewFiles') }}</button>
    <div v-if="error" role="alert">{{ error }}</div>
    <section v-if="files" :aria-label="t('settings.plugins.reviewFiles')">
      <p>{{ t('settings.plugins.untrusted') }}</p>
      <a :href="`https://github.com/${plugin.repo}/tree/${plugin.commit}/${plugin.subdirectory}`" target="_blank" rel="noopener noreferrer">{{ t('settings.plugins.pinnedSource') }}</a>
      <ul><li v-for="file in files" :key="file.path"><button :disabled="busy" @click="load(file.path)">{{ file.path }} <small>{{ file.bytes }} B</small></button></li></ul>
      <p v-if="selected">{{ selected }}</p><pre v-if="selected" tabindex="0">{{ content }}</pre>
    </section>
  </div>
</template>
