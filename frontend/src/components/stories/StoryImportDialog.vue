<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { apiFetch, buildSSEUrl } from '../../api'
import { useLang } from '../../composables/useLang'
import { useSSEConnection } from '../../composables/useSSEConnection'
import { prepareChapters } from '../../utils/storyImport'
const emit = defineEmits(['close', 'imported'])
const { t } = useLang()
const dialog = ref(null), folderInput = ref(null), filesInput = ref(null)
const title = ref(''), selection = ref(prepareChapters([])), busy = ref(false)
const error = ref(null), completed = ref(0), attempted = ref(false), chosen = ref(false)
let requestId = crypto.randomUUID(), controller = null
const canSubmit = computed(() => !busy.value && !selection.value.error && !!title.value.trim())
useSSEConnection(() => buildSSEUrl('/api/agents/activity-stream'), {
  onMessage(event) {
    try {
      const message = JSON.parse(event.data)
      if (message.event_type === 'story_import' && message.data?.request_id === requestId) completed.value = message.data.completed
    } catch { /* Unrelated messages must not interrupt an upload. */ }
  },
})
onMounted(() => dialog.value.showModal())
onUnmounted(() => controller?.abort())
function choose(event) {
  chosen.value = true
  selection.value = prepareChapters(event.target.files)
  title.value = event.target.files?.[0]?.webkitRelativePath?.split('/')[0] || ''
  requestId = crypto.randomUUID()
  error.value = null
  completed.value = 0
  attempted.value = false
  event.target.value = ''
}
async function submit() {
  if (!canSubmit.value) return
  busy.value = true
  attempted.value = true
  error.value = null
  completed.value = 0
  const data = new FormData()
  data.append('title', title.value.trim())
  data.append('request_id', requestId)
  for (const file of selection.value.files) data.append('files', file, file.webkitRelativePath || file.name)
  controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 120000)
  try {
    emit('imported', await apiFetch('/api/stories/import', { method: 'POST', body: data, signal: controller.signal }))
  } catch (failure) {
    error.value = failure.body?.detail?.code ? failure.body.detail : { code: failure.status === 401 ? 'auth' : 'connection' }
  } finally {
    clearTimeout(timeout)
    controller = null
    busy.value = false
  }
}
function close(event) {
  if (busy.value) { event?.preventDefault(); return }
  emit('close')
}
</script>
<template>
  <dialog ref="dialog" class="story-import" aria-labelledby="import-title" @cancel="close">
    <header class="story-import__header">
      <div><div class="eyebrow">{{ t('storyImport.eyebrow') }}</div><h2 id="import-title">{{ t('storyImport.open') }}</h2></div>
      <button type="button" class="btn btn-secondary" :disabled="busy" :aria-label="t('storyImport.close')" @click="close">×</button>
    </header>
    <p class="story-import__muted">{{ t('storyImport.description') }}</p>
    <form @submit.prevent="submit">
      <div class="story-import__pickers">
        <button type="button" class="btn btn-secondary" :disabled="busy || attempted" @click="folderInput.click()">{{ t('storyImport.folder') }}</button>
        <button type="button" class="btn btn-secondary" :disabled="busy || attempted" @click="filesInput.click()">{{ t('storyImport.files') }}</button>
        <input ref="folderInput" type="file" webkitdirectory multiple hidden @change="choose" />
        <input ref="filesInput" type="file" accept=".txt,text/plain" multiple hidden @change="choose" />
      </div>
      <label for="story-title">{{ t('storyImport.title') }}</label>
      <input id="story-title" v-model="title" class="story-import__input" maxlength="255" required :disabled="busy || attempted" autocomplete="off" />
      <template v-if="selection.files.length">
        <p class="story-import__muted">{{ t('storyImport.summary', { count: selection.files.length, size: (selection.bytes / 1048576).toFixed(2) }) }}</p>
        <h3>{{ t('storyImport.preview') }}</h3>
        <ol class="story-import__preview"><li v-for="(file, index) in selection.files" :key="index">{{ file.name }}</li></ol>
      </template>
      <p v-if="selection.ignored" class="story-import__muted">{{ t('storyImport.ignored', { count: selection.ignored }) }}</p>
      <p v-if="selection.error && chosen" class="story-import__error" role="alert">{{ t(`storyImport.errors.${selection.error}`) }}</p>
      <p v-if="error" class="story-import__error" role="alert">{{ t(`storyImport.errors.${error.code}`, { index: error.index || '' }) }}</p>
      <div v-if="busy" role="status" aria-live="polite"><p>{{ t('storyImport.progress', { completed, total: selection.files.length }) }}</p><progress :value="completed" :max="selection.files.length" /></div>
      <p v-if="attempted && !busy" class="story-import__muted">{{ t('storyImport.retryHint') }}</p>
      <footer class="story-import__footer">
        <button type="button" class="btn btn-secondary" :disabled="busy" @click="close">{{ t('storyImport.close') }}</button>
        <button type="submit" class="btn btn-primary" :disabled="!canSubmit">{{ t(attempted && !busy ? 'storyImport.retry' : 'storyImport.submit') }}</button>
      </footer>
    </form>
  </dialog>
</template>
<style scoped>
.story-import { width: min(640px, calc(100vw - 32px)); max-height: calc(100dvh - 32px); margin: auto; padding: 24px; background: var(--bg-1); color: var(--text); border: 1px solid var(--border); border-radius: var(--r-lg); overflow-y: auto; }
.story-import .btn:disabled { opacity: 0.45; cursor: not-allowed; }
.story-import::backdrop { background: rgb(0 0 0 / 65%); }
.story-import__header, .story-import__footer, .story-import__pickers { display: flex; gap: 12px; align-items: center; justify-content: space-between; }
.story-import h2 { font-size: 24px; margin: 8px 0; }
.story-import h3 { font-size: 14px; margin: 16px 0 8px; }
.story-import__muted { color: var(--text-muted); font-size: 14px; line-height: 1.6; margin: 16px 0; }
.story-import__pickers { justify-content: flex-start; flex-wrap: wrap; margin: 20px 0; }
.story-import label { display: block; margin-bottom: 8px; font-size: 14px; }
.story-import__input { width: 100%; padding: 12px; color: var(--text); background: var(--bg-2); border: 1px solid var(--border); border-radius: var(--r-md); }
.story-import__input:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.story-import__preview { max-height: 220px; overflow-y: auto; padding: 12px 16px 12px 48px; border: 1px solid var(--border); border-radius: var(--r-md); font-size: 14px; }
.story-import__preview li { padding: 5px 0; overflow-wrap: anywhere; }
.story-import__error { color: var(--danger, #ef4444); background: rgb(239 68 68 / 8%); padding: 12px; border-radius: var(--r-md); font-size: 14px; }
.story-import__footer { margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--border); justify-content: flex-end; }
.story-import progress { width: 100%; accent-color: var(--primary); }
@media (max-width: 480px) { .story-import { padding: 16px; } .story-import__pickers .btn, .story-import__footer .btn { flex: 1; } }
</style>
