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
import { useLang } from '../../composables/useLang'
import { apiFetch, ApiError } from '../../api'
import WizardCard from './WizardCard.vue'
import './wizard.css'

const { t } = useLang()
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
    '.cm-scroller': { fontFamily: "var(--font-mono)" },
    '.cm-gutters': { background: '#0A0C12', borderRight: '1px solid var(--border-strong)' },
    '&.cm-focused': { outline: 'none' },
  }),
]

const dirty = computed(() => content.value !== savedContent.value)
const activeFilename = computed(() => files.value.find((f) => f.name === activeName.value)?.filename || activeName.value || '')
const activeDescription = computed(() => files.value.find((f) => f.name === activeName.value)?.description || '')

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
      title: t('setup.yaml.discardTitle'),
      message: t('setup.yaml.discardMessage'),
      confirmText: t('setup.yaml.discardConfirm'),
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
    editorSuccess.value = t('setup.yaml.savedMsg', { file: fresh.filename, bytes: sizeBytes.value })
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
      title: t('setup.yaml.revertTitle'),
      message: t('setup.yaml.revertMessage'),
      confirmText: t('setup.yaml.revert'),
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
      return `${detail.message || t('setup.yaml.saveFailed')} — ${detail.error || ''}`.trim()
    }
    if (typeof detail === 'string') return detail
  }
  return err?.message || String(err)
}

async function onContinue() {
  if (dirty.value) {
    const ok = await confirm({
      title: t('setup.yaml.continueTitle'),
      message: t('setup.yaml.continueMessage'),
      confirmText: t('setup.yaml.continueConfirm'),
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
      title: t('setup.yaml.leaveTitle'),
      message: t('setup.yaml.leaveMessage'),
      confirmText: t('setup.yaml.leave'),
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

watch(content, () => { editorSuccess.value = '' })
</script>

<template>
  <WizardCard
    :title="t('setup.yaml.title')"
    :subtitle="t('setup.yaml.subtitle')"
    :step-label="t('setup.yaml.stepLabel')"
    width="960px"
  >
    <div class="wizard-callout">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--primary-hover)" stroke-width="2" style="flex-shrink:0; margin-top: 2px;">
        <circle cx="12" cy="12" r="10" />
        <line x1="12" y1="8" x2="12" y2="12" />
        <line x1="12" y1="16" x2="12.01" y2="16" />
      </svg>
      <div>
        {{ t('setup.yaml.calloutEdit') }} <code>fastagent.config.yaml</code> {{ t('setup.yaml.calloutOr') }} <code>fastagent.secrets.yaml</code>
        {{ t('setup.yaml.calloutBody1') }}
        <code>yaml.safe_load</code> {{ t('setup.yaml.calloutBody2') }} <code>.bak</code>
        {{ t('setup.yaml.calloutBody3') }} <strong>{{ t('setup.yaml.calloutSettings') }}</strong>.
      </div>
    </div>

    <div class="yaml-wrap">
      <aside class="file-list">
        <header>{{ t('setup.yaml.configFiles') }}</header>
        <button
          v-for="f in files"
          :key="f.name"
          type="button"
          class="file-item"
          :class="{ active: activeName === f.name }"
          @click="selectFile(f.name)"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
          </svg>
          <span class="name">{{ f.filename }}</span>
          <span v-if="!f.exists" class="hint">{{ t('setup.yaml.new') }}</span>
        </button>
      </aside>

      <section class="editor-col">
        <!-- Code-window chrome: 3 traffic-light dots + filename centered + status/actions right -->
        <header class="editor-bar">
          <span class="code-dots" aria-hidden="true">
            <span class="cdot" />
            <span class="cdot" />
            <span class="cdot" />
          </span>
          <div class="file-meta">
            <code class="filename" v-if="activeName">{{ activeFilename }}</code>
            <span class="desc" v-if="activeDescription">{{ activeDescription }}</span>
          </div>
          <div class="editor-actions">
            <span v-if="dirty" class="status-pill dirty">{{ t('setup.yaml.unsaved') }}</span>
            <span v-else-if="exists && activeName" class="status-pill clean">{{ t('setup.yaml.saved') }}</span>
            <button
              type="button"
              class="wizard-btn ghost small"
              :disabled="!dirty || saving"
              @click="onRevert"
            >
              {{ t('setup.yaml.revert') }}
            </button>
            <button
              type="button"
              class="wizard-btn primary small"
              :disabled="!dirty || saving || !activeName"
              @click="onSaveFile"
            >
              {{ saving ? t('common.saving') : t('common.save') }}
              <span v-if="!saving" class="shortcut">⌘S</span>
            </button>
          </div>
        </header>

        <div class="editor-shell">
          <div v-if="loading" class="overlay">{{ t('common.loading') }}</div>
          <Codemirror
            v-else-if="activeName"
            v-model="content"
            :extensions="extensions"
            :style="{ height: '100%' }"
            :placeholder="t('setup.yaml.editorPlaceholder')"
          />
          <div v-else class="overlay">{{ t('setup.yaml.selectFile') }}</div>
        </div>

        <footer class="editor-footer">
          <span v-if="editorError" class="msg error">{{ editorError }}</span>
          <span v-else-if="editorSuccess" class="msg ok">{{ editorSuccess }}</span>
          <span v-else-if="activeName" class="msg muted">
            {{ exists ? t('setup.yaml.bytesOnDisk', { bytes: sizeBytes }) : t('setup.yaml.fileNotExist') }}
          </span>
        </footer>
      </section>
    </div>

    <div v-if="store.lastSubmitError" class="wizard-error">
      {{ store.lastSubmitError }}
    </div>

    <template #footer-left>
      <button type="button" class="wizard-btn ghost" @click="onBack">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="19" y1="12" x2="5" y2="12" />
          <polyline points="12 19 5 12 12 5" />
        </svg>
        {{ t('common.back') }}
      </button>
    </template>
    <template #footer-right>
      <button type="button" class="wizard-btn ghost" :disabled="submitting" @click="onSkip">
        {{ t('setup.yaml.skipForNow') }}
      </button>
      <button
        type="button"
        class="wizard-btn primary"
        :disabled="submitting"
        @click="onContinue"
      >
        {{ submitting ? t('common.saving') : t('setup.yaml.acceptContinue') }}
        <svg v-if="!submitting" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
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
  grid-template-columns: 240px 1fr;
  gap: 14px;
  margin-top: 4px;
}
.file-list {
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  height: fit-content;
}
.file-list > header {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--text-subtle);
  padding: 6px 8px;
}
.file-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border: none;
  background: transparent;
  border-radius: var(--r-sm);
  border-left: 2px solid transparent;
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 11.5px;
  text-align: left;
  cursor: pointer;
}
.file-item:hover { background: var(--bg-3); color: var(--text); }
.file-item.active {
  background: var(--primary-bg);
  border-left-color: var(--primary);
  color: var(--text);
}
.file-item .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-item .hint {
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 500;
  letter-spacing: 0.10em;
  color: var(--text-subtle);
  padding: 1px 5px;
  border-radius: 3px;
  background: var(--bg-3);
}

