<script setup>
/**
 * Step 4 — YAML Config (optional).
 *
 * Loads the two fast-agent YAML files via /api/yaml/* (same endpoints the
 * Settings view uses) and surfaces a CodeMirror editor so the user can
 * tweak them before finishing setup.  The setup-gate middleware explicitly
 * whitelists /api/yaml so these calls succeed mid-wizard — the endpoints
 * still enforce the master API key via ``verify_api_key``.
 *
 * The step is non-critical: Skip bypasses editing entirely, Accept & Continue
 * marks the step done even if the user didn't touch anything.  If the user
 * has unsaved buffer edits we warn before leaving.
 */
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Codemirror } from 'vue-codemirror'
import { yaml } from '@codemirror/lang-yaml'
import { oneDark } from '@codemirror/theme-one-dark'
import { EditorView, lineNumbers, highlightActiveLine, keymap } from '@codemirror/view'
import { EditorState } from '@codemirror/state'
import { defaultKeymap, indentWithTab, history, historyKeymap } from '@codemirror/commands'
import { useSetupStore } from '../../stores/setup'
import { useConfirm } from '../../composables/useConfirm'
import { apiFetch, ApiError } from '../../api'
import WizardCard from './WizardCard.vue'
import './wizard.css'

const router = useRouter()
const store = useSetupStore()
const { confirm } = useConfirm()

const files = ref([])
const activeName = ref(null)
const content = ref('')
const savedContent = ref('')
const exists = ref(false)
const sizeBytes = ref(0)
const loading = ref(false)
const saving = ref(false)
const submitting = ref(false)
const editorError = ref('')
const editorSuccess = ref('')

const extensions = [
  yaml(),
  oneDark,
  lineNumbers(),
  highlightActiveLine(),
  history(),
  keymap.of([...defaultKeymap, ...historyKeymap, indentWithTab]),
  EditorState.tabSize.of(2),
  EditorView.theme({
    '&': { fontSize: '12px', height: '100%' },
    '.cm-scroller': { fontFamily: "ui-monospace, 'SF Mono', Menlo, Consolas, monospace" },
    '.cm-gutters': { background: '#0a0d14', borderRight: '1px solid #1e2030' },
    '&.cm-focused': { outline: 'none' },
  }),
]

const dirty = computed(() => content.value !== savedContent.value)

async function refreshList() {
  const res = await apiFetch('/api/yaml/files', { skipSetupRedirect: true })
  files.value = res?.files || []
  if (!activeName.value && files.value.length) {
    await selectFile(files.value[0].name)
  }
}

async function selectFile(name) {
  if (activeName.value === name) return
  if (dirty.value) {
    const ok = await confirm({
      title: 'Discard unsaved changes',
      message: 'You have unsaved changes in the current file. Discard them and switch?',
      confirmText: 'Discard & Switch',
      variant: 'warning',
    })
    if (!ok) return
  }
  editorError.value = ''
  editorSuccess.value = ''
  loading.value = true
  try {
    const res = await apiFetch(`/api/yaml/${encodeURIComponent(name)}`, {
      skipSetupRedirect: true,
    })
    activeName.value = name
    content.value = res.content || ''
    savedContent.value = res.content || ''
    exists.value = !!res.exists
    sizeBytes.value = res.size || 0
  } catch (err) {
    editorError.value = _friendly(err)
  } finally {
    loading.value = false
  }
}

async function onSaveFile() {
  if (!activeName.value || !dirty.value) return
  saving.value = true
  editorError.value = ''
  editorSuccess.value = ''
  try {
    const res = await apiFetch(`/api/yaml/${encodeURIComponent(activeName.value)}`, {
      method: 'PUT',
      skipSetupRedirect: true,
      body: JSON.stringify({ content: content.value }),
    })
    const fresh = await apiFetch(`/api/yaml/${encodeURIComponent(activeName.value)}`, {
      skipSetupRedirect: true,
    })
    content.value = fresh.content
    savedContent.value = fresh.content
    exists.value = true
    sizeBytes.value = res?.size ?? fresh.size
    editorSuccess.value = `${fresh.filename} saved (${sizeBytes.value} bytes).`
    refreshList().catch(() => {})
  } catch (err) {
    editorError.value = _friendly(err)
  } finally {
    saving.value = false
  }
}

