<script setup>
/**
 * Settings → YAML Config.
 *
 * Two-pane layout: file list on the left (grouped by core/secrets per the
 * backend's is_secret_file flag), CodeMirror editor on the right with
 * traffic-light chrome.
 *
 * Save flow (unchanged from previous version):
 *   1. user clicks Save (or ⌘S / Ctrl-S)
 *   2. backend does yaml.safe_load + atomic rename + backup
 *   3. we refetch the saved content so the buffer matches disk exactly.
 *
 * Unsaved-changes guard: we track a dirty flag against the last known good
 * content and warn before switching files or leaving the view.
 */
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
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
const activeKind = ref('fastagent')
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
    '&': { fontSize: '12.5px', height: '100%' },
    '.cm-scroller': { fontFamily: "var(--font-mono), ui-monospace, 'SF Mono', Menlo, Consolas, monospace" },
    '.cm-gutters': { background: 'var(--bg-0)', borderRight: '1px solid var(--border)' },
    '&.cm-focused': { outline: 'none' },
  }),
]

const dirty = computed(() => content.value !== savedContent.value)
const lineCount = computed(() => (content.value ? content.value.split('\n').length : 0))
const dirtyLineCount = computed(() => {
  if (!dirty.value) return 0
  const cur = content.value.split('\n')
  const sav = savedContent.value.split('\n')
  let count = Math.abs(cur.length - sav.length)
  const n = Math.min(cur.length, sav.length)
  for (let i = 0; i < n; i++) if (cur[i] !== sav[i]) count++
  return count
})
const activeFile = computed(() =>
  files.value.find(
    (f) => f.name === activeName.value && f.kind === activeKind.value,
  ) || null,
)

// Group files by kind. Two surfaces here:
//   • fastagent — fastagent.config.yaml + fastagent.secrets.yaml (handled
//     by /api/yaml/{name}).
//   • team-template — backend/team_templates/*.yaml (handled by
//     /api/team-templates/{name}; spawned teams use these as factory
//     defaults — edits do NOT touch already-running teams, see Running
//     Templates tab for that).
// We keep the editor + chrome identical and just branch the load/save URLs.
const groupedFiles = computed(() => {
  const core = []
  const secrets = []
  const teamTemplates = []
  for (const f of files.value) {
    if (f.kind === 'team-template') teamTemplates.push(f)
    else if (f.is_secret_file) secrets.push(f)
    else core.push(f)
  }
  return [
    { label: 'FAST-AGENT', items: core },
    { label: 'SECRETS', items: secrets },
    { label: 'TEAM TEMPLATES', items: teamTemplates },
  ].filter((g) => g.items.length)
})

function _fileKey(f) {
  // Unique key across surfaces — both APIs use "name" as their stem so we
  // namespace by kind to avoid a collision if a team template is ever
  // named "config" or "secrets".
  return `${f.kind}:${f.name}`
}

async function refreshList() {
  // Fetch both surfaces in parallel; surface a per-source failure rather
  // than letting one outage hide the other (matches the rest of the
  // backend's defensive listing patterns).
  const [coreRes, tmplRes] = await Promise.allSettled([
    apiFetch('/api/yaml/files'),
    apiFetch('/api/team-templates'),
  ])
  const coreFiles = (coreRes.status === 'fulfilled' ? coreRes.value?.files : []) || []
  const tmplFiles = (tmplRes.status === 'fulfilled' ? tmplRes.value?.templates : []) || []
  files.value = [
    ...coreFiles.map((f) => ({ ...f, kind: 'fastagent' })),
    ...tmplFiles.map((f) => ({
      ...f,
      kind: 'team-template',
      // Match the chrome description used by /api/yaml/files
      description: f.description || `Team template — ${f.display_name}`,
      // Factory templates are never secret files; surfaces the lock-icon
      // logic that already exists in the file tree.
      is_secret_file: false,
    })),
  ]
  if (!activeName.value && files.value.length) {
    await selectFile(files.value[0])
  }
}

