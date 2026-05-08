<script setup>
/**
 * Settings → General.
 *
 * Two sections:
 *   - Authentication: rotate master key (PUT auth.JARVIS_API_KEY).  The
 *     backend re-derives the Fernet master as part of the same request, so
 *     the UI just needs to store the new bearer and the next call will Just
 *     Work.
 *   - System: hot-reload flags (Phase 3a will wire the actual reload hooks —
 *     here we save the value; the backend reads it on next request).
 *
 * Change with Care: write buttons are disabled until the user has edited a
 * field; confirm modals guard irreversible changes.
 */
import { ref, computed, onMounted } from 'vue'
import { useSettingsStore } from '../../stores/settings'
import { generateApiKey } from '../../stores/setup'
import { useConfirm } from '../../composables/useConfirm'
import { useAuthStore } from '../../stores/auth'
import { setApiKey } from '../../api'

const store = useSettingsStore()
const auth = useAuthStore()
const { confirm } = useConfirm()

const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR']
const SUPPORTED_TIMEZONES = Intl.supportedValuesOf('timeZone')

// ─── Auth rotation ─────────────────────────────────────────────────────
const newKey = ref('')
const confirmKey = ref('')
const revealNew = ref(false)
const rotating = ref(false)
const rotationSuccess = ref(false)
const rotationError = ref('')
const MIN_LEN = 16

const rotationErrors = computed(() => {
  const errs = []
  const k = newKey.value.trim()
  const c = confirmKey.value.trim()
  if (!k && !c) return errs
  if (k.length < MIN_LEN) errs.push(`Key must be at least ${MIN_LEN} characters.`)
  if (k && c && k !== c) errs.push('Keys do not match.')
  return errs
})

const canRotate = computed(() => {
  if (rotating.value) return false
  const k = newKey.value.trim()
  return k.length >= MIN_LEN && k === confirmKey.value.trim()
})

function generateRotation() {
  const k = generateApiKey()
  newKey.value = k
  confirmKey.value = k
  revealNew.value = true
}

async function onRotate() {
  if (!canRotate.value) return
  if (
    !(await confirm({
      title: 'Rotate master API key',
      message:
        'Every stored secret will be re-encrypted under the new key. This browser session seamlessly picks up the new bearer.',
      confirmText: 'Rotate Key',
      variant: 'warning',
    }))
  ) {
    return
  }
  rotating.value = true
  rotationError.value = ''
  rotationSuccess.value = false
  const requestedKey = newKey.value.trim()
  try {
    await store.setValue('auth', 'JARVIS_API_KEY', requestedKey, { isSecret: false })

    // Backend rotated JARVIS_API_KEY → session cookie's key fingerprint
    // is now stale; the very next request would 401 with reason
    // "key_rotated". Re-login transparently with the new key so the
    // user never sees the AuthGate just for rotating their own key.
    //
    // Single-worker deployment: the PUT handler calls apply_api_key
    // synchronously before responding, so by the time setValue resolves
    // ``core.auth.JARVIS_API_KEY`` already holds the new value and
    // login() will match on the first try.
    //
    // Multi-worker future: the login POST may land on a worker that
    // still has the old in-process key. We do one short retry (with a
    // small delay) before giving up, so the user isn't kicked to the
    // AuthGate for a 100ms propagation gap. If it still fails, surface
    // a clear hint instead of letting them stare at a frozen page.
    let result = await auth.login(requestedKey)
    if (!result.ok) {
      await new Promise((r) => setTimeout(r, 250))
      result = await auth.login(requestedKey)
    }
    if (!result.ok) {
      throw new Error(
        'Key rotated but session refresh failed — reload the page and log in with the new key.',
      )
    }
    setApiKey(requestedKey)  // legacy localStorage path used by Setup Wizard

    rotationSuccess.value = true
    newKey.value = ''
    confirmKey.value = ''
    // Re-fetch so the masked representation updates.
    await store.fetchAll()
  } catch (err) {
    rotationError.value = store.lastMutationError || String(err?.message || err)
  } finally {
    rotating.value = false
  }
}

// ─── System settings ──────────────────────────────────────────────────
const logLevel = ref('INFO')
const sessionWindow = ref('200')
const timezone = ref('Asia/Ho_Chi_Minh')
const initialLog = ref('INFO')
const initialSession = ref('200')
const initialTimezone = ref('Asia/Ho_Chi_Minh')
const systemSaving = ref(false)
const systemError = ref('')
const systemSuccess = ref(false)