async function onRevert() {
  if (!dirty.value) return
  if (
    !(await confirm({
      title: 'Revert changes',
      message: 'Discard unsaved changes and revert to last saved content?',
      confirmText: 'Revert',
      variant: 'warning',
    }))
  ) {
    return
  }
  content.value = savedContent.value
}

function _friendly(err) {
  if (err instanceof ApiError && err.body && typeof err.body === 'object') {
    const detail = err.body.detail
    if (detail && typeof detail === 'object') {
      return `${detail.message || 'Save failed'} — ${detail.error || ''}`.trim()
    }
    if (typeof detail === 'string') return detail
  }
  return err?.message || String(err)
}

async function onContinue() {
  if (dirty.value) {
    const ok = await confirm({
      title: 'Continue without saving',
      message: 'You have unsaved YAML edits. Continue without saving them?',
      confirmText: 'Continue Anyway',
      variant: 'warning',
    })
    if (!ok) return
  }
  submitting.value = true
  try {
    await store.submitYaml()
    router.push({ name: 'SetupVerify' })
  } catch (_) {
    // surfaces via store
  } finally {
    submitting.value = false
  }
}

async function onSkip() {
  submitting.value = true
  try {
    await store.skipStep('yaml_config')
    router.push({ name: 'SetupVerify' })
  } catch (_) {} finally {
    submitting.value = false
  }
}

async function onBack() {
  if (dirty.value) {
    const ok = await confirm({
      title: 'Leave without saving',
      message: 'You have unsaved YAML edits. Leave without saving?',
      confirmText: 'Leave',
      variant: 'warning',
    })
    if (!ok) return
  }
  router.push({ name: 'SetupServices' })
}

function onKeydown(ev) {
  if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 's') {
    ev.preventDefault()
    onSaveFile()
  }
}

onMounted(() => {
  refreshList().catch((err) => (editorError.value = _friendly(err)))
  window.addEventListener('keydown', onKeydown)
})
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))

// Clear the save-success toast as soon as the user types again.
watch(content, () => { editorSuccess.value = '' })
</script>

<template>
  <WizardCard
    title="Advanced Configuration (Optional)"
    subtitle="Review and customize your YAML config files. Default values work great for most setups — you can skip this step."
    step-label="Step 4 of 5  ·  Optional"
    width="960px"
  >
    <div class="wizard-callout">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#5b8cff" stroke-width="2" style="flex-shrink:0; margin-top: 2px;">
        <circle cx="12" cy="12" r="10" />
        <line x1="12" y1="8" x2="12" y2="12" />
        <line x1="12" y1="16" x2="12.01" y2="16" />
      </svg>
      <div>
        Edit <code>fastagent.config.yaml</code> or <code>fastagent.secrets.yaml</code>
        if you need non-default MCP servers or provider overrides. Changes are
        validated with <code>yaml.safe_load</code> and written atomically with a
        <code>.bak</code> rollback. You can also revisit this later from
        <strong>Settings → YAML Config</strong>.
      </div>
    </div>

    <div class="yaml-wrap">
      <aside class="file-list">
        <header>Config Files</header>
        <button
          v-for="f in files"
          :key="f.name"
          type="button"
          class="file-item"
          :class="{ active: activeName === f.name }"
          @click="selectFile(f.name)"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
          </svg>
          <span class="name">{{ f.filename }}</span>
          <span v-if="!f.exists" class="hint">new</span>
        </button>
      </aside>

      <section class="editor-col">
        <header class="editor-bar">
          <div class="file-meta">
            <span v-if="activeName" class="filename">
              {{ files.find((f) => f.name === activeName)?.filename }}
            </span>
            <span v-if="activeName" class="desc">
              {{ files.find((f) => f.name === activeName)?.description }}
            </span>
          </div>
          <div class="editor-actions">
            <span v-if="dirty" class="status-pill dirty">Unsaved</span>
            <span v-else-if="exists && activeName" class="status-pill clean">Saved</span>
            <button
              type="button"
              class="wizard-btn ghost small"
              :disabled="!dirty || saving"
              @click="onRevert"
            >
              Revert
            </button>
            <button
              type="button"
              class="wizard-btn primary small"
              :disabled="!dirty || saving || !activeName"
              @click="onSaveFile"
            >
              {{ saving ? 'Saving...' : 'Save' }}
              <span v-if="!saving" class="shortcut">⌘S</span>
            </button>
          </div>
        </header>

        <div class="editor-shell">
          <div v-if="loading" class="overlay">Loading...</div>
          <Codemirror
            v-else-if="activeName"
            v-model="content"
            :extensions="extensions"
            :style="{ height: '100%' }"
            placeholder="File is empty. Start typing YAML..."
          />
          <div v-else class="overlay">Select a file to edit.</div>
        </div>

        <footer class="editor-footer">
          <span v-if="editorError" class="msg error">{{ editorError }}</span>
          <span v-else-if="editorSuccess" class="msg ok">{{ editorSuccess }}</span>
          <span v-else-if="activeName" class="msg muted">
            {{ exists ? `${sizeBytes} bytes on disk` : 'File does not exist yet — save to create it.' }}
          </span>
        </footer>
      </section>
    </div>

    <div v-if="store.lastSubmitError" class="wizard-error">
      {{ store.lastSubmitError }}
    </div>

    <template #footer-left>
      <button type="button" class="wizard-btn ghost" @click="onBack">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="19" y1="12" x2="5" y2="12" />
          <polyline points="12 19 5 12 12 5" />
        </svg>
        Back
      </button>
    </template>
    <template #footer-right>
      <button type="button" class="wizard-btn ghost" :disabled="submitting" @click="onSkip">
        Skip for now
      </button>
      <button
        type="button"
        class="wizard-btn primary"
        :disabled="submitting"
        @click="onContinue"
      >
        {{ submitting ? 'Saving...' : 'Accept & Continue' }}
        <svg v-if="!submitting" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="5" y1="12" x2="19" y2="12" />
          <polyline points="12 5 19 12 12 19" />
        </svg>
      </button>
    </template>
  </WizardCard>
