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
import { useLang } from '../../composables/useLang'
import MarkdownRenderer from '../MarkdownRenderer.vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
  mode: { type: String, default: 'edit' }, // 'create' | 'edit'
  skillName: { type: String, default: '' }, // required when mode === 'edit'
})
const emit = defineEmits(['close', 'saved'])

const { confirm } = useConfirm()
const { t } = useLang()

// ----- Editor state ------------------------------------------------------

const content = ref('')
const savedContent = ref('')
// In create mode the template content is loaded into ``content`` but the
// file doesn't exist on disk yet → ``savedContent`` stays '' so the
// "Unsaved" pill + beforeunload guard correctly mark the buffer as
// pending creation. ``templateBaseline`` snapshots the loaded template
// separately so ``onClose`` can ask "did the user actually type
// anything?" rather than always firing the discard prompt — clicking X
// on a freshly-opened Create modal without any edits should just close,
// not interrogate (2026-05-27 bug).
const templateBaseline = ref('')
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
    '.cm-gutters': { background: 'var(--bg-0)', borderRight: '1px solid var(--border-strong)' },
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
    return { error: t('skillEditor.missingFrontmatter'), frontmatter: {}, body: text }
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
    nameError.value = t('skillEditor.nameRuleError')
  } else if (['con', 'prn', 'aux', 'nul', '_builtin'].includes(v.toLowerCase())) {
    nameError.value = t('skillEditor.reservedName')
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
    savedContent.value = '' // pre-create file doesn't exist → "Unsaved" pill stays on
    templateBaseline.value = content.value // snapshot for "did user edit?" check
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
    successMsg.value = t('skillEditor.savedMsg', { name: res.name })
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
  // "Did the user actually type something?" — distinct from ``dirty``.
  //   Edit mode: dirty iff content !== savedContent (file on disk).
  //   Create mode: dirty is true the moment the template loads (no file
  //     yet), but we don't want to nag the user about "discarding" an
  //     untouched template — only ask if they typed past the baseline.
  // ``newName`` is also user input even when content stays at baseline.
  const hasUserEdits = props.mode === 'create'
    ? (content.value !== templateBaseline.value || !!newName.value)
    : dirty.value
  if (hasUserEdits) {
    const ok = await confirm({
      title: t('skillEditor.discardTitle'),
      message: t('skillEditor.discardMessage'),
      confirmText: t('skillEditor.discard'),
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
      <div v-if="visible" class="sk-overlay jv" @click.self="onClose">
        <div class="sk-card" :class="{ 'sk-mobile': isMobile }">
          <!-- Header -->
          <header class="sk-header">
            <div class="sk-title-wrap">
              <span class="sk-icon">⚡</span>
              <div>
                <h3 class="sk-title">
                  {{ mode === 'create' ? t('skillEditor.createSkill') : displayName }}
                  <span v-if="isBuiltin" class="sk-badge sk-badge-builtin" :title="t('skillEditor.builtinTitle')">{{ t('skillEditor.builtin') }}</span>
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
                >{{ t('skillEditor.viewSource') }}</button>
                <button
                  type="button"
                  :class="{ active: viewMode === 'split' }"
                  @click="viewMode = 'split'"
                  role="tab"
                >{{ t('skillEditor.viewSplit') }}</button>
                <button
                  type="button"
                  :class="{ active: viewMode === 'preview' }"
                  @click="viewMode = 'preview'"
                  role="tab"
                >{{ t('skillEditor.viewPreview') }}</button>
              </div>
              <div v-else class="sk-view-toggle" role="tablist">
                <button
                  type="button"
                  :class="{ active: viewMode === 'source' }"
                  @click="viewMode = 'source'"
                  role="tab"
                >{{ t('skillEditor.viewSource') }}</button>
                <button
                  type="button"
                  :class="{ active: viewMode === 'preview' }"
                  @click="viewMode = 'preview'"
                  role="tab"
                >{{ t('skillEditor.viewPreview') }}</button>
              </div>
              <button class="sk-close" @click="onClose" :aria-label="t('skillEditor.close')">×</button>
            </div>
          </header>

          <!-- Used-by banner (edit mode, with refs) -->
          <div v-if="mode === 'edit' && usedBy.length" class="sk-banner sk-banner-info">
            <strong>{{ t('skillEditor.usedByCount', { n: usedBy.length }) }}</strong>
            <span>{{ usedBy.join(', ') }}</span>
            <span class="sk-banner-note">{{ t('skillEditor.usedByNote') }}</span>
          </div>

          <!-- Conflict banner -->
          <div v-if="conflict" class="sk-banner sk-banner-warn">
            <strong>{{ t('skillEditor.conflictLabel') }}</strong>
            <span>{{ error }}</span>
            <button class="sk-banner-btn" @click="onReloadFromDisk">{{ t('skillEditor.reload') }}</button>
          </div>

          <!-- Create-mode name input -->
          <div v-if="mode === 'create'" class="sk-create-form">
            <label class="sk-label">
              {{ t('skillEditor.skillName') }}
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
            <p v-else class="sk-input-hint">{{ t('skillEditor.nameHint') }}</p>
          </div>

          <!-- Frontmatter preview parse error (preview pane only) -->
          <div
            v-if="(viewMode === 'preview' || viewMode === 'split') && parsed.error"
            class="sk-banner sk-banner-warn"
          >
            <strong>{{ t('skillEditor.frontmatterParseError') }}</strong>
            <span>{{ parsed.error }}</span>
          </div>

          <!-- Body: editor + preview -->
          <div class="sk-body" :class="`sk-body-${viewMode}`">
            <div v-if="viewMode !== 'preview'" class="sk-pane sk-pane-source">
              <div v-if="loading" class="sk-loading">{{ t('skillEditor.loading') }}</div>
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
                <div v-else class="sk-empty-preview">{{ t('skillEditor.emptyPreview') }}</div>
              </div>
            </div>
          </div>

          <!-- Footer -->
          <footer class="sk-footer">
            <div class="sk-status">
              <span v-if="dirty && !saving" class="sk-pill sk-pill-dirty">{{ t('skillEditor.unsaved') }}</span>
              <span v-else-if="saving" class="sk-pill sk-pill-saving">{{ t('skillEditor.saving') }}</span>
              <span v-else-if="successMsg" class="sk-pill sk-pill-saved">{{ successMsg }}</span>
              <span v-else-if="!loading" class="sk-pill sk-pill-clean">{{ t('skillEditor.saved') }}</span>
              <span v-if="error && !conflict" class="sk-error">{{ error }}</span>
            </div>
            <div class="sk-footer-actions">
              <button class="sk-btn sk-btn-secondary" @click="onClose">{{ t('skillEditor.cancel') }}</button>
              <button
                class="sk-btn sk-btn-primary"
                :disabled="!canSave"
                @click="onSave"
              >
                {{ mode === 'create' ? t('skillEditor.create') : t('skillEditor.save') }} <span class="sk-kbd">⌘S</span>
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
  background: var(--bg-overlay);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
}

.sk-card {
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: 16px;
  width: 1100px;
  max-width: 95vw;
  /* dvh excludes the iOS URL bar so the footer (Save/Cancel) stays
     reachable as the bar shows/hides. Same effect on desktop. */
  height: 80dvh;
  max-height: 800px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.03);
}
.sk-card.sk-mobile {
  width: 100vw;
  /* dvh + safe-area-bottom is the iOS-correct way to fill the screen:
     content goes edge-to-edge but the footer padding lifts above the
     home indicator. 100vh would clip behind the iOS URL bar. */
  height: 100dvh;
  max-width: 100vw;
  max-height: 100dvh;
  border-radius: 0;
  border: none;
  padding-top: var(--safe-top);
}

/* Header */
.sk-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
  gap: 12px;
  /* flex-shrink: 0 + body min-height: 0 makes the body the sole
     scroll surface. Without this, on a short mobile viewport with
     extra banners visible (used-by / conflict / parse-error), the
     whole card scrolls — including header — and the X button rolls
     off-screen. */
  flex-shrink: 0;
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
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 10px;
}
.sk-subtitle {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--text-muted);
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
  color: var(--primary-hover);
  border: 1px solid rgba(59, 130, 246, 0.3);
}