const logDirty = computed(() => logLevel.value !== initialLog.value)
const sessionDirty = computed(() => String(sessionWindow.value) !== String(initialSession.value))
const timezoneDirty = computed(() => timezone.value.trim() !== initialTimezone.value)

// If the saved value is an alias not in the IANA list, prepend it so the
// current selection stays valid in the dropdown.
const timezoneOptions = computed(() => {
  const current = timezone.value
  if (current && !SUPPORTED_TIMEZONES.includes(current)) {
    return [current, ...SUPPORTED_TIMEZONES]
  }
  return SUPPORTED_TIMEZONES
})

async function refreshSystem() {
  await store.fetchAll().catch(() => {})
  logLevel.value = store.getValue('system', 'LOG_CONSOLE_LEVEL') || 'INFO'
  sessionWindow.value = String(
    store.getValue('system', 'SESSION_HISTORY_WINDOW') ?? '200',
  )
  timezone.value = store.getValue('system', 'TIMEZONE') || 'Asia/Ho_Chi_Minh'
  initialLog.value = logLevel.value
  initialSession.value = sessionWindow.value
  initialTimezone.value = timezone.value
}

async function saveLogLevel() {
  systemSaving.value = true
  systemError.value = ''
  systemSuccess.value = false
  try {
    await store.setValue('system', 'LOG_CONSOLE_LEVEL', logLevel.value)
    initialLog.value = logLevel.value
    systemSuccess.value = true
  } catch (err) {
    systemError.value = store.lastMutationError || String(err?.message || err)
  } finally {
    systemSaving.value = false
  }
}

async function saveTimezone() {
  const tz = timezone.value.trim()
  if (!tz) {
    systemError.value = 'Timezone cannot be empty.'
    return
  }
  systemSaving.value = true
  systemError.value = ''
  systemSuccess.value = false
  try {
    await store.setValue('system', 'TIMEZONE', tz)
    initialTimezone.value = tz
    systemSuccess.value = true
  } catch (err) {
    systemError.value = store.lastMutationError || String(err?.message || err)
  } finally {
    systemSaving.value = false
  }
}

async function saveSessionWindow() {
  const n = parseInt(sessionWindow.value, 10)
  if (!Number.isFinite(n) || n < 10 || n > 10000) {
    systemError.value = 'Session window must be between 10 and 10000.'
    return
  }
  systemSaving.value = true
  systemError.value = ''
  systemSuccess.value = false
  try {
    await store.setValue('system', 'SESSION_HISTORY_WINDOW', String(n))
    initialSession.value = String(n)
    systemSuccess.value = true
  } catch (err) {
    systemError.value = store.lastMutationError || String(err?.message || err)
  } finally {
    systemSaving.value = false
  }
}

// ─── Data management (export / import / restart) ──────────────────────
const dataBusy = ref(false)
const dataError = ref('')
const dataSuccess = ref('')
const includeSecretsInExport = ref(false)
const importReplace = ref(false)
const importFileInput = ref(null)
const pendingImport = ref(null)

function _flashError(msg) {
  dataError.value = msg
  dataSuccess.value = ''
}
function _flashSuccess(msg) {
  dataSuccess.value = msg
  dataError.value = ''
}

