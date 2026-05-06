<script setup>
/**
 * SkillEditorModal — Create / edit a skill (SKILL.md) with markdown preview.
 *
 * Dual-mode:
 *   • mode="create" — POSTs a brand-new skill. Name input is editable; the
 *     content area is pre-filled with a template.
 *   • mode="edit"   — PUTs an existing skill. Name is locked (editor uses the
 *     directory name as the immutable key); content area is loaded from disk
 *     with optimistic locking via mtime_ns.
 *
 * View modes (toggle in header):
 *   • Source — editor only (CodeMirror, full width)
 *   • Split  — editor + live preview side-by-side (≥768px)
 *   • Preview — rendered markdown only
 *
 * The editor stores the *raw* SKILL.md content (frontmatter + body) so power
 * users can hand-tune frontmatter. The preview strips the frontmatter into a
 * compact metadata box and renders the body with the existing
 * MarkdownRenderer pipeline.
 *
 * Conflict handling: if the disk mtime has moved on between GET and PUT, the
 * backend returns 409. We surface that as a "Reload" prompt instead of
 * overwriting silently.
 */
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { Codemirror } from 'vue-codemirror'
import { markdown } from '@codemirror/lang-markdown'
import { oneDark } from '@codemirror/theme-one-dark'
import { EditorView, lineNumbers, highlightActiveLine, keymap } from '@codemirror/view'
import { EditorState } from '@codemirror/state'
import { defaultKeymap, indentWithTab, history, historyKeymap } from '@codemirror/commands'
import { load as yamlLoad } from 'js-yaml'
import { apiFetch, ApiError } from '../../api'
import { useConfirm } from '../../composables/useConfirm'
import MarkdownRenderer from '../MarkdownRenderer.vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
  mode: { type: String, default: 'edit' }, // 'create' | 'edit'
  skillName: { type: String, default: '' }, // required when mode === 'edit'
})
const emit = defineEmits(['close', 'saved'])

const { confirm } = useConfirm()

// ----- Editor state ------------------------------------------------------

const content = ref('')
const savedContent = ref('')
const mtimeNs = ref(null)
const isBuiltin = ref(false)
const usedBy = ref([])
const description = ref('')

// Create-mode form
const newName = ref('')
const nameError = ref('')

// UX state
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const successMsg = ref('')
const conflict = ref(false)
const viewMode = ref('split') // 'source' | 'split' | 'preview'
const isMobile = ref(false)

// ----- CodeMirror config -------------------------------------------------

const extensions = [
  markdown(),
  oneDark,
  lineNumbers(),
  highlightActiveLine(),
  history(),
  keymap.of([...defaultKeymap, ...historyKeymap, indentWithTab]),
  EditorState.tabSize.of(2),
  EditorView.lineWrapping,
  EditorView.theme({
    '&': { fontSize: '13px', height: '100%' },
    '.cm-scroller': { fontFamily: "ui-monospace, 'SF Mono', Menlo, Consolas, monospace" },
    '.cm-gutters': { background: '#0a0d14', borderRight: '1px solid #1e2030' },
    '&.cm-focused': { outline: 'none' },
  }),
]

// ----- Derived ------------------------------------------------------------

const dirty = computed(() => content.value !== savedContent.value)

const displayName = computed(() =>
  props.mode === 'create' ? (newName.value || 'new-skill') : props.skillName
)

