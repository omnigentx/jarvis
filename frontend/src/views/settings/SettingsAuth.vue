<script setup>
/**
 * Settings → Authentication.
 *
 * Two panels:
 *   - **Passkeys** (browser sign-in). List registered passkeys for this
 *     origin, register new ones, delete unused ones. Each row shows
 *     label, transports, when it was created, when it last signed in.
 *   - **API Key** is intentionally NOT shown here — it lives under
 *     Settings → General → "Change API Key / Password" and is kept as
 *     the machine-to-machine credential (Xiaozhi voice device, scripts)
 *     and the documented recovery path when every passkey is lost.
 *
 * The split mirrors the docs: passkey = browser, API key = scripts +
 * recovery. Showing both side-by-side in one panel encourages users
 * to think of them as equivalent, which they aren't.
 */
import { computed, onMounted, ref } from 'vue'

import {
  deletePasskey,
  hasAnyPasskey,
  isSupported as isPasskeySupported,
  listPasskeys,
  registerPasskey,
} from '../../services/passkey.js'

const supported = ref(true)
const credentials = ref([])
const loadError = ref('')
const loading = ref(true)
const registering = ref(false)
const registerError = ref('')
const registerSuccess = ref('')
const labelDraft = ref('')

onMounted(async () => {
  supported.value = isPasskeySupported()
  await refresh()
})

async function refresh() {
  loading.value = true
  loadError.value = ''
  const result = await listPasskeys()
  if (result.ok) {
    credentials.value = result.rows
  } else if (result.code === 'auth_failed') {
    loadError.value =
      'Your session has expired. Reload the page and sign in again.'
  } else {
    loadError.value =
      result.detail || 'Failed to load passkeys. Check the backend.'
  }
  loading.value = false
}

async function register() {
  if (registering.value) return
  registering.value = true
  registerError.value = ''
  registerSuccess.value = ''
  try {
    const result = await registerPasskey({
      label: labelDraft.value.trim() || null,
    })
    if (result.ok) {
      registerSuccess.value = result.replaced
        ? 'Existing passkey on this authenticator was updated.'
        : 'Passkey registered. You can now sign in with it.'
      labelDraft.value = ''
      await refresh()
    } else {
      registerError.value = _registerErrorMessage(result)
    }
  } finally {
    registering.value = false
  }
}

async function onDelete(cred) {
  if (!confirm(
    `Delete passkey "${cred.label || cred.id.slice(0, 12)}"? ` +
    `You will no longer be able to sign in with this authenticator on ` +
    `${cred.rp_id}.`,
  )) {
    return
  }
  const result = await deletePasskey(cred.id)
  if (!result.ok) {
    loadError.value =
      result.detail || `Failed to delete passkey (${result.code}).`
    return
  }
  await refresh()
}

function _registerErrorMessage(result) {
  switch (result.code) {
    case 'cancelled':
      return 'Cancelled. No passkey was registered.'
    case 'already_registered':
      return 'This authenticator already has a passkey for this site.'
    case 'unsupported':
      return 'Your browser does not support passkeys.'
    case 'auth_failed':
      return 'Your session expired. Reload and sign in again.'
    case 'verify_failed':
      return `Attestation rejected (${result.detail || 'unknown'}).`
    case 'network':
      return 'Network error. Check the backend is reachable.'
    default:
      return result.detail || `Register failed (${result.code}).`
  }
}

const hostname = computed(() => {
  if (typeof window === 'undefined') return ''
  return window.location.hostname || ''
})

function formatTransports(transports) {
  if (!transports || transports.length === 0) return '—'
  return transports.map((t) => {
    if (t === 'internal') return 'Touch ID / built-in'
    if (t === 'hybrid') return 'Phone / hybrid'
    if (t === 'usb') return 'USB security key'
    if (t === 'nfc') return 'NFC'
    if (t === 'ble') return 'Bluetooth'
    return t
  }).join(', ')
}

