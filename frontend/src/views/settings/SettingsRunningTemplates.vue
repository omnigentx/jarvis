<script setup>
/**
 * Settings → Running Templates.
 *
 * Per-session view of the LIVE team-template state (DB SSoT). Pairs with the
 * YAML Config tab which edits the factory yaml files — those edits don't
 * touch already-running teams; this tab is where you actually apply them.
 *
 * Three primary actions per role:
 *   • Edit fields (instruction / servers / skills / model / server_overrides /
 *     role_display) — PATCH /api/team-sessions/{id}/template/roles/{role}.
 *   • Reset to yaml factory — POST .../template/reset/{role}.
 *   • Force-reload (SIGKILL + respawn agents) — POST .../reload. Confirmed.
 *
 * History rail on the right: every audit row for the session, with rollback.
 *
 * Banner: per decision 2026-05-17, edits land in DB only. The banner makes
 * that explicit so the user doesn't expect yaml to follow along.
 */
import { ref, computed, watch, onMounted } from 'vue'
import { CodeDiff } from 'v-code-diff'
import { apiFetch, ApiError } from '../../api'
import { useConfirm } from '../../composables/useConfirm'

const { confirm } = useConfirm()

const sessions = ref([])
const activeSessionId = ref(null)
const template = ref(null)
const history = ref([])
const yamlDiff = ref(null)
const loading = ref(false)
const error = ref('')
const successMsg = ref('')

// Per-role draft state — keyed by role name. We start drafts from the
// fetched template every time the user switches roles (or saves) so the
// "dirty" indicator reflects only their pending edits.
const activeRole = ref(null)
const draft = ref(null)
const saving = ref(false)
const resetting = ref(false)
const reloading = ref(false)
const rollingBack = ref(null)  // audit_id while in-flight

// "Reset to yaml" opens a 3-way modal (Cancel / Sync only / Sync + Force
// reload) so the user doesn't have to remember the two-step sequence —
// the underlying API is still two calls, but the UI hides the coupling.
const resetModal = ref({ visible: false, busy: false, mode: null })

// Per-role / per-field diff viewer — reads the yamlDiff response we
// already fetch in loadTemplate(), so no extra round-trip.
const diffModal = ref({ visible: false })

const activeSession = computed(
  () => sessions.value.find((s) => s.session_id === activeSessionId.value) || null,
)
const roles = computed(() => template.value?.roles || {})
const roleEntries = computed(() => Object.entries(roles.value))
const driftByRole = computed(() => {
  const out = {}
  if (yamlDiff.value?.per_role) {
    for (const [k, v] of Object.entries(yamlDiff.value.per_role)) out[k] = v.status
  }
  return out
})

const dirty = computed(() => {
  if (!draft.value || !activeRole.value) return false
  const live = roles.value[activeRole.value]
  if (!live) return false
  // Empty / whitespace server_overrides_text round-trips to `{}` (see
  // _buildPatch). Avoid showing a UNSAVED pill in that no-op case.
  const liveOverrides = JSON.stringify(live.server_overrides || {})
  let draftOverrides
  try {
    draftOverrides = JSON.stringify(
      draft.value.server_overrides_text.trim()
        ? JSON.parse(draft.value.server_overrides_text)
        : {},
    )
  } catch {
    // Unparseable text is definitively dirty — Save will surface the error.
    draftOverrides = draft.value.server_overrides_text
  }
  // servers/skills are unordered sets server-side (compute_role_diff), and
  // _buildPatch sorts before diffing — so a pure reorder produces an EMPTY
  // patch. Mirror that normalization here, else the UNSAVED pill lights up on
  // a reorder but Save reports "No changes" and the pill never clears (stuck).
  const serversDirty =
    JSON.stringify(_splitLines(draft.value.servers_text).sort()) !==
    JSON.stringify([...(live.servers || [])].sort())
  const skillsDirty =
    JSON.stringify(_splitLines(draft.value.skills_text).sort()) !==
    JSON.stringify([...(live.skills || [])].sort())
  return (
    draft.value.instruction !== (live.instruction || '') ||
    draft.value.role_display !== (live.role_display || '') ||
    draft.value.model !== (live.model || '') ||
    serversDirty ||
    skillsDirty ||
    draftOverrides !== liveOverrides
  )
})


// ── Network ──────────────────────────────────────────────────────────────


async function loadSessions() {
  loading.value = true
  error.value = ''
  try {
    const res = await apiFetch('/api/team-sessions')
    sessions.value = res?.sessions || []
    if (!activeSessionId.value && sessions.value.length) {
      activeSessionId.value = sessions.value[0].session_id
    }
  } catch (err) {
    error.value = _friendly(err)
  } finally {
    loading.value = false
  }
}