// Frontmatter parsing for the preview pane. Uses js-yaml (already shipping
// for mock-backend dev tooling — runtime cost is small) so multi-line YAML
// scalars (`description: >`, `description: |`) render correctly. Best-effort:
// if it fails, show a banner but still render the body raw so the user can
// fix it.
const parsed = computed(() => {
  const text = content.value
  const m = text.match(/^---\s*\n([\s\S]*?)\n---\s*(?:\n|$)/)
  if (!m) {
    return { error: 'Missing frontmatter (--- ... ---)', frontmatter: {}, body: text }
  }
  const yamlText = m[1]
  const body = text.slice(m[0].length)
  let fm = {}
  let parseErr = null
  try {
    const parsed = yamlLoad(yamlText)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      // Coerce values to strings for the metadata table — js-yaml may return
      // arrays/objects for nested fields, which the .sk-fm-val span can't
      // render natively. Stringify nested structures with a tiny inline YAML
      // dump for readability.
      for (const [k, v] of Object.entries(parsed)) {
        if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
          fm[k] = String(v)
        } else if (v == null) {
          fm[k] = ''
        } else {
          fm[k] = JSON.stringify(v)
        }
      }
    }
  } catch (e) {
    parseErr = e?.message || String(e)
  }
  return { error: parseErr, frontmatter: fm, body }
})

const previewBody = computed(() => parsed.value.body || '')

// Validate skill name on the fly for create mode.
const NAME_RE = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/
watch(newName, (v) => {
  if (props.mode !== 'create') return
  if (!v) {
    nameError.value = ''
  } else if (!NAME_RE.test(v)) {
    nameError.value = 'Lowercase letters, digits, and hyphens only (no leading/trailing hyphen).'
  } else if (['con', 'prn', 'aux', 'nul', '_builtin'].includes(v.toLowerCase())) {
    nameError.value = 'Reserved name.'
  } else {
    nameError.value = ''
  }
})

// Keep `newName` reflected in template content so the immutable-name check
// passes server-side without forcing the user to retype it. Only for create.
watch(newName, (v) => {
  if (props.mode !== 'create') return
  // Replace the `name:` line in frontmatter if present.
  content.value = content.value.replace(
    /^(---\s*\n(?:[^\n]*\n)*?name:\s*)([^\n]*)/m,
    `$1${v}`,
  )
})

const canSave = computed(() => {
  if (saving.value) return false
  if (props.mode === 'create') {
    return !!newName.value && !nameError.value && !!content.value
  }
  return dirty.value
})

// ----- Lifecycle ----------------------------------------------------------

async function loadSkill() {
  loading.value = true
  error.value = ''
  conflict.value = false
  try {
    const res = await apiFetch(`/api/skills/${encodeURIComponent(props.skillName)}`)
    content.value = res.content || ''
    savedContent.value = res.content || ''
    mtimeNs.value = res.mtime_ns
    isBuiltin.value = !!res.is_builtin
    usedBy.value = res.used_by || []
    description.value = res.description || ''
  } catch (err) {
    error.value = _friendly(err)
  } finally {
    loading.value = false
  }
}

async function loadTemplate() {
  loading.value = true
  error.value = ''
  try {
    const res = await apiFetch('/api/skills/_template')
    content.value = res.content || ''
    savedContent.value = '' // Anything counts as dirty for create.
  } catch (err) {
    error.value = _friendly(err)
  } finally {
    loading.value = false
  }
}

async function onSave() {
  if (!canSave.value) return
  saving.value = true
  error.value = ''
  successMsg.value = ''
  try {
    let res
    if (props.mode === 'create') {
      res = await apiFetch('/api/skills', {
        method: 'POST',
        body: JSON.stringify({ name: newName.value, content: content.value }),
      })
    } else {
      res = await apiFetch(`/api/skills/${encodeURIComponent(props.skillName)}`, {
        method: 'PUT',
        body: JSON.stringify({ content: content.value, expected_mtime_ns: mtimeNs.value }),
      })
    }
    // The backend persists exactly what we sent — anchor `savedContent` to
    // the buffer rather than the response, so the dirty flag flips back to
    // clean even if the response trims trailing whitespace.
    savedContent.value = content.value
    mtimeNs.value = res.mtime_ns
    successMsg.value = `${res.name} saved.`
    emit('saved', res)
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      conflict.value = true
      error.value = _friendly(err)
    } else {
      error.value = _friendly(err)
    }
  } finally {
    saving.value = false
  }
}

async function onReloadFromDisk() {
  conflict.value = false
  await loadSkill()
}

