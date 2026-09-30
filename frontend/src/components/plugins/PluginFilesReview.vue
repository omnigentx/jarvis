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

<style scoped>
.package-review { margin: 12px 0; min-width: 0; }
button { min-height: 44px; padding: 10px 14px; border: 1px solid var(--border); border-radius: var(--r-md); color: var(--text); background: var(--bg-0); cursor: pointer; max-width: 100%; overflow-wrap: anywhere; text-align: left; }
button:disabled { opacity: .5; }
button:focus-visible, pre:focus-visible, a:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
ul { max-height: 220px; overflow: auto; list-style: none; padding: 0; }
li { margin: 6px 0; }
a { color: var(--primary); }
p, small { color: var(--text-muted); overflow-wrap: anywhere; }
pre { max-height: 400px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font: 13px var(--font-mono); line-height: 1.6; }
[role=alert] { color: var(--danger, #ef4444); }
</style>