function formatRelative(unixSeconds) {
  if (!unixSeconds) return 'never'
  const deltaSec = Date.now() / 1000 - unixSeconds
  if (deltaSec < 60) return 'just now'
  if (deltaSec < 3600) return `${Math.floor(deltaSec / 60)} min ago`
  if (deltaSec < 86400) return `${Math.floor(deltaSec / 3600)} hr ago`
  if (deltaSec < 30 * 86400) return `${Math.floor(deltaSec / 86400)} days ago`
  const d = new Date(unixSeconds * 1000)
  return d.toLocaleDateString()
}
</script>

<template>
  <div class="gen-sections">
    <!-- Passkeys panel -->
    <section class="panel-card">
      <header>
        <div class="icon-circle">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" />
          </svg>
        </div>
        <div>
          <h2>Passkeys</h2>
          <p>
            Sign in to the dashboard with Touch ID, Face ID, Windows Hello, or a
            security key — no password to remember. Passkeys are scoped to the
            current origin (<code>{{ hostname }}</code>): one registered on
            localhost cannot be used after you switch to a public domain.
          </p>
        </div>
      </header>

      <div v-if="!supported" class="error-msg">
        Your browser does not support WebAuthn / passkeys.
        Sign in with the API key under Settings → General.
      </div>

      <div v-else-if="loading" class="muted-row">Loading passkeys…</div>

      <div v-else-if="credentials.length === 0" class="muted-row">
        No passkeys registered for <code>{{ hostname }}</code> yet.
      </div>

      <table v-else class="passkey-table">
        <thead>
          <tr>
            <th>Label</th>
            <th>Type</th>
            <th>Created</th>
            <th>Last used</th>
            <th class="col-actions"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="cred in credentials" :key="cred.id">
            <td class="label-cell">
              {{ cred.label || '(unlabeled)' }}
              <div class="cred-id" :title="cred.id">{{ cred.id.slice(0, 12) }}…</div>
            </td>
            <td>{{ formatTransports(cred.transports) }}</td>
            <td>{{ formatRelative(cred.created_at) }}</td>
            <td>{{ formatRelative(cred.last_used_at) }}</td>
            <td class="col-actions">
              <button
                type="button"
                class="btn danger small"
                @click="onDelete(cred)"
              >
                Delete
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="loadError" class="error-msg">{{ loadError }}</div>

      <div class="field" style="margin-top: 18px;">
        <label>Add a passkey from this device</label>
        <div class="input-group">
          <input
            v-model="labelDraft"
            class="pwd-input"
            type="text"
            placeholder="Label (e.g. MacBook Touch ID, iPhone)"
            :disabled="registering || !supported"
            maxlength="100"
          />
        </div>
      </div>

      <div class="action-row">
        <button
          type="button"
          class="btn primary"
          :disabled="!supported || registering"
          @click="register"
        >
          {{ registering ? 'Waiting for authenticator…' : 'Register passkey' }}
        </button>
      </div>

      <div v-if="registerError" class="error-msg">{{ registerError }}</div>
      <div v-else-if="registerSuccess" class="success-msg">{{ registerSuccess }}</div>
    </section>

    <!-- Pointer to API key panel -->
    <section class="panel-card">
      <header>
        <div class="icon-circle">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="9" />
            <path d="M12 8v4l3 2" />
          </svg>
        </div>
        <div>
          <h2>API Key (scripts &amp; recovery)</h2>
          <p>
            The API key in <code>JARVIS_API_KEY</code> remains the credential for
            non-browser callers (the Xiaozhi voice device, CLI tools, scripts)
            and is the recovery path if you lose every passkey: read it from
            <code>.env</code>, sign in once, then register a new passkey here.
            Rotate it under <strong>Settings → General → Change API Key</strong>.
          </p>
        </div>
      </header>
    </section>
  </div>
</template>