async function onClose() {
  if (dirty.value) {
    const ok = await confirm({
      title: 'Discard unsaved changes',
      message: 'You have unsaved changes. Discard them and close?',
      confirmText: 'Discard',
      variant: 'warning',
    })
    if (!ok) return
  }
  emit('close')
}

function _friendly(err) {
  if (err instanceof ApiError && err.body && typeof err.body === 'object') {
    const detail = err.body.detail
    if (detail && typeof detail === 'object') {
      const parts = [detail.message]
      if (detail.error) parts.push(detail.error)
      return parts.filter(Boolean).join(' — ')
    }
    if (typeof detail === 'string') return detail
  }
  return err?.message || String(err)
}

// ----- Keyboard shortcut -------------------------------------------------

function onKeydown(ev) {
  if (!props.visible) return
  if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 's') {
    ev.preventDefault()
    onSave()
  } else if (ev.key === 'Escape') {
    ev.preventDefault()
    onClose()
  }
}

function onBeforeUnload(ev) {
  if (!props.visible || !dirty.value) return
  ev.preventDefault()
  ev.returnValue = ''
}

function checkMobile() {
  isMobile.value = window.innerWidth < 768
  // Force preview-only UI to be readable on mobile by snapping out of split.
  if (isMobile.value && viewMode.value === 'split') viewMode.value = 'source'
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('beforeunload', onBeforeUnload)
  window.addEventListener('resize', checkMobile)
  checkMobile()
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('beforeunload', onBeforeUnload)
  window.removeEventListener('resize', checkMobile)
})

// (Re)load whenever the modal opens or the target skill changes.
watch(
  () => [props.visible, props.mode, props.skillName],
  async ([vis, mode]) => {
    if (!vis) return
    successMsg.value = ''
    error.value = ''
    conflict.value = false
    if (mode === 'create') {
      newName.value = ''
      nameError.value = ''
      mtimeNs.value = null
      isBuiltin.value = false
      usedBy.value = []
      description.value = ''
      await loadTemplate()
    } else {
      await loadSkill()
    }
    await nextTick()
  },
  { immediate: true },
)
</script>

