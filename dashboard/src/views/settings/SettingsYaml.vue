<script setup>
/**
 * Settings → YAML Config.
 *
 * Two-pane layout: file list on the left, CodeMirror editor on the right.
 * The backend enforces the allowlist, so the file list is just a UX nicety —
 * the browser never tries to open arbitrary paths.
 *
 * Save flow:
 *   1. user clicks Save
 *   2. backend does yaml.safe_load + atomic rename + backup
 *   3. we refetch the saved content so the buffer matches disk exactly.
 *
 * Unsaved-changes guard: we track a dirty flag against the last known good
 * content and warn before switching files or leaving the view.
 */
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { Codemirror } from 'vue-codemirror'
import { yaml } from '@codemirror/lang-yaml'
import { oneDark } from '@codemirror/theme-one-dark'
import { EditorView, lineNumbers, highlightActiveLine, keymap } from '@codemirror/view'
import { EditorState } from '@codemirror/state'
import { defaultKeymap, indentWithTab, history, historyKeymap } from '@codemirror/commands'
import { apiFetch, ApiError } from '../../api'
import { useConfirm } from '../../composables/useConfirm'

const { confirm } = useConfirm()

const files = ref([])
const activeName = ref(null)
const content = ref('')
const savedContent = ref('')
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const successMsg = ref('')
const exists = ref(false)
const sizeBytes = ref(0)

const extensions = [
  yaml(),
  oneDark,
  lineNumbers(),
  highlightActiveLine(),
  history(),
  keymap.of([...defaultKeymap, ...historyKeymap, indentWithTab]),
  EditorState.tabSize.of(2),
  EditorView.theme({
    '&': { fontSize: '13px', height: '100%' },
    '.cm-scroller': { fontFamily: "ui-monospace, 'SF Mono', Menlo, Consolas, monospace" },
    '.cm-gutters': { background: '#0a0d14', borderRight: '1px solid #1e2030' },
    '&.cm-focused': { outline: 'none' },
  }),
]

const dirty = computed(() => content.value !== savedContent.value)

async function refreshList() {
  const res = await apiFetch('/api/yaml/files')
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
  error.value = ''
  successMsg.value = ''
  loading.value = true
  try {
    const res = await apiFetch(`/api/yaml/${encodeURIComponent(name)}`)
    activeName.value = name
    content.value = res.content || ''
    savedContent.value = res.content || ''
    exists.value = !!res.exists
    sizeBytes.value = res.size || 0
  } catch (err) {
    error.value = _friendly(err)
  } finally {
    loading.value = false
  }
}

async function onSave() {
  if (!activeName.value || !dirty.value) return
  saving.value = true
  error.value = ''
  successMsg.value = ''
  try {
    const res = await apiFetch(`/api/yaml/${encodeURIComponent(activeName.value)}`, {
      method: 'PUT',
      body: JSON.stringify({ content: content.value }),
    })
    // Re-read from disk so we're 100% sure our buffer matches what was
    // persisted (and pick up any post-write normalisation if the backend
    // ever adds one).
    const fresh = await apiFetch(`/api/yaml/${encodeURIComponent(activeName.value)}`)
    content.value = fresh.content
    savedContent.value = fresh.content
    exists.value = true
    sizeBytes.value = res?.size ?? fresh.size
    successMsg.value = `${fresh.filename} saved (${sizeBytes.value} bytes).`
    // refresh list so "exists" badges catch up for newly-created files
    refreshList().catch(() => {})
  } catch (err) {
    error.value = _friendly(err)
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

function onBeforeUnload(e) {
  if (dirty.value) {
    e.preventDefault()
    e.returnValue = ''
  }
}

onMounted(() => {
  refreshList().catch((err) => (error.value = _friendly(err)))
  window.addEventListener('beforeunload', onBeforeUnload)
})
onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', onBeforeUnload)
})