async function loadTemplate(sid) {
  if (!sid) return
  loading.value = true
  error.value = ''
  successMsg.value = ''
  try {
    const [tmpl, hist, diff] = await Promise.all([
      apiFetch(`/api/team-sessions/${encodeURIComponent(sid)}/template`),
      apiFetch(`/api/team-sessions/${encodeURIComponent(sid)}/template/history?limit=100`),
      apiFetch(`/api/team-sessions/${encodeURIComponent(sid)}/template/yaml-diff`).catch(() => null),
    ])
    template.value = tmpl?.template || null
    history.value = hist?.rows || []
    yamlDiff.value = diff
    // Pick first role by default; preserves selection across reloads if
    // the role still exists.
    const keys = Object.keys(template.value?.roles || {})
    if (!activeRole.value || !keys.includes(activeRole.value)) {
      activeRole.value = keys[0] || null
    }
    _seedDraft()
  } catch (err) {
    error.value = _friendly(err)
  } finally {
    loading.value = false
  }
}

function _seedDraft() {
  const r = activeRole.value ? roles.value[activeRole.value] : null
  if (!r) {
    draft.value = null
    return
  }
  draft.value = {
    instruction: r.instruction || '',
    role_display: r.role_display || '',
    model: r.model || '',
    servers_text: (r.servers || []).join('\n'),
    skills_text: (r.skills || []).join('\n'),
    server_overrides_text: JSON.stringify(r.server_overrides || {}, null, 2),
  }
}