<template>
  <Teleport to="body">
    <Transition name="sk-modal">
      <div v-if="visible" class="sk-overlay" @click.self="onClose">
        <div class="sk-card" :class="{ 'sk-mobile': isMobile }">
          <!-- Header -->
          <header class="sk-header">
            <div class="sk-title-wrap">
              <span class="sk-icon">⚡</span>
              <div>
                <h3 class="sk-title">
                  {{ mode === 'create' ? 'Create skill' : displayName }}
                  <span v-if="isBuiltin" class="sk-badge sk-badge-builtin" title="Ships with Jarvis. Editable, but cannot be deleted.">Built-in</span>
                </h3>
                <p v-if="mode === 'edit' && description" class="sk-subtitle">{{ description }}</p>
              </div>
            </div>
            <div class="sk-header-actions">
              <div v-if="!isMobile" class="sk-view-toggle" role="tablist">
                <button
                  type="button"
                  :class="{ active: viewMode === 'source' }"
                  @click="viewMode = 'source'"
                  role="tab"
                >Source</button>
                <button
                  type="button"
                  :class="{ active: viewMode === 'split' }"
                  @click="viewMode = 'split'"
                  role="tab"
                >Split</button>
                <button
                  type="button"
                  :class="{ active: viewMode === 'preview' }"
                  @click="viewMode = 'preview'"
                  role="tab"
                >Preview</button>
              </div>
              <div v-else class="sk-view-toggle" role="tablist">
                <button
                  type="button"
                  :class="{ active: viewMode === 'source' }"
                  @click="viewMode = 'source'"
                  role="tab"
                >Source</button>
                <button
                  type="button"
                  :class="{ active: viewMode === 'preview' }"
                  @click="viewMode = 'preview'"
                  role="tab"
                >Preview</button>
              </div>
              <button class="sk-close" @click="onClose" aria-label="Close">×</button>
            </div>
          </header>

          <!-- Used-by banner (edit mode, with refs) -->
          <div v-if="mode === 'edit' && usedBy.length" class="sk-banner sk-banner-info">
            <strong>Used by {{ usedBy.length }} agent{{ usedBy.length === 1 ? '' : 's' }}:</strong>
            <span>{{ usedBy.join(', ') }}</span>
            <span class="sk-banner-note">Changes apply to all of them.</span>
          </div>

          <!-- Conflict banner -->
          <div v-if="conflict" class="sk-banner sk-banner-warn">
            <strong>Conflict:</strong>
            <span>{{ error }}</span>
            <button class="sk-banner-btn" @click="onReloadFromDisk">Reload</button>
          </div>

          <!-- Create-mode name input -->
          <div v-if="mode === 'create'" class="sk-create-form">
            <label class="sk-label">
              Skill name
              <input
                v-model="newName"
                type="text"
                placeholder="my-skill"
                class="sk-name-input"
                :class="{ invalid: nameError }"
                autocomplete="off"
                spellcheck="false"
              />
            </label>
            <p v-if="nameError" class="sk-input-error">{{ nameError }}</p>
            <p v-else class="sk-input-hint">Lowercase letters, digits, and hyphens. Becomes the skill's directory name.</p>
          </div>

          <!-- Frontmatter preview parse error (preview pane only) -->
          <div
            v-if="(viewMode === 'preview' || viewMode === 'split') && parsed.error"
            class="sk-banner sk-banner-warn"
          >
            <strong>Frontmatter parse error:</strong>
            <span>{{ parsed.error }}</span>
          </div>

          <!-- Body: editor + preview -->
          <div class="sk-body" :class="`sk-body-${viewMode}`">
            <div v-if="viewMode !== 'preview'" class="sk-pane sk-pane-source">
              <div v-if="loading" class="sk-loading">Loading…</div>
              <Codemirror
                v-else
                v-model="content"
                :extensions="extensions"
                :indent-with-tab="true"
                :tab-size="2"
                placeholder="---&#10;name: my-skill&#10;description: …&#10;---"
              />
            </div>
            <div v-if="viewMode !== 'source'" class="sk-pane sk-pane-preview">
              <div v-if="parsed.frontmatter && Object.keys(parsed.frontmatter).length" class="sk-fm-card">
                <div v-for="(v, k) in parsed.frontmatter" :key="k" class="sk-fm-row">
                  <span class="sk-fm-key">{{ k }}</span>
                  <span class="sk-fm-val">{{ v }}</span>
                </div>
              </div>
              <div class="sk-md">
                <MarkdownRenderer
                  v-if="previewBody.trim()"
                  :content="previewBody"
                  content-type="markdown"
                />
                <div v-else class="sk-empty-preview">Empty preview — start typing to see rendered markdown.</div>
              </div>
            </div>
          </div>

          <!-- Footer -->
          <footer class="sk-footer">
            <div class="sk-status">
              <span v-if="dirty && !saving" class="sk-pill sk-pill-dirty">Unsaved</span>
              <span v-else-if="saving" class="sk-pill sk-pill-saving">Saving…</span>
              <span v-else-if="successMsg" class="sk-pill sk-pill-saved">{{ successMsg }}</span>
              <span v-else-if="!loading" class="sk-pill sk-pill-clean">Saved</span>
              <span v-if="error && !conflict" class="sk-error">{{ error }}</span>
            </div>
            <div class="sk-footer-actions">
              <button class="sk-btn sk-btn-secondary" @click="onClose">Cancel</button>
              <button
                class="sk-btn sk-btn-primary"
                :disabled="!canSave"
                @click="onSave"
              >
                {{ mode === 'create' ? 'Create' : 'Save' }} <span class="sk-kbd">⌘S</span>
              </button>
            </div>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.sk-overlay {
  position: fixed;
  inset: 0;
  z-index: 9999;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
}