// Keyboard shortcut: Cmd/Ctrl-S saves.
watch(
  () => ({ dirty: dirty.value, name: activeName.value }),
  () => {
    // placeholder; shortcut is registered below as a raw listener so it
    // survives across component re-renders.
  },
)
function onKeydown(ev) {
  if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 's') {
    ev.preventDefault()
    onSave()
  }
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
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
            class="btn ghost"
            :disabled="!dirty || saving"
            @click="onRevert"
          >
            Revert
          </button>
          <button
            type="button"
            class="btn primary"
            :disabled="!dirty || saving || !activeName"
            @click="onSave"
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
        <span v-if="error" class="msg error">{{ error }}</span>
        <span v-else-if="successMsg" class="msg ok">{{ successMsg }}</span>
        <span v-else-if="activeName" class="msg muted">
          {{ exists ? `${sizeBytes} bytes on disk` : 'File does not exist yet — save to create it.' }}
        </span>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.yaml-wrap {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: 16px;
  min-height: 640px;
}
.file-list {
  background: var(--bg-card, #111318);
  border: 1px solid var(--border, #1e2030);
  border-radius: 12px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.file-list > header {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-sub, #555872);
  padding: 8px 4px;
}
.file-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: none;
  background: transparent;
  border-radius: 6px;
  color: var(--text-nav, #8b8fa3);
  font-family: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.file-item:hover { background: rgba(255,255,255,0.03); color: var(--text-secondary, #c4c8d4); }
.file-item.active {
  background: rgba(59, 130, 246, 0.12);
  color: var(--accent-blue, #3b82f6);
}
.file-item .name { flex: 1; }
.file-item .hint {
  font-size: 10px;
  color: var(--text-sub, #555872);
  text-transform: uppercase;
  padding: 2px 6px;
  border-radius: 999px;
  background: rgba(255,255,255,0.03);
}

.editor-col {
  display: flex;
  flex-direction: column;
  background: var(--bg-card, #111318);
  border: 1px solid var(--border, #1e2030);
  border-radius: 12px;
  overflow: hidden;
}
.editor-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border, #1e2030);
  background: #0c0e15;
}
.file-meta { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.filename {
  font-family: ui-monospace, 'SF Mono', Menlo, monospace;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary, #f0f2f5);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.desc {
  font-size: 11px;
  color: var(--text-sub, #555872);
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

.btn {
  padding: 7px 14px;
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
  border-radius: 6px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-nav, #8b8fa3);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.btn.ghost:hover:not([disabled]) { color: var(--text-primary, #f0f2f5); background: rgba(255,255,255,0.04); }
.btn.primary {
  background: var(--accent-blue, #3b82f6);
  color: #ffffff;
  border-color: var(--accent-blue, #3b82f6);
}
.btn.primary:hover:not([disabled]) { background: #2f6cdc; }
.btn[disabled] { opacity: 0.45; cursor: not-allowed; }
.shortcut {
  font-size: 10px;
  opacity: 0.8;
  font-weight: 500;
  padding: 1px 4px;
  border-radius: 3px;
  background: rgba(255,255,255,0.15);
}

.editor-shell {
  position: relative;
  flex: 1 1 auto;
  min-height: 520px;
  background: #0a0d14;
}
.overlay {
  position: absolute; inset: 0;
  display: grid; place-items: center;
  color: var(--text-sub, #555872);
  font-size: 13px;
}
.editor-footer {
  padding: 10px 16px;
  border-top: 1px solid var(--border, #1e2030);
  background: #0c0e15;
  min-height: 40px;
  display: flex;
  align-items: center;
}
.msg { font-size: 12px; }
.msg.error { color: #ef4444; }
.msg.ok { color: #22c55e; }
.msg.muted { color: var(--text-sub, #555872); }

@media (max-width: 860px) {
  .yaml-wrap {
    grid-template-columns: 1fr;
  }
  .file-list { flex-direction: row; overflow-x: auto; }
  .file-list > header { display: none; }
}
</style>