function _splitLines(text) {
  return (text || '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
}

function _buildPatch() {
  // Compute the smallest patch that reaches the desired state. Skip fields
  // that match the current value so the audit log stays focused.
  if (!draft.value || !activeRole.value) return {}
  const live = roles.value[activeRole.value] || {}
  const patch = {}
  if (draft.value.instruction !== (live.instruction || '')) {
    patch.instruction = draft.value.instruction
  }
  if (draft.value.role_display !== (live.role_display || '')) {
    patch.role_display = draft.value.role_display
  }
  if (draft.value.model !== (live.model || '')) {
    patch.model = draft.value.model
  }
  const servers = _splitLines(draft.value.servers_text)
  const liveServers = [...(live.servers || [])].sort()
  if (JSON.stringify([...servers].sort()) !== JSON.stringify(liveServers)) {
    patch.servers = servers
  }
  const skills = _splitLines(draft.value.skills_text)
  const liveSkills = [...(live.skills || [])].sort()
  if (JSON.stringify([...skills].sort()) !== JSON.stringify(liveSkills)) {
    patch.skills = skills
  }
  // server_overrides has a "clear" intent that must round-trip — emptying
  // the textarea should remove the overrides, not be a no-op. Treat empty
  // or all-whitespace as `{}` so the same code path catches both "set to
  // empty" and "edit a value".
  const overridesText = draft.value.server_overrides_text.trim()
  let parsedOverrides
  if (overridesText) {
    try {
      parsedOverrides = JSON.parse(overridesText)
    } catch {
      throw new Error('server_overrides must be valid JSON')
    }
  } else {
    parsedOverrides = {}
  }
  if (
    JSON.stringify(parsedOverrides) !==
    JSON.stringify(live.server_overrides || {})
  ) {
    patch.server_overrides = parsedOverrides
  }
  return patch
}

async function onSaveRole() {
  if (!activeRole.value || !activeSessionId.value) return
  let patch
  try {
    patch = _buildPatch()
  } catch (err) {
    error.value = err.message
    return
  }
  if (!Object.keys(patch).length) {
    successMsg.value = 'No changes to save.'
    return
  }
  saving.value = true
  error.value = ''
  successMsg.value = ''
  try {
    const r = await apiFetch(
      `/api/team-sessions/${encodeURIComponent(activeSessionId.value)}/template/roles/${encodeURIComponent(activeRole.value)}`,
      {
        method: 'PATCH',
        body: JSON.stringify({ patch, comment: 'edited from Settings UI' }),
      },
    )
    successMsg.value = `Saved · ${r.audit_ids?.length || 0} audit row(s). ${r.warning || ''}`.trim()
    await loadTemplate(activeSessionId.value)
  } catch (err) {
    error.value = _friendly(err)
  } finally {
    saving.value = false
  }
}

function onResetRole() {
  // Just opens the chooser — the actual API call(s) fire from the modal
  // so the user picks "sync only" or "sync + force reload" first.
  if (!activeRole.value || !activeSessionId.value) return
  resetModal.value = { visible: true, busy: false, mode: null }
}

async function _doResetCall() {
  return apiFetch(
    `/api/team-sessions/${encodeURIComponent(activeSessionId.value)}/template/reset/${encodeURIComponent(activeRole.value)}`,
    { method: 'POST', body: JSON.stringify({ comment: 'reset from Settings UI' }) },
  )
}

async function _doReloadCall() {
  return apiFetch(
    `/api/team-sessions/${encodeURIComponent(activeSessionId.value)}/reload`,
    {
      method: 'POST',
      body: JSON.stringify({ roles: [activeRole.value], confirm: true }),
    },
  )
}

async function onResetModalConfirm(mode) {
  // mode: 'sync-only' — pull yaml → DB. Running agents keep using stale
  //                     in-memory template until the next natural restart.
  //       'sync-and-reload' — same DB write, then SIGKILL+respawn agents
  //                     so the new template takes effect immediately.
  resetModal.value = { ...resetModal.value, busy: true, mode }
  resetting.value = true
  error.value = ''
  successMsg.value = ''
  try {
    const r = await _doResetCall()
    // From here the DB write has landed. A reload failure below is a PARTIAL
    // success — catch it locally so we still refetch and converge the UI to
    // the new DB truth. If we let it bubble to the outer catch (old behaviour),
    // the UI kept showing pre-reset values; the user would re-edit them and
    // Save, silently overwriting the just-synced fields.
    let respawned = 0
    let reloadError = null
    if (mode === 'sync-and-reload') {
      reloading.value = true
      try {
        const rl = await _doReloadCall()
        respawned = Object.values(rl.results || {}).flat().length
      } catch (rlErr) {
        reloadError = rlErr
      } finally {
        reloading.value = false
      }
    }
    const fields = r.audit_ids?.length || 0
    resetModal.value = { visible: false, busy: false, mode: null }
    await loadTemplate(activeSessionId.value)
    if (reloadError) {
      error.value =
        `Synced ${fields} field(s) to DB, but force-reload failed — agents are ` +
        `still running the old template: ${_friendly(reloadError)}`
    } else {
      successMsg.value =
        mode === 'sync-and-reload'
          ? `Reset · ${fields} field(s) synced from yaml · ${respawned} agent(s) respawned.`
          : `Reset · ${fields} field(s) synced from yaml. Running agents will pick up the change on next restart — click "Force reload" to apply now.`
    }
  } catch (err) {
    // Reset itself failed (or the refetch threw). Still refetch so the UI
    // reflects whatever the DB actually holds rather than a stale draft.
    error.value = _friendly(err)
    resetModal.value = { ...resetModal.value, busy: false }
    loadTemplate(activeSessionId.value).catch(() => {})
  } finally {
    resetting.value = false
  }
}

function onResetModalCancel() {
  if (resetModal.value.busy) return
  resetModal.value = { visible: false, busy: false, mode: null }
}

// Switching role/session or refreshing re-seeds the draft from live data,
// silently discarding in-progress edits. Confirm first (mirrors
// SettingsYaml.selectFile) so a glance at a sibling role can't lose work.
async function _confirmDiscardIfDirty() {
  if (!dirty.value) return true
  return await confirm({
    title: 'Discard unsaved changes',
    message: `You have unsaved edits to "${activeRole.value}". Discard and switch?`,
    confirmText: 'Discard & Switch',
    variant: 'warning',
  })
}

async function onSelectRole(role) {
  if (role === activeRole.value) return
  if (!(await _confirmDiscardIfDirty())) return
  activeRole.value = role
}

async function onSelectSession(evt) {
  const sid = evt.target.value
  if (sid === activeSessionId.value) return
  if (!(await _confirmDiscardIfDirty())) {
    evt.target.value = activeSessionId.value // revert the <select> on cancel
    return
  }
  activeSessionId.value = sid
}

async function onRefresh() {
  if (!(await _confirmDiscardIfDirty())) return
  loadTemplate(activeSessionId.value)
}

function onShowDiff() {
  diffModal.value = { visible: true }
}

function onCloseDiff() {
  diffModal.value = { visible: false }
}

function formatDiffValue(v) {
  // Stable, human-readable rendering for any of: string, list-of-strings,
  // object. v-code-diff expects strings; pretty-JSON non-strings so the
  // structural shape is obvious in both sides.
  if (v === null || v === undefined) return ''
  if (typeof v === 'string') return v
  return JSON.stringify(v, null, 2)
}

// Layout toggle for the v-code-diff component. Persisted in localStorage
// so the user's preference sticks across tab switches / refreshes.
const diffLayout = ref(
  (typeof localStorage !== 'undefined' && localStorage.getItem('jv.diff.layout')) || 'unified',
)
watch(diffLayout, (v) => {
  try { localStorage.setItem('jv.diff.layout', v) } catch { /* private mode */ }
})

function diffLanguageFor(field) {
  // Hints highlight.js which grammar to use. ``instruction`` is free-form
  // text; lists/objects are rendered as JSON above; the catch-all is yaml
  // since the rest of these fields come from yaml originally.
  if (field === 'instruction') return 'plaintext'
  if (field === 'server_overrides') return 'json'
  if (field === 'servers' || field === 'skills') return 'json'
  return 'yaml'
}

// Roles to render in the diff modal, in this order:
//   1. The currently-active role first (so the user lands on the role
//      they were just editing).
//   2. Then any other diverged roles.
//   3. In-sync / unchanged roles are dropped — the modal title already
//      says "diff", listing in-sync rows would just be noise.
const diffRolesOrdered = computed(() => {
  const per = yamlDiff.value?.per_role || {}
  const entries = Object.entries(per).filter(([, v]) => v.status !== 'in_sync')
  entries.sort(([a], [b]) => {
    if (a === activeRole.value) return -1
    if (b === activeRole.value) return 1
    return a.localeCompare(b)
  })
  return entries
})

async function onReloadRole() {
  if (!activeRole.value || !activeSessionId.value) return
  const ok = await confirm({
    title: `Force-reload ${activeRole.value}?`,
    message: `SIGKILL every agent in this role mid-task and respawn with the current template. This is DESTRUCTIVE — in-progress work in those agents is lost. Continue?`,
    confirmText: 'Force-kill & respawn',
    variant: 'danger',
  })
  if (!ok) return
  reloading.value = true
  error.value = ''
  successMsg.value = ''
  try {
    const r = await apiFetch(
      `/api/team-sessions/${encodeURIComponent(activeSessionId.value)}/reload`,
      {
        method: 'POST',
        body: JSON.stringify({ roles: [activeRole.value], confirm: true }),
      },
    )
    const respawned = Object.values(r.results || {}).flat().length
    successMsg.value = `Reload complete · ${respawned} agent(s) respawned.`
    await loadTemplate(activeSessionId.value)
  } catch (err) {
    error.value = _friendly(err)
  } finally {
    reloading.value = false
  }
}

async function onRollback(auditId) {
  const ok = await confirm({
    title: 'Roll back this audit row?',
    message: 'Writes a new audit row that reverts the field to its previous value. Intermediate edits to the same field will be silently overwritten.',
    confirmText: 'Rollback',
    variant: 'warning',
  })
  if (!ok) return
  rollingBack.value = auditId
  error.value = ''
  try {
    await apiFetch(
      `/api/team-sessions/${encodeURIComponent(activeSessionId.value)}/template/rollback/${auditId}`,
      { method: 'POST', body: JSON.stringify({ comment: 'rollback from Settings UI' }) },
    )
    successMsg.value = `Rolled back audit #${auditId}.`
    await loadTemplate(activeSessionId.value)
  } catch (err) {
    error.value = _friendly(err)
  } finally {
    rollingBack.value = null
  }
}

function _friendly(err) {
  if (err instanceof ApiError && err.body && typeof err.body === 'object') {
    const detail = err.body.detail
    if (detail && typeof detail === 'object') {
      return `${detail.message || 'Request failed'} — ${detail.error || ''}`.trim()
    }
    if (typeof detail === 'string') return detail
  }
  return err?.message || String(err)
}

function fmtTime(epoch) {
  if (!epoch) return ''
  return new Date(epoch * 1000).toLocaleString()
}

function jsonPreview(v) {
  const s = JSON.stringify(v)
  return s.length > 60 ? `${s.slice(0, 57)}…` : s
}


// ── Lifecycle ────────────────────────────────────────────────────────────

watch(activeSessionId, (sid) => sid && loadTemplate(sid))
watch(activeRole, () => _seedDraft())

onMounted(() => loadSessions())
</script>

<template>
  <div class="running-tmpl">
    <header class="banner">
      <div class="banner-icon">⚠</div>
      <div>
        <strong>Edits live in this team's DB only.</strong>
        Save here to take effect now; commit the same change to <code>team_templates/&lt;team&gt;.yaml</code>
        (YAML Config tab) to survive a recreate. Use "Reset role" to pull a yaml edit into this team.
      </div>
    </header>

    <!-- Session selector -->
    <div class="session-bar">
      <label class="session-label">Team session</label>
      <select :value="activeSessionId" @change="onSelectSession" class="session-select" :disabled="!sessions.length">
        <option v-if="!sessions.length" :value="null">No active team sessions</option>
        <option v-for="s in sessions" :key="s.session_id" :value="s.session_id">
          {{ s.team_name }} · {{ s.session_id.slice(0, 8) }} · {{ s.agents_count }} agent{{ s.agents_count === 1 ? '' : 's' }}
        </option>
      </select>
      <span v-if="yamlDiff" class="drift-pill" :class="{ insync: yamlDiff.in_sync }">
        {{ yamlDiff.in_sync ? '✓ in sync with yaml' : `△ ${yamlDiff.diverged_count} role(s) diverged from yaml` }}
      </span>
      <button class="btn ghost" type="button" :disabled="loading" @click="onRefresh">
        Refresh
      </button>
    </div>

    <!-- Two-column layout: role list + editor; history rail at far right -->
    <div class="layout">
      <aside class="role-list">
        <div class="tree-eyebrow">ROLES</div>
        <button
          v-for="[role, cfg] in roleEntries"
          :key="role"
          type="button"
          class="role-item"
          :class="{ active: activeRole === role }"
          @click="onSelectRole(role)"
        >
          <span class="role-name">{{ cfg.role_display || role }}</span>
          <span class="role-key"><code>{{ role }}</code></span>
          <span
            v-if="driftByRole[role] && driftByRole[role] !== 'in_sync'"
            class="drift-dot"
            :title="`Drift from yaml: ${driftByRole[role]}`"
          >△</span>
        </button>
        <div v-if="!roleEntries.length && !loading" class="empty">No roles in this template.</div>
      </aside>

      <section class="editor">
        <header class="editor-head" v-if="activeRole && draft">
          <div class="title">
            <code>{{ activeRole }}</code>
            <span class="muted">·</span>
            <input v-model="draft.role_display" class="display-input" placeholder="role display name" />
          </div>
          <div class="actions">
            <span v-if="dirty" class="pill dirty">● UNSAVED</span>
            <button
              type="button"
              class="btn ghost"
              :disabled="!yamlDiff || yamlDiff.in_sync"
              :title="yamlDiff?.in_sync ? 'DB matches yaml — nothing to diff' : 'Show yaml vs DB diff'"
              @click="onShowDiff"
            >
              View diff
            </button>
            <button type="button" class="btn ghost" :disabled="resetting || !activeRole" @click="onResetRole">
              {{ resetting ? 'Resetting…' : 'Reset to yaml' }}
            </button>
            <button type="button" class="btn danger" :disabled="reloading || !activeRole" @click="onReloadRole">
              {{ reloading ? 'Reloading…' : 'Force reload' }}
            </button>
            <button type="button" class="btn primary" :disabled="!dirty || saving" @click="onSaveRole">
              {{ saving ? 'Saving…' : 'Save' }}
            </button>
          </div>
        </header>

        <div v-if="loading" class="overlay">Loading…</div>
        <div v-else-if="!activeRole" class="overlay">Pick a role to edit.</div>
        <div v-else class="editor-body">
          <label class="field">
            <span class="fl">Model override</span>
            <input v-model="draft.model" class="text" placeholder="(empty = inherit team default)" />
          </label>
          <label class="field">
            <span class="fl">Instruction</span>
            <textarea v-model="draft.instruction" class="textarea" rows="8" />
          </label>
          <div class="field-grid">
            <label class="field">
              <span class="fl">Servers (one per line)</span>
              <textarea v-model="draft.servers_text" class="textarea mono" rows="6" />
            </label>
            <label class="field">
              <span class="fl">Skills (one per line)</span>
              <textarea v-model="draft.skills_text" class="textarea mono" rows="6" />
            </label>
          </div>
          <label class="field">
            <span class="fl">server_overrides (JSON)</span>
            <textarea v-model="draft.server_overrides_text" class="textarea mono" rows="6" />
          </label>
        </div>

        <footer class="editor-foot">
          <span v-if="error" class="msg error">✕ {{ error }}</span>
          <span v-else-if="successMsg" class="msg ok">✓ {{ successMsg }}</span>
          <span v-else class="msg muted">Allowed fields: instruction, servers, skills, server_overrides, model, role_display.</span>
        </footer>
      </section>

      <aside class="history-rail">
        <div class="tree-eyebrow">HISTORY</div>
        <div v-if="!history.length" class="empty">No edits yet.</div>
        <div
          v-for="row in history"
          :key="row.id"
          class="hist-row"
          :class="{ rollback: row.source === 'rollback', 'yaml-reset': row.source === 'yaml-reset' }"
        >
          <div class="hist-line">
            <code class="hist-role">{{ row.role }}.{{ row.field }}</code>
            <span class="hist-src">{{ row.source }}</span>
          </div>
          <div class="hist-meta">
            #{{ row.id }} · {{ row.edited_by }} · {{ fmtTime(row.edited_at) }}
          </div>
          <div class="hist-diff">
            <div><span class="muted">−</span> <code>{{ jsonPreview(row.before) }}</code></div>
            <div><span class="muted">+</span> <code>{{ jsonPreview(row.after) }}</code></div>
          </div>
          <div v-if="row.comment" class="hist-comment">{{ row.comment }}</div>
          <button
            type="button"
            class="btn-mini"
            :disabled="rollingBack === row.id"
            @click="onRollback(row.id)"
          >{{ rollingBack === row.id ? '…' : 'Rollback' }}</button>
        </div>
      </aside>
    </div>

    <!-- yaml-vs-DB diff viewer. Per-role section, per-field before/after.
         Reads yamlDiff (already fetched in loadTemplate) — no extra
         round-trip. In-sync roles are filtered out — they'd just be
         noise on a "diff" screen. -->
    <Teleport to="body">
      <Transition name="reset-modal">
        <div v-if="diffModal.visible" class="reset-overlay" @click.self="onCloseDiff">
          <div class="diff-card">
            <header class="diff-head">
              <div>
                <h3 class="reset-title">Yaml vs running template</h3>
                <p class="diff-path" v-if="yamlDiff?.yaml_path">
                  <code>{{ yamlDiff.yaml_path }}</code>
                </p>
              </div>
              <div class="diff-head-actions">
                <div class="layout-toggle" role="tablist" aria-label="Diff layout">
                  <button
                    type="button"
                    :class="{ active: diffLayout === 'unified' }"
                    role="tab"
                    @click="diffLayout = 'unified'"
                  >Unified</button>
                  <button
                    type="button"
                    :class="{ active: diffLayout === 'side-by-side' }"
                    role="tab"
                    @click="diffLayout = 'side-by-side'"
                  >Side-by-side</button>
                </div>
                <button type="button" class="r-btn cancel diff-close" @click="onCloseDiff">Close</button>
              </div>
            </header>

            <div class="diff-body">
              <div v-if="!diffRolesOrdered.length" class="diff-empty">
                In sync — no diverged roles.
              </div>
              <section
                v-for="[role, info] in diffRolesOrdered"
                :key="role"
                class="diff-role"
                :class="{ 'is-active': role === activeRole }"
              >
                <header class="diff-role-head">
                  <code class="diff-role-name">{{ role }}</code>
                  <span class="diff-role-status" :class="info.status">
                    {{ info.status === 'diverged'
                       ? `${Object.keys(info.fields || {}).length} field(s) drift`
                       : info.status.replace('_', ' ') }}
                  </span>
                </header>

                <!-- Field-level rows for diverged roles. v-code-diff
                     renders Myers + intra-line word highlight + syntax
                     highlight via highlight.js. Layout toggles in header. -->
                <div v-if="info.status === 'diverged'" class="diff-fields">
                  <div
                    v-for="[field, change] in Object.entries(info.fields || {})"
                    :key="field"
                    class="diff-field"
                  >
                    <div class="diff-field-name">
                      <code>{{ field }}</code>
                    </div>
                    <CodeDiff
                      :old-string="formatDiffValue(change.before)"
                      :new-string="formatDiffValue(change.after)"
                      :output-format="diffLayout"
                      :language="diffLanguageFor(field)"
                      :context="3"
                      filename="YAML (factory)"
                      new-filename="DB (running)"
                      theme="dark"
                    />
                  </div>
                </div>

                <!-- Whole-role status (added/removed) — no field-level data -->
                <div v-else-if="info.status === 'added_in_db'" class="diff-fields">
                  <p class="diff-note">Role exists in DB only — not present in factory yaml. Saving the yaml would lose this role on next recreate.</p>
                  <pre class="diff-pre">{{ formatDiffValue(info.current) }}</pre>
                </div>
                <div v-else-if="info.status === 'removed_from_db'" class="diff-fields">
                  <p class="diff-note">Role exists in factory yaml only — removed from DB. Reset / reload would restore it.</p>
                  <pre class="diff-pre">{{ formatDiffValue(info.yaml) }}</pre>
                </div>
              </section>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- Reset-to-yaml chooser. Two-action modal: the user has to decide
         whether running agents pick up the change immediately (force
         reload) or only on next natural restart. Cancel returns to the
         editor without touching DB. -->
    <Teleport to="body">
      <Transition name="reset-modal">
        <div
          v-if="resetModal.visible"
          class="reset-overlay"
          @click.self="onResetModalCancel"
        >
          <div class="reset-card">
            <div class="reset-icon">↺</div>
            <h3 class="reset-title">Reset <code>{{ activeRole }}</code> to factory yaml?</h3>
            <p class="reset-body">
              Pulls every editable field of <strong>{{ activeRole }}</strong> from
              <code>team_templates/{{ template?.name?.replaceAll('-', '_') || 'agile_team' }}.yaml</code>
              into this team's DB. Other roles are untouched and audited.
            </p>
            <p class="reset-body muted">
              Already-running agents keep their stale in-memory template
              until the next restart — pick whether to force-respawn them now.
            </p>
            <div class="reset-actions">
              <button
                type="button"
                class="r-btn cancel"
                :disabled="resetModal.busy"
                @click="onResetModalCancel"
              >Cancel</button>
              <button
                type="button"
                class="r-btn sync"
                :disabled="resetModal.busy"
                @click="onResetModalConfirm('sync-only')"
              >
                <span v-if="resetModal.busy && resetModal.mode === 'sync-only'" class="r-spin"></span>
                Sync yaml only
              </button>
              <button
                type="button"
                class="r-btn reload"
                :disabled="resetModal.busy"
                @click="onResetModalConfirm('sync-and-reload')"
              >
                <span v-if="resetModal.busy && resetModal.mode === 'sync-and-reload'" class="r-spin"></span>
                Sync + force reload
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.running-tmpl {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  color: var(--text);
}
.banner {
  display: flex;
  gap: 10px;
  padding: 10px 12px;
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-left: 3px solid #d4a017;
  border-radius: 6px;
  font-size: 12.5px;
}
.banner code {
  background: var(--bg-0);
  padding: 1px 6px;
  border-radius: 3px;
  font-family: var(--font-mono);
  font-size: 11.5px;
}
.banner-icon {
  font-size: 16px;
  line-height: 1;
}
.session-bar {
  display: flex;
  align-items: center;
  gap: 10px;
}
.session-label {
  font-family: var(--font-mono);
  font-size: 10.5px;
  letter-spacing: 0.16em;
  color: var(--text-dim);
}
.session-select {
  flex: 1;
  min-width: 200px;
  padding: 6px 10px;
  background: var(--bg-1);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 4px;
}
.drift-pill {
  padding: 3px 9px;
  font-size: 11px;
  font-family: var(--font-mono);
  border-radius: 999px;
  background: rgba(212, 160, 23, 0.18);
  color: #d4a017;
  border: 1px solid rgba(212, 160, 23, 0.32);
}
.drift-pill.insync {
  background: rgba(46, 160, 67, 0.18);
  color: #2ea043;
  border-color: rgba(46, 160, 67, 0.32);
}
.layout {
  display: grid;
  grid-template-columns: 200px 1fr 320px;
  gap: 12px;
  min-height: 0;
  flex: 1;
}
.role-list, .history-rail {
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  overflow-y: auto;
}
.tree-eyebrow {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.16em;
  color: var(--text-dim);
  padding: 0 2px 6px;
}
.role-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border: none;
  background: transparent;
  color: var(--text);
  border-radius: 4px;
  text-align: left;
  cursor: pointer;
  font-size: 12.5px;
}
.role-item:hover { background: var(--bg-0); }
.role-item.active {
  background: rgba(96, 134, 199, 0.18);
  box-shadow: inset 2px 0 0 var(--primary, #5b8def);
}
.role-name { flex: 1; font-weight: 500; }
.role-key { color: var(--text-dim); font-size: 11px; }
.drift-dot { color: #d4a017; font-weight: 700; }
.empty { color: var(--text-dim); font-style: italic; padding: 8px; }
.editor {
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.editor-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  gap: 12px;
}
.editor-head .title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13.5px;
}
.editor-head .title code {
  background: var(--bg-0);
  padding: 2px 8px;
  border-radius: 4px;
  font-family: var(--font-mono);
}
.display-input {
  background: transparent;
  border: 1px dashed var(--border);
  color: var(--text);
  padding: 2px 8px;
  font-size: 12.5px;
  border-radius: 3px;
  min-width: 140px;
}
.actions { display: flex; gap: 8px; align-items: center; }
.pill.dirty {
  padding: 3px 10px;
  font-size: 10.5px;
  font-family: var(--font-mono);
  color: #f0883e;
  background: rgba(240, 136, 62, 0.16);
  border-radius: 999px;
}
.btn {
  padding: 6px 12px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg-1);
  color: var(--text);
  font-size: 12.5px;
  cursor: pointer;
}
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.ghost { background: transparent; }
.btn.primary { background: var(--primary, #5b8def); border-color: var(--primary, #5b8def); color: #fff; }
.btn.danger { background: #b54545; border-color: #b54545; color: #fff; }
.editor-body {
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  overflow-y: auto;
  flex: 1;
}
.field { display: flex; flex-direction: column; gap: 4px; }
.field-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.fl {
  font-family: var(--font-mono);
  font-size: 10.5px;
  letter-spacing: 0.12em;
  color: var(--text-dim);
}
.text, .textarea {
  background: var(--bg-0);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 12.5px;
}
.textarea { resize: vertical; }
.textarea.mono { font-family: var(--font-mono); font-size: 11.5px; }
.editor-foot {
  padding: 8px 14px;
  border-top: 1px solid var(--border);
  font-size: 11.5px;
}
.msg.error { color: #f85149; }
.msg.ok { color: #2ea043; }
.msg.muted { color: var(--text-dim); }
.history-rail .hist-row {
  border-top: 1px solid var(--border);
  padding: 8px 6px;
  font-size: 11.5px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.history-rail .hist-row:first-of-type { border-top: none; }
.history-rail .hist-row.rollback { background: rgba(91, 141, 239, 0.06); }
.history-rail .hist-row.yaml-reset { background: rgba(46, 160, 67, 0.06); }
.hist-line { display: flex; justify-content: space-between; }
.hist-role { font-family: var(--font-mono); color: var(--text); }
.hist-src { font-size: 10px; color: var(--text-dim); font-family: var(--font-mono); }
.hist-meta { color: var(--text-dim); font-size: 10.5px; }
.hist-diff code { font-family: var(--font-mono); font-size: 11px; }
.hist-comment { color: var(--text-dim); font-style: italic; }
.btn-mini {
  align-self: flex-start;
  padding: 2px 8px;
  font-size: 10.5px;
  border-radius: 3px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text);
  cursor: pointer;
}
.btn-mini:hover { background: var(--bg-0); }
.overlay { padding: 30px; text-align: center; color: var(--text-dim); }

/* Reset-to-yaml chooser modal — sits above all app modals (matches
   ConfirmModal z-index policy). */
.reset-overlay {
  position: fixed;
  inset: 0;
  z-index: 10001;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(6px);
}
.reset-card {
  width: 480px;
  max-width: 92vw;
  padding: 28px;
  background: var(--bg-2, #1a1d24);
  border: 1px solid var(--border-bright, #3a3f4a);
  border-radius: 12px;
  text-align: center;
  box-shadow: 0 20px 40px -10px rgba(0, 0, 0, 0.6);
}
.reset-icon {
  width: 48px;
  height: 48px;
  margin: 0 auto 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 22px;
  border: 1px solid rgba(91, 141, 239, 0.32);
  border-radius: 12px;
  color: #5b8def;
  background: rgba(91, 141, 239, 0.12);
}
.reset-title {
  margin: 0 0 10px;
  font-size: 17px;
  font-weight: 600;
}
.reset-title code {
  font-family: var(--font-mono);
  font-size: 14px;
  background: var(--bg-0);
  padding: 2px 8px;
  border-radius: 4px;
}
.reset-body {
  margin: 0 0 10px;
  font-size: 12.5px;
  color: var(--text-dim);
  line-height: 1.6;
}
.reset-body code {
  font-family: var(--font-mono);
  font-size: 11.5px;
  background: var(--bg-0);
  padding: 1px 6px;
  border-radius: 3px;
}
.reset-body.muted { font-size: 11.5px; }
.reset-actions {
  display: flex;
  gap: 8px;
  justify-content: center;
  margin-top: 18px;
  flex-wrap: wrap;
}
.r-btn {
  flex: 1 1 auto;
  min-width: 120px;
  padding: 10px 14px;
  border-radius: 6px;
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  border: 1px solid;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}
.r-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.r-btn.cancel {
  background: transparent;
  border-color: var(--border, #2d3038);
  color: var(--text-dim);
}
.r-btn.sync {
  background: rgba(46, 160, 67, 0.12);
  border-color: rgba(46, 160, 67, 0.32);
  color: #2ea043;
}
.r-btn.reload {
  background: rgba(181, 69, 69, 0.18);
  border-color: rgba(181, 69, 69, 0.4);
  color: #ff7676;
}
.r-btn:not(:disabled):hover { filter: brightness(1.15); }
.r-spin {
  width: 12px;
  height: 12px;
  border: 2px solid transparent;
  border-top-color: currentColor;
  border-radius: 50%;
  animation: r-spin 0.6s linear infinite;
}
@keyframes r-spin { to { transform: rotate(360deg); } }
.reset-modal-enter-active,
.reset-modal-leave-active { transition: opacity 0.18s; }
.reset-modal-enter-from,
.reset-modal-leave-to { opacity: 0; }

/* Yaml-vs-DB diff modal — wider than the chooser because it renders
   field-level before/after side-by-side. */
.diff-card {
  width: 920px;
  max-width: 94vw;
  max-height: 86vh;
  padding: 0;
  background: var(--bg-2, #1a1d24);
  border: 1px solid var(--border-bright, #3a3f4a);
  border-radius: 12px;
  box-shadow: 0 20px 40px -10px rgba(0, 0, 0, 0.6);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.diff-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 16px 22px;
  border-bottom: 1px solid var(--border);
  gap: 12px;
}
.diff-path {
  margin: 4px 0 0;
  font-size: 11.5px;
  color: var(--text-dim);
}
.diff-path code {
  font-family: var(--font-mono);
  background: var(--bg-0);
  padding: 1px 6px;
  border-radius: 3px;
}
.diff-close { flex: 0 0 auto; min-width: 80px; }
.diff-body {
  overflow-y: auto;
  padding: 12px 22px 22px;
  flex: 1;
}
.diff-empty {
  text-align: center;
  padding: 40px;
  color: var(--text-dim);
  font-style: italic;
}
.diff-role {
  border: 1px solid var(--border);
  border-radius: 6px;
  margin-top: 12px;
  background: var(--bg-1);
}
.diff-role.is-active { border-color: rgba(91, 141, 239, 0.45); }
.diff-role-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
}
.diff-role-name {
  font-family: var(--font-mono);
  font-size: 13px;
  background: var(--bg-0);
  padding: 2px 8px;
  border-radius: 4px;
}
.diff-role-status {
  font-family: var(--font-mono);
  font-size: 10.5px;
  padding: 2px 9px;
  border-radius: 999px;
  background: rgba(212, 160, 23, 0.18);
  color: #d4a017;
}
.diff-role-status.added_in_db {
  background: rgba(91, 141, 239, 0.18);
  color: #5b8def;
}
.diff-role-status.removed_from_db {
  background: rgba(181, 69, 69, 0.18);
  color: #ff7676;
}
.diff-fields { padding: 12px 14px; display: flex; flex-direction: column; gap: 14px; }
.diff-field-name {
  margin-bottom: 5px;
  font-size: 11.5px;
  color: var(--text-dim);
}
.diff-field-name code {
  font-family: var(--font-mono);
  background: var(--bg-0);
  padding: 1px 6px;
  border-radius: 3px;
  color: var(--text);
}
/* v-code-diff renders its own coloured diff; we only need to constrain
   its max-height so a 200-line instruction doesn't blow out the modal. */
.diff-field :deep(.d2h-wrapper),
.diff-field :deep(.d2h-file-list-wrapper),
.diff-field :deep(.code-diff-container),
.diff-field :deep(table) {
  font-size: 11.5px;
}
.diff-field :deep(.code-diff-container) {
  max-height: 440px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: 5px;
}

/* Header layout toggle — segmented control matching the rest of the UI. */
.diff-head-actions { display: flex; align-items: center; gap: 10px; }
.layout-toggle {
  display: inline-flex;
  border: 1px solid var(--border);
  border-radius: 5px;
  overflow: hidden;
  background: var(--bg-1);
}
.layout-toggle button {
  background: transparent;
  border: none;
  color: var(--text-dim);
  padding: 5px 12px;
  font-size: 11.5px;
  cursor: pointer;
  font-family: var(--font-body);
}
.layout-toggle button.active {
  background: rgba(91, 141, 239, 0.18);
  color: #5b8def;
}
.diff-note {
  margin: 0 0 8px;
  font-size: 11.5px;
  color: var(--text-dim);
  font-style: italic;
}
@media (max-width: 720px) {
  .diff-grid { grid-template-columns: 1fr; }
}
</style>