.sk-card {
  background: #0c0e15;
  border: 1px solid #1a1d2e;
  border-radius: 16px;
  width: 1100px;
  max-width: 95vw;
  height: 80vh;
  max-height: 800px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.03);
}
.sk-card.sk-mobile {
  width: 100vw;
  height: 100vh;
  max-width: 100vw;
  max-height: 100vh;
  border-radius: 0;
  border: none;
}

/* Header */
.sk-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid #1a1d2e;
  gap: 12px;
}
.sk-title-wrap { display: flex; align-items: center; gap: 12px; min-width: 0; }
.sk-icon {
  width: 36px; height: 36px;
  display: flex; align-items: center; justify-content: center;
  background: rgba(59, 130, 246, 0.1);
  border: 1px solid rgba(59, 130, 246, 0.2);
  border-radius: 10px; font-size: 18px;
}
.sk-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: #f0f2f5;
  display: flex;
  align-items: center;
  gap: 10px;
}
.sk-subtitle {
  margin: 2px 0 0;
  font-size: 12px;
  color: #8b8fa3;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 600px;
}
.sk-badge {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 2px 7px;
  border-radius: 4px;
}
.sk-badge-builtin {
  background: rgba(59, 130, 246, 0.15);
  color: #60a5fa;
  border: 1px solid rgba(59, 130, 246, 0.3);
}

.sk-header-actions { display: flex; align-items: center; gap: 10px; }