<style scoped>
.gen-sections {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.panel-card {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 22px 26px;
}
.panel-card header {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  margin-bottom: 16px;
}
.panel-card header h2 {
  margin: 0 0 4px 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}
.panel-card header p {
  margin: 0;
  font-size: 12.5px;
  color: var(--text-dim);
  line-height: 1.5;
}
.panel-card header code,
.panel-card p code {
  font-family: var(--font-mono);
  font-size: 11.5px;
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 5px;
  border-radius: 3px;
  color: var(--accent);
}
.icon-circle {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  border-radius: var(--r-md);
  background: var(--primary-bg);
  color: var(--primary-hover);
  display: flex;
  align-items: center;
  justify-content: center;
}
.muted-row {
  font-size: 12.5px;
  color: var(--text-muted);
  padding: 8px 0;
}
.passkey-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}
.passkey-table th {
  text-align: left;
  padding: 8px;
  border-bottom: 1px solid var(--border);
  font-family: var(--font-mono);
  font-weight: 500;
  color: var(--text-muted);
  text-transform: uppercase;
  font-size: 10px;
  letter-spacing: 0.08em;
}
.passkey-table td {
  padding: 10px 8px;
  border-bottom: 1px solid var(--border);
  color: var(--text);
  vertical-align: top;
}
.passkey-table tr:last-child td {
  border-bottom: none;
}
.label-cell {
  font-weight: 500;
}
.cred-id {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-muted);
  margin-top: 2px;
}
.col-actions {
  text-align: right;
  width: 90px;
}
.field {
  margin-top: 4px;
}
.field label {
  display: block;
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-muted);
  margin-bottom: 6px;
}
.input-group {
  display: flex;
  gap: 8px;
}
.pwd-input {
  flex: 1;
  height: 36px;
  padding: 0 12px;
  background: var(--bg-4);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  font-size: 13px;
  color: var(--text);
  outline: none;
}
.pwd-input:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-bg-strong);
}
.action-row {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
.btn {
  height: 36px;
  padding: 0 16px;
  border-radius: var(--r-md);
  border: 1px solid transparent;
  font-size: 13px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}
.btn.primary {
  background: var(--primary);
  color: #fff;
  border-color: var(--primary);
}
.btn.primary:hover:not(:disabled) {
  background: var(--primary-active);
  border-color: var(--primary-active);
}
.btn.primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn.danger.small {
  height: 28px;
  padding: 0 10px;
  background: transparent;
  color: var(--danger);
  border: 1px solid rgba(239, 68, 68, 0.35);
  font-size: 11.5px;
  border-radius: var(--r-sm);
}
.btn.danger.small:hover {
  background: var(--danger-bg);
}
.error-msg {
  margin-top: 10px;
  padding: 8px 12px;
  border-radius: var(--r-md);
  background: var(--danger-bg);
  border: 1px solid rgba(239, 68, 68, 0.3);
  color: var(--danger);
  font-size: 12.5px;
}
.success-msg {
  margin-top: 10px;
  padding: 8px 12px;
  border-radius: var(--r-md);
  background: var(--success-bg);
  border: 1px solid rgba(16, 185, 129, 0.3);
  color: var(--success);
  font-size: 12.5px;
}

/* Mobile — 5-col passkey table doesn't fit a 339px content area
   (the col-actions track alone is 90px). Degrade to card-style stack:
   each row becomes a vertical block; thead is hidden because labels
   would be redundant alongside the visible values. */
@media (max-width: 768px) {
  .panel-card { padding: 18px 16px; }
  .passkey-table,
  .passkey-table tbody,
  .passkey-table tr,
  .passkey-table td { display: block; width: 100%; }
  .passkey-table thead { display: none; }
  .passkey-table tr {
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    padding: 10px 12px;
    margin-bottom: 8px;
    background: var(--bg-2);
  }
  .passkey-table td {
    padding: 4px 0;
    border-bottom: 0;
  }
  .col-actions {
    width: 100%;
    display: flex;
    justify-content: flex-end;
    margin-top: 6px;
  }
  .cred-id { white-space: normal; word-break: break-all; }
  .input-group { width: 100%; }
  .action-row { flex-wrap: wrap; gap: 8px; }
  .action-row > .btn { flex: 1; min-width: 0; }
}
</style>