async function doExport() {
  dataBusy.value = true
  dataError.value = ''
  dataSuccess.value = ''
  try {
    if (
      includeSecretsInExport.value &&
      !(await confirm({
        title: 'Export with plaintext secrets',
        message:
          'The file will contain plaintext API keys and tokens. Only do this if the file will be stored securely.',
        confirmText: 'Export Anyway',
        variant: 'warning',
      }))
    ) {
      dataBusy.value = false
      return
    }
    const body = await store.exportConfig({
      includeSecrets: includeSecretsInExport.value,
    })
    const stamp = new Date().toISOString().replace(/[:.]/g, '-')
    const suffix = includeSecretsInExport.value ? '-with-secrets' : ''
    const filename = `jarvis-settings-${stamp}${suffix}.json`
    const blob = new Blob([JSON.stringify(body, null, 2)], {
      type: 'application/json',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    _flashSuccess(`Exported ${body.items.length} items → ${filename}`)
  } catch (err) {
    _flashError(store.lastMutationError || err?.message || String(err))
  } finally {
    dataBusy.value = false
  }
}

function triggerImportPick() {
  importFileInput.value?.click()
}

async function onImportFileChosen(ev) {
  const file = ev.target.files?.[0]
  ev.target.value = '' // allow re-selecting the same file later
  if (!file) return
  dataError.value = ''
  dataSuccess.value = ''
  try {
    const text = await file.text()
    const payload = JSON.parse(text)
    if (!payload || typeof payload !== 'object' || !Array.isArray(payload.items)) {
      throw new Error('File is not a valid settings export (missing items).')
    }
    if (payload.version !== 1) {
      throw new Error(
        `Unsupported export version: ${payload.version}. Expected 1.`,
      )
    }
    pendingImport.value = { filename: file.name, ...payload }
  } catch (err) {
    _flashError(err?.message || String(err))
  }
}

async function confirmImport() {
  const p = pendingImport.value
  if (!p) return
  const replacing = importReplace.value
  const ok = await confirm({
    title: replacing ? 'Replace settings from file' : 'Merge settings from file',
    message: replacing
      ? 'Replace mode DELETES any key in a category present in the file but missing from it.'
      : 'Merge mode only adds or updates keys from the file. Existing keys not in the file stay untouched.',
    confirmText: replacing ? 'Replace' : 'Merge',
    variant: replacing ? 'danger' : 'info',
  })
  if (!ok) return
  dataBusy.value = true
  try {
    const res = await store.importConfig({
      version: p.version,
      items: p.items,
      replace: importReplace.value,
    })
    const skipped = (res?.skipped_secrets || []).length
    const parts = [`applied ${res?.applied ?? 0}`]
    if (res?.deleted) parts.push(`deleted ${res.deleted}`)
    if (skipped) parts.push(`skipped ${skipped} secret placeholder(s)`)
    _flashSuccess(`Import complete — ${parts.join(', ')}.`)
    pendingImport.value = null
    await refreshSystem()
  } catch (err) {
    _flashError(store.lastMutationError || err?.message || String(err))
  } finally {
    dataBusy.value = false
  }
}

function cancelImport() {
  pendingImport.value = null
}

async function doRestart() {
  if (
    !(await confirm({
      title: 'Restart backend',
      message:
        'The process will exit and your container/process manager will bring it back up. API calls will fail for a few seconds.',
      confirmText: 'Restart',
      variant: 'warning',
    }))
  ) {
    return
  }
  dataBusy.value = true
  dataError.value = ''
  dataSuccess.value = ''
  try {
    await store.restartBackend()
    _flashSuccess('Restart signal sent. Backend is shutting down…')
  } catch (err) {
    // A restart may drop the connection mid-response; don't treat a network
    // error as a hard failure.
    const msg = err?.message || String(err)
    if (/network|failed to fetch|load failed/i.test(msg)) {
      _flashSuccess('Restart signal sent (connection dropped as expected).')
    } else {
      _flashError(msg)
    }
  } finally {
    dataBusy.value = false
  }
}

// ─── History viewer ────────────────────────────────────────────────────
const historyOpen = ref(false)
const historyItems = ref([])
const historyLoading = ref(false)
const historyError = ref('')
const historyFilterCategory = ref('')
const historyFilterKey = ref('')

async function openHistory() {
  historyOpen.value = true
  await loadHistory()
}

function closeHistory() {
  historyOpen.value = false
  historyError.value = ''
}

async function loadHistory() {
  historyLoading.value = true
  historyError.value = ''
  try {
    historyItems.value = await store.fetchHistory({
      category: historyFilterCategory.value.trim() || null,
      key: historyFilterKey.value.trim() || null,
      limit: 100,
    })
  } catch (err) {
    historyError.value = err?.message || String(err)
  } finally {
    historyLoading.value = false
  }
}

function formatHistoryTime(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

onMounted(refreshSystem)
</script>

<template>
  <div class="gen-sections">
    <!-- Authentication ─────────────────────────────────────── -->
    <section class="panel-card">
      <header>
        <div class="icon-circle">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
            <path d="M7 11V7a5 5 0 0 1 10 0v4" />
          </svg>
        </div>
        <div>
          <h2>Authentication</h2>
          <p>The master API key protects the entire API surface and doubles as the encryption master for stored secrets.</p>
        </div>
      </header>

      <div class="field">
        <label>Change API Key / Password</label>
        <div class="input-group">
          <input
            class="pwd-input"
            :type="revealNew ? 'text' : 'password'"
            autocomplete="new-password"
            placeholder="Enter new master key"
            v-model="newKey"
          />
          <button type="button" class="icon-btn" @click="revealNew = !revealNew">
            <svg v-if="revealNew" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
              <line x1="1" y1="1" x2="23" y2="23" />
            </svg>
            <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
              <circle cx="12" cy="12" r="3" />
            </svg>
          </button>
        </div>

        <input
          class="pwd-input"
          :type="revealNew ? 'text' : 'password'"
          autocomplete="new-password"
          placeholder="Confirm new key"
          v-model="confirmKey"
          style="margin-top: 8px;"
        />
      </div>

      <div class="action-row">
        <button type="button" class="btn ghost" @click="generateRotation">
          Generate
        </button>
        <button type="button" class="btn primary" :disabled="!canRotate" @click="onRotate">
          {{ rotating ? 'Rotating...' : 'Update Key' }}
        </button>
      </div>

      <div v-if="rotationErrors.length" class="error-msg">
        <div v-for="e in rotationErrors" :key="e">{{ e }}</div>
      </div>
      <div v-if="rotationError" class="error-msg">{{ rotationError }}</div>
      <div v-if="rotationSuccess" class="success-msg">Master key rotated. New bearer token is active.</div>
    </section>

    <!-- System ─────────────────────────────────────────────── -->
    <section class="panel-card">
      <header>
        <div class="icon-circle">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="4" y1="21" x2="4" y2="14" />
            <line x1="4" y1="10" x2="4" y2="3" />
            <line x1="12" y1="21" x2="12" y2="12" />
            <line x1="12" y1="8" x2="12" y2="3" />
            <line x1="20" y1="21" x2="20" y2="16" />
            <line x1="20" y1="12" x2="20" y2="3" />
            <line x1="1" y1="14" x2="7" y2="14" />
            <line x1="9" y1="8" x2="15" y2="8" />
            <line x1="17" y1="16" x2="23" y2="16" />
          </svg>
        </div>
        <div>
          <h2>System Settings</h2>
          <p>Runtime toggles that apply without a full restart (unless marked).</p>
        </div>
      </header>

      <!-- Log level -->
      <div class="setting-row">
        <div class="setting-meta">
          <label>Console Log Level</label>
          <span class="hint pill hot">Hot Reload</span>
          <span class="hint">Changes apply immediately without restart.</span>
        </div>
        <div class="setting-control">
          <select v-model="logLevel">
            <option v-for="l in LOG_LEVELS" :key="l" :value="l">{{ l }}</option>
          </select>
          <button
            type="button"
            class="btn primary small"
            :disabled="!logDirty || systemSaving"
            @click="saveLogLevel"
          >
            Save
          </button>
        </div>
      </div>

      <!-- Timezone -->
      <div class="setting-row">
        <div class="setting-meta">
          <label>Timezone</label>
          <span class="hint pill warn">Requires Restart</span>
          <span class="hint">IANA timezone used by all time tools (e.g. Asia/Ho_Chi_Minh, America/New_York, Europe/London). Takes effect after backend restart.</span>
        </div>
        <div class="setting-control">
          <select v-model="timezone" style="min-width: 200px;">
            <option v-for="tz in timezoneOptions" :key="tz" :value="tz">{{ tz }}</option>
          </select>
          <button
            type="button"
            class="btn primary small"
            :disabled="!timezoneDirty || systemSaving"
            @click="saveTimezone"
          >
            Save
          </button>
        </div>
      </div>

      <!-- Session window -->
      <div class="setting-row">
        <div class="setting-meta">
          <label>Session History Window</label>
          <span class="hint pill warn">Requires Restart</span>
          <span class="hint">messages per agent session — needs backend restart to take effect.</span>
        </div>
        <div class="setting-control">
          <input
            type="number"
            min="10"
            max="10000"
            step="10"
            v-model="sessionWindow"
          />
          <button
            type="button"
            class="btn primary small"
            :disabled="!sessionDirty || systemSaving"
            @click="saveSessionWindow"
          >
            Save
          </button>
        </div>
      </div>

      <div v-if="systemError" class="error-msg">{{ systemError }}</div>
      <div v-if="systemSuccess" class="success-msg">Saved.</div>
    </section>

    <!-- Data management ─────────────────────────────────────── -->
    <section class="panel-card">
      <header>
        <div class="icon-circle">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="7 10 12 15 17 10" />
            <line x1="12" y1="15" x2="12" y2="3" />
          </svg>
        </div>
        <div>
          <h2>Data Management</h2>
          <p>Back up, restore, audit, and restart. Exports mask secrets by default — opt in only when you need a true backup.</p>
        </div>
      </header>

      <!-- Export -->
      <div class="setting-row">
        <div class="setting-meta">
          <label>Export Configuration</label>
          <span class="hint">Download a JSON snapshot of every stored setting. Safe to share unless you include secrets.</span>
          <label class="checkbox-inline">
            <input type="checkbox" v-model="includeSecretsInExport" />
            <span>Include plaintext secrets <strong>(dangerous)</strong></span>
          </label>
        </div>
        <div class="setting-control">
          <button type="button" class="btn primary small" :disabled="dataBusy" @click="doExport">
            {{ dataBusy ? 'Working…' : 'Download JSON' }}
          </button>
        </div>
      </div>

      <!-- Import -->
      <div class="setting-row">
        <div class="setting-meta">
          <label>Import Configuration</label>
          <span class="hint">Apply a previously exported JSON file. Merge (default) or replace per category.</span>
          <label class="checkbox-inline">
            <input type="checkbox" v-model="importReplace" />
            <span>Replace mode — delete keys missing from file</span>
          </label>
        </div>
        <div class="setting-control">
          <input
            ref="importFileInput"
            type="file"
            accept="application/json,.json"
            style="display: none;"
            @change="onImportFileChosen"
          />
          <button type="button" class="btn ghost small" :disabled="dataBusy" @click="triggerImportPick">
            Choose file…
          </button>
        </div>
      </div>

      <!-- Import preview / confirmation -->
      <div v-if="pendingImport" class="import-preview">
        <div class="import-preview-head">
          <strong>{{ pendingImport.filename }}</strong>
          <span class="hint">
            version {{ pendingImport.version }} · {{ pendingImport.items.length }} item(s) ·
            mode: <em>{{ importReplace ? 'replace' : 'merge' }}</em>
            <template v-if="pendingImport.includes_secrets">
              · <span class="warn-text">plaintext secrets</span>
            </template>
          </span>
        </div>
        <div class="import-preview-actions">
          <button type="button" class="btn ghost small" @click="cancelImport" :disabled="dataBusy">Cancel</button>
          <button type="button" class="btn primary small" @click="confirmImport" :disabled="dataBusy">
            {{ dataBusy ? 'Applying…' : 'Apply Import' }}
          </button>
        </div>
      </div>

      <!-- History -->
      <div class="setting-row">
        <div class="setting-meta">
          <label>Change History</label>
          <span class="hint">Every setting change is logged. Use this to audit when a value was modified and by what action.</span>
        </div>
        <div class="setting-control">
          <button type="button" class="btn ghost small" @click="openHistory">View History</button>
        </div>
      </div>

      <!-- Restart -->
      <div class="setting-row">
        <div class="setting-meta">
          <label>Restart Backend</label>
          <span class="hint pill warn">Disruptive</span>
          <span class="hint">Requests a graceful shutdown. Only works when running under a process manager (docker-compose, systemd, PM2).</span>
        </div>
        <div class="setting-control">
          <button type="button" class="btn danger small" :disabled="dataBusy" @click="doRestart">
            Restart
          </button>
        </div>
      </div>

      <div v-if="dataError" class="error-msg">{{ dataError }}</div>
      <div v-if="dataSuccess" class="success-msg">{{ dataSuccess }}</div>
    </section>
  </div>

  <!-- History modal ────────────────────────────────────────────── -->
  <div v-if="historyOpen" class="modal-backdrop" @click.self="closeHistory">
    <div class="modal">
      <header class="modal-head">
        <h3>Change History</h3>
        <button type="button" class="icon-btn" @click="closeHistory" aria-label="Close">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </header>
      <div class="modal-filters">
        <input type="text" placeholder="Category filter" v-model="historyFilterCategory" />
        <input type="text" placeholder="Key filter" v-model="historyFilterKey" />
        <button type="button" class="btn ghost small" @click="loadHistory" :disabled="historyLoading">
          {{ historyLoading ? 'Loading…' : 'Apply' }}
        </button>
      </div>
      <div v-if="historyError" class="error-msg">{{ historyError }}</div>
      <div class="modal-body">
        <table v-if="historyItems.length" class="history-table">
          <thead>
            <tr>
              <th>When</th>
              <th>Category</th>
              <th>Key</th>
              <th>Action</th>
              <th>Old</th>
              <th>New</th>
              <th>By</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in historyItems" :key="row.id">
              <td class="muted">{{ formatHistoryTime(row.changed_at) }}</td>
              <td>{{ row.category }}</td>
              <td>{{ row.key }}</td>
              <td>
                <span class="action-pill" :class="row.action">{{ row.action }}</span>
              </td>
              <td class="truncate">{{ row.old_value ?? '—' }}</td>
              <td class="truncate">{{ row.new_value ?? '—' }}</td>
              <td class="muted">{{ row.changed_by || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else-if="!historyLoading" class="empty-state">No history entries match these filters.</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.gen-sections { display: flex; flex-direction: column; gap: 20px; }
.panel-card {
  background: var(--bg-card, #111318);
  border: 1px solid var(--border, #1e2030);
  border-radius: 12px;
  padding: 28px;
}
.panel-card > header {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  margin-bottom: 24px;
}
.panel-card h2 {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary, #f0f2f5);
}
.panel-card header p {
  margin-top: 4px;
  font-size: 13px;
  color: var(--text-nav, #8b8fa3);
  line-height: 1.5;
}
.icon-circle {
  flex-shrink: 0;
  width: 36px; height: 36px;
  border-radius: 10px;
  background: rgba(59, 130, 246, 0.12);
  color: var(--accent-blue, #3b82f6);
  display: grid; place-items: center;
}

.field label {
  display: block;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-nav, #8b8fa3);
  margin-bottom: 8px;
}
.input-group {
  position: relative;
  display: flex;
  align-items: center;
}
.pwd-input {
  width: 100%;
  background: var(--bg-input, #0f172a);
  border: 1px solid var(--border-input, #1e2030);
  border-radius: 8px;
  padding: 11px 40px 11px 14px;
  color: var(--text-primary, #f0f2f5);
  font-family: inherit;
  font-size: 14px;
}
.pwd-input:focus {
  outline: none;
  border-color: var(--accent-blue, #3b82f6);
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
}
.icon-btn {
  position: absolute;
  right: 8px;
  background: transparent;
  border: none;
  color: var(--text-nav, #8b8fa3);
  cursor: pointer;
  padding: 6px;
  border-radius: 6px;
}
.icon-btn:hover { color: var(--text-primary, #f0f2f5); background: rgba(255,255,255,0.04); }

.action-row {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 16px;
}
.btn {
  padding: 9px 16px;
  font-family: inherit;
  font-size: 13px;
  font-weight: 600;
  border-radius: 8px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-nav, #8b8fa3);
  cursor: pointer;
  transition: all 0.15s;
}
.btn.small { padding: 7px 14px; font-size: 12px; }
.btn.ghost:hover:not([disabled]) { color: var(--text-primary, #f0f2f5); background: rgba(255,255,255,0.04); }
.btn.primary {
  background: var(--accent-blue, #3b82f6);
  color: #ffffff;
  border-color: var(--accent-blue, #3b82f6);
}
.btn.primary:hover:not([disabled]) { background: #2f6cdc; border-color: #2f6cdc; }
.btn[disabled] { opacity: 0.5; cursor: not-allowed; }

.setting-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 18px 0;
  border-top: 1px solid var(--border, #1e2030);
  gap: 20px;
}
.setting-row:first-of-type { border-top: none; padding-top: 0; }
.setting-meta { display: flex; flex-direction: column; gap: 4px; max-width: 420px; }
.setting-meta label { font-size: 13px; font-weight: 500; color: var(--text-primary, #f0f2f5); }
.setting-meta .hint { font-size: 12px; color: var(--text-sub, #555872); line-height: 1.4; }
.setting-meta .hint.pill {
  align-self: flex-start;
  padding: 2px 8px;
  border-radius: 999px;
  font-weight: 500;
  margin: 2px 0;
}
.setting-meta .hint.pill.hot { background: rgba(34, 197, 94, 0.1); color: #22c55e; }
.setting-meta .hint.pill.warn { background: rgba(245, 158, 11, 0.1); color: #f59e0b; }

.setting-control {
  display: flex;
  gap: 8px;
  align-items: center;
}
.setting-control select,
.setting-control input[type="number"] {
  background: var(--bg-input, #0f172a);
  border: 1px solid var(--border-input, #1e2030);
  border-radius: 8px;
  padding: 9px 12px;
  color: var(--text-primary, #f0f2f5);
  font-family: inherit;
  font-size: 13px;
  min-width: 100px;
}
.setting-control select:focus,
.setting-control input:focus {
  outline: none;
  border-color: var(--accent-blue, #3b82f6);
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
}

.error-msg {
  margin-top: 14px;
  padding: 10px 14px;
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: 8px;
  color: #ef4444;
  font-size: 13px;
  line-height: 1.4;
}
.success-msg {
  margin-top: 14px;
  padding: 10px 14px;
  background: rgba(34, 197, 94, 0.08);
  border: 1px solid rgba(34, 197, 94, 0.3);
  border-radius: 8px;
  color: #22c55e;
  font-size: 13px;
}

.checkbox-inline {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-sub, #8b8fa3);
  margin-top: 4px;
  cursor: pointer;
}
.checkbox-inline input { accent-color: var(--accent-blue, #3b82f6); }
.checkbox-inline strong { color: #f59e0b; }

.btn.danger {
  background: rgba(239, 68, 68, 0.12);
  color: #ef4444;
  border-color: rgba(239, 68, 68, 0.35);
}
.btn.danger:hover:not([disabled]) { background: rgba(239, 68, 68, 0.2); border-color: #ef4444; }

.import-preview {
  margin-top: 14px;
  padding: 14px 16px;
  background: rgba(59, 130, 246, 0.08);
  border: 1px solid rgba(59, 130, 246, 0.25);
  border-radius: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  justify-content: space-between;
  align-items: center;
}
.import-preview-head { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
.import-preview-head .hint { color: var(--text-nav, #8b8fa3); font-size: 12px; }
.import-preview-head .warn-text { color: #f59e0b; font-weight: 600; }
.import-preview-actions { display: flex; gap: 8px; }

.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: grid;
  place-items: center;
  z-index: 80;
  padding: 24px;
}
.modal {
  width: min(960px, 100%);
  max-height: 86vh;
  background: var(--bg-card, #111318);
  border: 1px solid var(--border, #1e2030);
  border-radius: 14px;
  display: flex;
  flex-direction: column;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
}
.modal-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border, #1e2030);
}
.modal-head h3 { font-size: 15px; font-weight: 600; color: var(--text-primary, #f0f2f5); }
.modal-filters {
  display: flex;
  gap: 8px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border, #1e2030);
}
.modal-filters input {
  flex: 1;
  background: var(--bg-input, #0f172a);
  border: 1px solid var(--border-input, #1e2030);
  border-radius: 8px;
  padding: 8px 12px;
  color: var(--text-primary, #f0f2f5);
  font-size: 13px;
}
.modal-filters input:focus {
  outline: none;
  border-color: var(--accent-blue, #3b82f6);
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
}
.modal-body {
  overflow: auto;
  padding: 0 20px 20px;
}
.history-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}
.history-table th,
.history-table td {
  padding: 9px 10px;
  text-align: left;
  border-bottom: 1px solid var(--border, #1e2030);
  vertical-align: top;
}
.history-table th {
  position: sticky;
  top: 0;
  background: var(--bg-card, #111318);
  color: var(--text-nav, #8b8fa3);
  font-weight: 500;
  font-size: 11.5px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.history-table td.muted { color: var(--text-sub, #8b8fa3); white-space: nowrap; }
.history-table td.truncate {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary, #c4c8d4);
}
.action-pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  text-transform: lowercase;
}
.action-pill.create { background: rgba(34, 197, 94, 0.12); color: #22c55e; }
.action-pill.update { background: rgba(59, 130, 246, 0.12); color: #3b82f6; }
.action-pill.delete { background: rgba(239, 68, 68, 0.12); color: #ef4444; }
.empty-state {
  padding: 40px;
  text-align: center;
  color: var(--text-nav, #8b8fa3);
  font-size: 13px;
}

@media (max-width: 680px) {
  .setting-row { flex-direction: column; }
  .setting-control { width: 100%; }
  .modal-filters { flex-direction: column; }
  .history-table td.truncate { max-width: 140px; }
}
</style>