.sk-view-toggle {
  display: flex;
  background: #111318;
  border: 1px solid #1a1d2e;
  border-radius: 8px;
  padding: 2px;
}
.sk-view-toggle button {
  padding: 5px 12px;
  background: transparent;
  border: none;
  color: #8b8fa3;
  font-size: 12px;
  font-weight: 500;
  border-radius: 6px;
  cursor: pointer;
}
.sk-view-toggle button:hover:not(.active) { color: #c4c8d4; }
.sk-view-toggle button.active {
  background: #1e2233;
  color: #f0f2f5;
}

.sk-close {
  background: transparent;
  border: 1px solid #1a1d2e;
  color: #8b8fa3;
  width: 32px; height: 32px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 22px;
  line-height: 1;
}
.sk-close:hover { color: #f0f2f5; background: #1e2233; }

/* Banners */
.sk-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 20px;
  font-size: 13px;
  border-bottom: 1px solid #1a1d2e;
}
.sk-banner-info {
  background: rgba(59, 130, 246, 0.06);
  color: #c4c8d4;
}
.sk-banner-info strong { color: #60a5fa; }
.sk-banner-note { color: #8b8fa3; margin-left: auto; font-size: 12px; }
.sk-banner-warn {
  background: rgba(245, 158, 11, 0.08);
  color: #fbbf24;
}
.sk-banner-warn strong { color: #fbbf24; }
.sk-banner-btn {
  margin-left: auto;
  background: rgba(245, 158, 11, 0.15);
  color: #fbbf24;
  border: 1px solid rgba(245, 158, 11, 0.3);
  padding: 4px 12px;
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
}
.sk-banner-btn:hover { background: rgba(245, 158, 11, 0.25); }

/* Create form */
.sk-create-form {
  padding: 14px 20px;
  border-bottom: 1px solid #1a1d2e;
  background: #0a0d14;
}
.sk-label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  color: #8b8fa3;
  font-weight: 500;
}
.sk-name-input {
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 8px;
  padding: 9px 12px;
  color: #f0f2f5;
  font-family: ui-monospace, 'SF Mono', monospace;
  font-size: 13px;
}
.sk-name-input:focus {
  outline: none;
  border-color: #3b82f6;
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
}
.sk-name-input.invalid {
  border-color: #ef4444;
  box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.1);
}
.sk-input-error {
  margin: 6px 0 0;
  color: #f87171;
  font-size: 12px;
}
.sk-input-hint {
  margin: 6px 0 0;
  color: #555872;
  font-size: 12px;
}

/* Body / panes */
.sk-body {
  flex: 1;
  display: flex;
  min-height: 0;
  background: #0a0d14;
}
.sk-body-source .sk-pane-source { flex: 1; }
.sk-body-preview .sk-pane-preview { flex: 1; }
.sk-body-split .sk-pane-source { flex: 1; border-right: 1px solid #1a1d2e; }
.sk-body-split .sk-pane-preview { flex: 1; }
.sk-pane { min-width: 0; min-height: 0; display: flex; flex-direction: column; }
.sk-pane-source :deep(.cm-editor) { height: 100%; }
.sk-pane-source :deep(.v-codemirror) { flex: 1; min-height: 0; }
.sk-pane-preview {
  overflow-y: auto;
  padding: 16px 20px;
  color: #c4c8d4;
}
.sk-fm-card {
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 8px;
  padding: 10px 14px;
  margin-bottom: 16px;
  font-family: ui-monospace, 'SF Mono', monospace;
  font-size: 12px;
}
.sk-fm-row {
  display: flex;
  gap: 10px;
  padding: 2px 0;
}
.sk-fm-key { color: #8b8fa3; font-weight: 600; min-width: 96px; }
.sk-fm-val { color: #c4c8d4; }
.sk-md :deep(h1) { font-size: 22px; }
.sk-md :deep(h2) { font-size: 18px; }
.sk-md :deep(h3) { font-size: 15px; }
.sk-empty-preview {
  color: #555872;
  font-size: 13px;
  font-style: italic;
}
.sk-loading {
  flex: 1; display: flex; align-items: center; justify-content: center;
  color: #8b8fa3;
}

/* Footer */
.sk-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  border-top: 1px solid #1a1d2e;
  background: #0c0e15;
  gap: 12px;
  flex-wrap: wrap;
}
.sk-status { display: flex; align-items: center; gap: 12px; min-width: 0; }
.sk-pill {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 3px 8px;
  border-radius: 4px;
}
.sk-pill-dirty {
  background: rgba(245, 158, 11, 0.12);
  color: #fbbf24;
}
.sk-pill-saving {
  background: rgba(59, 130, 246, 0.12);
  color: #60a5fa;
}
.sk-pill-saved, .sk-pill-clean {
  background: rgba(16, 185, 129, 0.12);
  color: #34d399;
}
.sk-error {
  color: #f87171;
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sk-footer-actions { display: flex; gap: 10px; }
.sk-btn {
  padding: 8px 16px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid transparent;
  display: flex;
  align-items: center;
  gap: 8px;
}
.sk-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.sk-btn-secondary {
  background: #111318;
  border-color: #1a1d2e;
  color: #c4c8d4;
}
.sk-btn-secondary:hover:not(:disabled) {
  background: #1e2233;
  color: #f0f2f5;
}
.sk-btn-primary {
  background: #3b82f6;
  color: white;
  border-color: #3b82f6;
}
.sk-btn-primary:hover:not(:disabled) {
  background: #2563eb;
  border-color: #2563eb;
}
.sk-kbd {
  font-size: 11px;
  font-family: ui-monospace, 'SF Mono', monospace;
  opacity: 0.7;
  background: rgba(0, 0, 0, 0.2);
  padding: 1px 5px;
  border-radius: 3px;
}

/* Mobile tweaks */
@media (max-width: 640px) {
  .sk-banner-note { display: none; }
  .sk-footer { flex-direction: column-reverse; align-items: stretch; }
  .sk-footer-actions { width: 100%; }
  .sk-footer-actions .sk-btn { flex: 1; justify-content: center; }
}

/* Transition */
.sk-modal-enter-active,
.sk-modal-leave-active { transition: opacity 0.2s ease; }
.sk-modal-enter-from,
.sk-modal-leave-to { opacity: 0; }
.sk-modal-enter-active .sk-card,
.sk-modal-leave-active .sk-card { transition: transform 0.25s ease; }
.sk-modal-enter-from .sk-card { transform: scale(0.98) translateY(8px); }
.sk-modal-leave-to .sk-card { transform: scale(0.98) translateY(8px); }
</style>