</template>

<style scoped>
.yaml-wrap {
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: 14px;
  margin-top: 4px;
}
.file-list {
  background: #0c0e15;
  border: 1px solid #243244;
  border-radius: 10px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  height: fit-content;
}
.file-list > header {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #555872;
  padding: 6px 4px;
}
.file-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: none;
  background: transparent;
  border-radius: 6px;
  color: #8b8fa3;
  font-family: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.file-item:hover { background: rgba(255,255,255,0.03); color: #c4c8d4; }
.file-item.active {
  background: rgba(59, 130, 246, 0.12);
  color: #3b82f6;
}
.file-item .name { flex: 1; }
.file-item .hint {
  font-size: 9px;
  color: #555872;
  text-transform: uppercase;
  padding: 2px 6px;
  border-radius: 999px;
  background: rgba(255,255,255,0.03);
}

.editor-col {
  display: flex;
  flex-direction: column;
  background: #0a0d14;
  border: 1px solid #243244;
  border-radius: 10px;
  overflow: hidden;
  min-height: 440px;
}
.editor-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-bottom: 1px solid #243244;
  background: #0c0e15;
}
.file-meta { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.filename {
  font-family: ui-monospace, 'SF Mono', Menlo, monospace;
  font-size: 12px;
  font-weight: 600;
  color: #f0f2f5;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.desc {
  font-size: 11px;
  color: #555872;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.editor-actions { display: flex; align-items: center; gap: 8px; }
.status-pill {
  font-size: 10px;
  font-weight: 600;
  padding: 3px 8px;
  border-radius: 999px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.status-pill.dirty { background: rgba(245, 158, 11, 0.1); color: #f59e0b; }
.status-pill.clean { background: rgba(34, 197, 94, 0.1); color: #22c55e; }

.wizard-btn.small {
  padding: 6px 10px;
  font-size: 12px;
}
.shortcut {
  font-size: 10px;
  opacity: 0.8;
  font-weight: 500;
  padding: 1px 4px;
  border-radius: 3px;
  background: rgba(255,255,255,0.15);
  margin-left: 4px;
}

.editor-shell {
  position: relative;
  flex: 1 1 auto;
  min-height: 360px;
  background: #0a0d14;
}
.overlay {
  position: absolute; inset: 0;
  display: grid; place-items: center;
  color: #555872;
  font-size: 13px;
}
.editor-footer {
  padding: 8px 14px;
  border-top: 1px solid #243244;
  background: #0c0e15;
  min-height: 34px;
  display: flex;
  align-items: center;
}
.msg { font-size: 11px; }
.msg.error { color: #ef4444; }
.msg.ok { color: #22c55e; }
.msg.muted { color: #555872; }

@media (max-width: 820px) {
  .yaml-wrap { grid-template-columns: 1fr; }
  .file-list { flex-direction: row; overflow-x: auto; }
  .file-list > header { display: none; }
}
</style>