.sk-header-actions { display: flex; align-items: center; gap: 10px; }

.sk-view-toggle {
  display: flex;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 2px;
}
.sk-view-toggle button {
  padding: 5px 12px;
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 500;
  border-radius: 6px;
  cursor: pointer;
}
.sk-view-toggle button:hover:not(.active) { color: var(--text-dim); }
.sk-view-toggle button.active {
  /* Use the primary tint instead of bg-3 — at light theme bg-3 (#F1F2F6)
     and the toggle's bg-2 host (#FAFAFC) only differ by ~3% lightness,
     making "selected" invisible. Primary-bg-strong gives the active tab
     a tinted blue that pops against the white card in both themes. */
  background: var(--primary-bg-strong);
  color: var(--primary-hover);
}

.sk-close {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-muted);
  width: 32px; height: 32px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 22px;
  line-height: 1;
}
.sk-close:hover { color: var(--text); background: var(--bg-3); }

/* Banners */
.sk-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 20px;
  font-size: 13px;
  border-bottom: 1px solid var(--border);
}
.sk-banner-info {
  background: rgba(59, 130, 246, 0.06);
  color: var(--text-dim);
}
.sk-banner-info strong { color: var(--primary-hover); }
.sk-banner-note { color: var(--text-muted); margin-left: auto; font-size: 12px; }
.sk-banner-warn {
  background: rgba(245, 158, 11, 0.08);
  color: var(--warning);
}
.sk-banner-warn strong { color: var(--warning); }
.sk-banner-btn {
  margin-left: auto;
  background: rgba(245, 158, 11, 0.15);
  color: var(--warning);
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
  border-bottom: 1px solid var(--border);
  background: var(--bg-0);
}
.sk-label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  color: var(--text-muted);
  font-weight: 500;
}
.sk-name-input {
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  padding: 9px 12px;
  color: var(--text);
  font-family: ui-monospace, 'SF Mono', monospace;
  font-size: 13px;
}
.sk-name-input:focus {
  outline: none;
  border-color: var(--info);
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
}
.sk-name-input.invalid {
  border-color: var(--danger);
  box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.1);
}
.sk-input-error {
  margin: 6px 0 0;
  color: var(--danger);
  font-size: 12px;
}
.sk-input-hint {
  margin: 6px 0 0;
  color: var(--text-subtle);
  font-size: 12px;
}