function _endpointFor(file) {
  if (!file) return null
  return file.kind === 'team-template'
    ? `/api/team-templates/${encodeURIComponent(file.name)}`
    : `/api/yaml/${encodeURIComponent(file.name)}`
}

async function selectFile(file) {
  // Backwards-compat: existing callers pass the bare name. Resolve it
  // against the current list so we don't have to thread the kind through
  // every click handler.
  const target = typeof file === 'string'
    ? files.value.find((f) => f.name === file && f.kind === 'fastagent')
      || files.value.find((f) => f.name === file)
    : file
  if (!target) return
  const targetKey = _fileKey(target)
  const currentKey = activeFile.value ? _fileKey(activeFile.value) : null
  if (targetKey === currentKey) return
  if (dirty.value) {
    const ok = await confirm({
      title: 'Discard unsaved changes',
      message: `You have ${dirtyLineCount.value} edited line(s) in the current file. Discard and switch?`,
      confirmText: 'Discard & Switch',
      variant: 'warning',
    })
    if (!ok) return
  }
  error.value = ''
  successMsg.value = ''
  loading.value = true
  try {
    const res = await apiFetch(_endpointFor(target))
    activeName.value = target.name
    activeKind.value = target.kind
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
  const target = activeFile.value
  if (!target) return
  saving.value = true
  error.value = ''
  successMsg.value = ''
  try {
    const url = _endpointFor(target)
    const res = await apiFetch(url, {
      method: 'PUT',
      body: JSON.stringify({ content: content.value }),
    })
    const fresh = await apiFetch(url)
    content.value = fresh.content
    savedContent.value = fresh.content
    exists.value = true
    sizeBytes.value = res?.size ?? fresh.size
    successMsg.value = `${fresh.filename} saved (${sizeBytes.value} bytes).`
    if (target.kind === 'team-template') {
      // Surface the decision-2026-05-17 invariant inline: factory yaml edits
      // are NEVER auto-applied to running teams. The user opens Running
      // Templates → <session> → "Reset role to yaml" / "Reload" to apply.
      successMsg.value += ' Edits to factory yaml do NOT touch running teams — open Running Templates to apply.'
    }
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
    <!-- File tree -->
    <aside class="file-tree">
      <div class="tree-eyebrow">CONFIG FILES</div>
      <template v-for="group in groupedFiles" :key="group.label">
        <div class="tree-section">{{ group.label }}</div>
        <button
          v-for="f in group.items"
          :key="`${f.kind}:${f.name}`"
          type="button"
          class="file-item"
          :class="{
            active: activeName === f.name && activeKind === f.kind,
            dirty: activeName === f.name && activeKind === f.kind && dirty,
          }"
          @click="selectFile(f)"
        >
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
          </svg>
          <code class="name">{{ f.filename }}</code>
          <span v-if="f.is_secret_file" class="lock-icon" title="Contains secrets">🔒</span>
          <span v-if="activeName === f.name && dirty" class="dirty-dot">●</span>
          <span v-if="!f.exists" class="new-tag">NEW</span>
        </button>
      </template>

      <div class="tree-spacer" />
      <div class="safety-note">
        <div class="tree-section" style="padding: 0 0 4px;">SAFETY</div>
        Validated with <code>yaml.safe_load</code>; <code>.bak</code> backup written before each save.
      </div>
    </aside>

    <!-- Editor column -->
    <section class="editor-col">
      <!-- Window chrome with traffic-light dots + filename -->
      <header class="editor-chrome">
        <div class="traffic">
          <span class="dot dot-red" />
          <span class="dot dot-amber" />
          <span class="dot dot-green" />
        </div>
        <div v-if="activeFile" class="chrome-title">
          <code>{{ activeFile.filename }}</code>
          <span class="chrome-desc">{{ activeFile.description }}</span>
        </div>
        <div class="chrome-actions">
          <span v-if="dirty" class="status-pill dirty">● UNSAVED · {{ dirtyLineCount }} LINE{{ dirtyLineCount === 1 ? '' : 'S' }}</span>
          <span v-else-if="exists && activeName" class="status-pill clean">✓ SAVED</span>
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
            {{ saving ? 'Saving…' : 'Save' }}
            <kbd v-if="!saving" class="shortcut">⌘S</kbd>
          </button>
        </div>
      </header>

      <!-- Editor body -->
      <div class="editor-shell">
        <div v-if="loading" class="overlay">Loading…</div>
        <Codemirror
          v-else-if="activeName"
          v-model="content"
          :extensions="extensions"
          :style="{ height: '100%' }"
          placeholder="File is empty. Start typing YAML…"
        />
        <div v-else class="overlay">Select a file to edit.</div>
      </div>

      <!-- Footer status bar -->
      <footer class="editor-footer">
        <span v-if="error" class="msg error">
          <span class="msg-icon">✕</span>
          <strong>YAML parse error</strong> — {{ error }}
        </span>
        <span v-else-if="successMsg" class="msg ok">
          <span class="msg-icon">✓</span> {{ successMsg }}
        </span>
        <span v-else-if="dirty" class="msg dirty">
          <span class="msg-icon">●</span> {{ dirtyLineCount }} line edited · ⌘S to save · backend hot-reloads
        </span>
        <span v-else-if="activeName" class="msg muted">
          {{ exists ? `${sizeBytes} bytes on disk · backup at ${activeFile?.filename}.bak` : 'File does not exist yet — save to create it.' }}
        </span>
        <span class="meta-pill">
          YAML · UTF-8 · LF · {{ lineCount }} lines
        </span>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.yaml-wrap {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: 14px;
  min-height: 640px;
}

/* ── File tree ────────────────────────────────────────────────────── */
.file-tree {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow: hidden;
}
.tree-eyebrow {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-muted);
  padding: 6px 8px 4px;
}
.tree-section {
  font-family: var(--font-mono);
  font-size: 9.5px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-subtle);
  padding: 10px 8px 4px;
}
.file-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border: none;
  background: transparent;
  border-left: 2px solid transparent;
  border-radius: var(--r-sm);
  color: var(--text-dim);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 0.12s, color 0.12s, border-color 0.12s;
}
.file-item:hover { background: rgba(255,255,255,0.03); color: var(--text); }
.file-item.active {
  background: var(--primary-bg);
  border-left-color: var(--primary);
  color: var(--text);
}
.file-item .name {
  flex: 1;
  font-family: var(--font-mono);
  font-size: 11.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.file-item .lock-icon { font-size: 10px; }
.file-item .dirty-dot {
  font-size: 10px;
  color: var(--warning);
}
.file-item .new-tag {
  font-family: var(--font-mono);
  font-size: 9px;
  letter-spacing: 0.08em;
  color: var(--text-subtle);
  padding: 1px 5px;
  border-radius: 3px;
  background: var(--bg-3);
}
.tree-spacer { flex: 1; }
.safety-note {
  margin-top: 10px;
  padding: 10px;
  border-radius: var(--r-sm);
  background: var(--bg-1);
  border: 1px solid var(--border);
  font-size: 10.5px;
  color: var(--text-muted);
  line-height: 1.55;
}
.safety-note code {
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: 10.5px;
}

/* ── Editor column ────────────────────────────────────────────────── */
.editor-col {
  display: flex;
  flex-direction: column;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  overflow: hidden;
  min-height: 0;
}

.editor-chrome {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 14px;
  background: var(--bg-1);
  border-bottom: 1px solid var(--border);
}
.traffic {
  display: flex;
  gap: 6px;
  align-items: center;
}
.dot {
  width: 11px; height: 11px;
  border-radius: 50%;
}
.dot-red    { background: #ff5f57; }
.dot-amber  { background: #febc2e; }
.dot-green  { background: #28c840; }
.chrome-title {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}
.chrome-title code {
  font-family: var(--font-mono);
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chrome-desc {
  font-size: 10.5px;
  color: var(--text-subtle);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chrome-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.status-pill {
  padding: 3px 8px;
  border-radius: 999px;
  font-family: var(--font-mono);
  font-size: 9.5px;
  letter-spacing: 0.06em;
}
.status-pill.dirty { background: var(--warning-bg); color: var(--warning); }
.status-pill.clean { background: var(--success-bg); color: var(--success); }

.btn {
  padding: 7px 12px;
  font-family: inherit;
  font-size: 12px;
  font-weight: 500;
  border-radius: var(--r-sm);
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.btn.ghost { border-color: var(--border-strong); }
.btn.ghost:hover:not([disabled]) { color: var(--text); background: rgba(255,255,255,0.04); }
.btn.primary {
  background: var(--primary); color: #ffffff;
  border-color: var(--primary);
}
.btn.primary:hover:not([disabled]) { background: var(--primary-active); border-color: var(--primary-active); }
.btn[disabled] { opacity: 0.45; cursor: not-allowed; }
.shortcut {
  font-family: var(--font-mono);
  font-size: 9.5px;
  font-weight: 500;
  padding: 1px 5px;
  border-radius: 3px;
  background: rgba(255,255,255,0.18);
}

.editor-shell {
  position: relative;
  flex: 1 1 auto;
  min-height: 520px;
  background: var(--bg-0);
}
.overlay {
  position: absolute; inset: 0;
  display: grid; place-items: center;
  color: var(--text-subtle);
  font-size: 13px;
}

/* ── Footer ───────────────────────────────────────────────────────── */
.editor-footer {
  padding: 8px 14px;
  border-top: 1px solid var(--border);
  background: var(--bg-1);
  min-height: 38px;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11.5px;
}
.msg { display: inline-flex; align-items: center; gap: 6px; }
.msg.error  { color: var(--danger); }
.msg.error strong { color: var(--danger); }
.msg.ok     { color: var(--success); }
.msg.dirty  { color: var(--warning); }
.msg.muted  { color: var(--text-muted); }
.msg.muted code { color: var(--accent-warm); font-family: var(--font-mono); }
.msg-icon { font-size: 11px; }
.meta-pill {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-subtle);
}

@media (max-width: 768px) {
  .yaml-wrap {
    grid-template-columns: 1fr;
  }
  /* Drop the 220px cap on mobile: with the layout stacked vertically
     the tree is the only thing competing for that vertical slot, and
     220px was silently clipping the Safety section's body (overflow:
     hidden + no scrollbar means content just disappears). The page
     already scrolls so letting the tree grow natural-height is fine. */
  .file-tree { max-height: none; }
  /* Cut the editor height — 520px on a 640px-tall phone left no room
     for the tree above OR scrolling content elsewhere. 320 keeps the
     editor usable while leaving viewport breathing room. */
  .editor-shell { min-height: 320px; }

  /* Chrome row (traffic lights + filename + status + Revert + Save+kbd)
     overflowed at narrow widths because nothing wrapped. Allow it to
     break: filename row 1, actions row 2. Bring this wrap-at-mobile
     rule up to the 768 unified breakpoint (was previously gated at
     600px, leaving 600-768 in an ugly squeezed state). */
  .editor-chrome { flex-wrap: wrap; }
  .chrome-actions { width: 100%; justify-content: flex-end; }
  /* The ⌘S shortcut hint is irrelevant on touch — hide so the Save
     button isn't artificially widened by a chrome that doesn't apply. */
  .chrome-actions kbd { display: none; }
  /* File tree rows: 28px-tall list items were below the 40px tap
     target floor. Bump padding so each row is comfortably tappable. */
  .file-item { padding: 12px 12px; }
}
</style>