.editor-col {
  display: flex;
  flex-direction: column;
  background: #0A0C12;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  overflow: hidden;
  min-height: 440px;
}
.editor-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-strong);
  background: var(--bg-2);
}
.code-dots { display: inline-flex; gap: 5px; }
.cdot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #2A2E3D;
}
.file-meta {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
  text-align: center;
  align-items: center;
}
.filename {
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.desc {
  font-size: 10.5px;
  color: var(--text-subtle);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.editor-actions { display: flex; align-items: center; gap: 8px; }
.status-pill {
  font-family: var(--font-mono);
  font-size: 9.5px;
  font-weight: 500;
  padding: 3px 8px;
  border-radius: 999px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.status-pill.dirty { background: var(--warning-bg); color: var(--warning); }
.status-pill.clean { background: var(--success-bg); color: var(--success); }

.wizard-btn.small {
  padding: 6px 10px;
  font-size: 12px;
}
.shortcut {
  font-family: var(--font-mono);
  font-size: 9.5px;
  opacity: 0.85;
  padding: 1px 5px;
  border-radius: 3px;
  background: rgba(255, 255, 255, 0.18);
  margin-left: 4px;
}

.editor-shell {
  position: relative;
  flex: 1 1 auto;
  min-height: 360px;
  background: #0A0C12;
}
.overlay {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: var(--text-subtle);
  font-size: 13px;
}
.editor-footer {
  padding: 8px 14px;
  border-top: 1px solid var(--border-strong);
  background: var(--bg-2);
  min-height: 34px;
  display: flex;
  align-items: center;
}
.msg { font-size: 11.5px; }
.msg.error { color: var(--danger); }
.msg.ok { color: var(--success); }
.msg.muted { color: var(--text-subtle); }

@media (max-width: 820px) {
  .yaml-wrap { grid-template-columns: 1fr; }
  .file-list { flex-direction: row; overflow-x: auto; }
  .file-list > header { display: none; }
}
</style>