/* Body / panes */
.sk-body {
  flex: 1;
  display: flex;
  min-height: 0;
  background: var(--bg-0);
  /* Allow panes to overflow within the body, never outside the modal. */
  overflow: hidden;
}
.sk-body-source .sk-pane-source { flex: 1; }
.sk-body-preview .sk-pane-preview { flex: 1; }
.sk-body-split .sk-pane-source { flex: 1; border-right: 1px solid var(--border); }
.sk-body-split .sk-pane-preview { flex: 1; }
.sk-pane { min-width: 0; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
.sk-pane-source :deep(.cm-editor) { height: 100%; max-width: 100%; }
.sk-pane-source :deep(.v-codemirror) { flex: 1; min-height: 0; width: 100%; }
.sk-pane-preview {
  overflow-y: auto;
  padding: 16px 20px;
  color: var(--text-dim);
}
.sk-fm-card {
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
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
.sk-fm-key { color: var(--text-muted); font-weight: 600; min-width: 96px; }
.sk-fm-val { color: var(--text-dim); }
/* Wrap rendered markdown as a content card so the body is visually
   separated from the frontmatter card above. Without this the preview
   was a wall of text on the same surface as the body bg → no section
   boundaries, hard to scan (2026-05-27 "text blocks not clearly
   visible" report). */
.sk-md {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 18px;
  color: var(--text-dim);
}
.sk-md :deep(h1) {
  font-size: 22px;
  color: var(--text);
  margin: 0 0 10px;
}
/* H2 sections (When to use / How it works) get an underline so the
   preview reads as a structured doc rather than a wall. */
.sk-md :deep(h2) {
  font-size: 18px;
  color: var(--text);
  margin: 20px 0 10px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
}
.sk-md :deep(h2:first-child) { margin-top: 0; }
.sk-md :deep(h3) { font-size: 15px; color: var(--text); margin: 14px 0 6px; }
.sk-md :deep(ul), .sk-md :deep(ol) { margin: 6px 0; padding-left: 24px; }
.sk-md :deep(li) { margin: 2px 0; }
.sk-empty-preview {
  color: var(--text-subtle);
  font-size: 13px;
  font-style: italic;
}
.sk-loading {
  flex: 1; display: flex; align-items: center; justify-content: center;
  color: var(--text-muted);
}

/* Footer */
.sk-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  /* Add safe-area-bottom so Save/Cancel sit above the iOS home
     indicator on phone. */
  padding: 12px 20px max(12px, var(--safe-bottom));
  border-top: 1px solid var(--border);
  background: var(--bg-1);
  gap: 12px;
  flex-wrap: wrap;
  /* Pair with header — body remains the sole scroll surface. */
  flex-shrink: 0;
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
  color: var(--warning);
}
.sk-pill-saving {
  background: rgba(59, 130, 246, 0.12);
  color: var(--primary-hover);
}
.sk-pill-saved, .sk-pill-clean {
  background: rgba(16, 185, 129, 0.12);
  color: var(--success);
}
.sk-error {
  color: var(--danger);
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
  background: var(--bg-2);
  border-color: var(--border);
  color: var(--text-dim);
}
.sk-btn-secondary:hover:not(:disabled) {
  background: var(--bg-3);
  color: var(--text);
}
.sk-btn-primary {
  background: var(--info);
  color: white;
  border-color: var(--info);
}
.sk-btn-primary:hover:not(:disabled) {
  background: var(--primary);
  border-color: var(--primary);
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
/* JS uses `< 768px` for the same break (checkMobile). Keep the CSS
   threshold aligned so we never end up in a "JS thinks desktop, CSS
   thinks mobile" half-state at the 641-767px range. */
@media (max-width: 767px) {
  .sk-banner-note { display: none; }
  .sk-footer { flex-direction: column-reverse; align-items: stretch; }
  .sk-footer-actions { width: 100%; }
  .sk-footer-actions .sk-btn { flex: 1; justify-content: center; }

  /* Header: title + subtitle + view-toggle + close on one row overflowed
     on phones — the toggle landed on top of the subtitle and the × got
     clipped off the right edge. Stack into two rows: title (full width,
     subtitle truncates) on top, then the toggle + close on their own row
     with the close pinned right so it's always reachable. */
  .sk-header { flex-wrap: wrap; row-gap: 10px; }
  .sk-title-wrap { flex: 1 1 100%; }
  .sk-header-actions { flex: 1 1 100%; justify-content: space-between; }
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
