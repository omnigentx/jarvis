<script setup>
/**
 * Skills Library — global view of every skill on disk.
 *
 * Anchors the skills system as a first-class resource: skills aren't just
 * "things attached to agents" but a library users curate. Lets users:
 *   - browse all skills (built-in + user-created, attached or orphan)
 *   - filter & search to find one
 *   - edit any skill (built-in editable, only delete locked)
 *   - delete user-created skills (with cleanup of agent card refs)
 *   - attach a skill to an agent (lazy via picker — opens an inline menu)
 *
 * Newly-created skills appear here even before they're wired into any agent,
 * fixing the "create then can't find it again" gap that existed when the
 * only entry point was Agent Detail's Skills tab.
 */
import { ref, computed, onMounted } from 'vue'
import { apiFetch, ApiError } from '../api'
import { useAgentsStore } from '../stores/agents'
import { useConfirm } from '../composables/useConfirm'
import SkillEditorModal from '../components/agent/SkillEditorModal.vue'
import SkillDeleteModal from '../components/agent/SkillDeleteModal.vue'

const store = useAgentsStore()
const { confirm } = useConfirm()

const skills = ref([])
const loading = ref(false)
const loadError = ref('')
const search = ref('')
const filterMode = ref('all') // all | builtin | user | orphan

// Editor / delete modal state.
const editorVisible = ref(false)
const editorMode = ref('edit') // edit | create
const editorTarget = ref('')
const deleteVisible = ref(false)
const deleteTarget = ref({ name: '', usedBy: [] })

// Attach picker state.
const attachOpenFor = ref('') // skill name whose dropdown is open
const attachBusy = ref(false)
const attachToast = ref('')

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  return skills.value.filter((s) => {
    if (filterMode.value === 'builtin' && !s.is_builtin) return false
    if (filterMode.value === 'user' && s.is_builtin) return false
    if (filterMode.value === 'orphan' && (s.used_by || []).length > 0) return false
    if (!q) return true
    const hay = (s.name + ' ' + (s.description || '')).toLowerCase()
    return hay.includes(q)
  })
})

// All known agents (for the Attach picker), labeled with persistence semantics.
const agentChoices = computed(() => {
  return [...store.agents.values()].map((a) => ({
    name: a.name,
    type: a.type, // 'card' or 'builtin' (= code-based)
    is_card_based: a.type === 'card',
  }))
})

async function loadSkills() {
  loading.value = true
  loadError.value = ''
  try {
    const data = await apiFetch('/api/skills')
    skills.value = data.skills || []
  } catch (err) {
    loadError.value = _friendly(err)
  } finally {
    loading.value = false
  }
}

function _friendly(err) {
  if (err instanceof ApiError && err.body && typeof err.body === 'object') {
    const detail = err.body.detail
    if (detail && typeof detail === 'object') return detail.message || 'Request failed.'
    if (typeof detail === 'string') return detail
  }
  return err?.message || String(err)
}

function openCreate() {
  editorMode.value = 'create'
  editorTarget.value = ''
  editorVisible.value = true
}
function openEdit(skill) {
  editorMode.value = 'edit'
  editorTarget.value = skill.name
  editorVisible.value = true
}
function openDelete(skill) {
  deleteTarget.value = { name: skill.name, usedBy: skill.used_by || [] }
  deleteVisible.value = true
}

async function onSaved() {
  await loadSkills()
}
async function onDeleted() {
  deleteVisible.value = false
  await loadSkills()
}

function toggleAttachMenu(name) {
  attachOpenFor.value = attachOpenFor.value === name ? '' : name
}

async function attachToAgent(skill, agent) {
  // Confirm runtime-only impact for code-based agents.
  if (!agent.is_card_based) {
    const proceed = await confirm({
      title: `Attach to ${agent.name}?`,
      message:
        `${agent.name} is defined in code (agent.py), so this attachment is ` +
        `runtime-only — it will revert on backend restart unless you also ` +
        `add '${skill.name}' to ${agent.name}'s get_skills(...) call.`,
      confirmText: 'Attach (runtime only)',
      variant: 'warning',
    })
    if (!proceed) return
  }
  attachBusy.value = true
  attachToast.value = ''
  try {
    const res = await apiFetch(
      `/api/skills/${encodeURIComponent(skill.name)}/agents/${encodeURIComponent(agent.name)}`,
      { method: 'PUT' },
    )
    attachToast.value = res.persisted
      ? `Attached '${skill.name}' to ${agent.name}.`
      : `Attached '${skill.name}' to ${agent.name} — runtime only, will revert on restart.`
    attachOpenFor.value = ''
    await loadSkills()
    setTimeout(() => (attachToast.value = ''), 4000)
  } catch (err) {
    attachToast.value = `Attach failed: ${_friendly(err)}`
  } finally {
    attachBusy.value = false
  }
}

async function detachFromAgent(skill, agentName) {
  const proceed = await confirm({
    title: `Detach from ${agentName}?`,
    message: `'${skill.name}' will be removed from ${agentName}. The skill itself stays in the library.`,
    confirmText: 'Detach',
    variant: 'warning',
  })
  if (!proceed) return
  try {
    await apiFetch(
      `/api/skills/${encodeURIComponent(skill.name)}/agents/${encodeURIComponent(agentName)}`,
      { method: 'DELETE' },
    )
    await loadSkills()
  } catch (err) {
    attachToast.value = `Detach failed: ${_friendly(err)}`
    setTimeout(() => (attachToast.value = ''), 4000)
  }
}

function unattachedAgents(skill) {
  const used = new Set(skill.used_by || [])
  return agentChoices.value.filter((a) => !used.has(a.name))
}

onMounted(() => {
  if (!store.agents.size) store.fetchAgents()
  loadSkills()
})
</script>

<template>
  <div class="skills-library">
    <header class="header">
      <div class="header-text">
        <h1>Skills Library</h1>
        <p>
          All skills available to your agents. Editing one updates every
          agent that references it. Built-in skills can be edited but not
          deleted.
        </p>
      </div>
      <button class="btn-primary" @click="openCreate">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
          <line x1="12" y1="5" x2="12" y2="19"/>
          <line x1="5" y1="12" x2="19" y2="12"/>
        </svg>
        New skill
      </button>
    </header>

    <div class="toolbar">
      <input
        v-model="search"
        type="search"
        placeholder="Search skills…"
        class="search-input"
      />
      <div class="filter-tabs">
        <button :class="{ active: filterMode === 'all' }" @click="filterMode = 'all'">All</button>
        <button :class="{ active: filterMode === 'builtin' }" @click="filterMode = 'builtin'">Built-in</button>
        <button :class="{ active: filterMode === 'user' }" @click="filterMode = 'user'">User-created</button>
        <button :class="{ active: filterMode === 'orphan' }" @click="filterMode = 'orphan'">Orphan</button>
      </div>
    </div>

    <p v-if="attachToast" class="toast">{{ attachToast }}</p>
    <p v-if="loadError" class="error">{{ loadError }} <button @click="loadSkills">Retry</button></p>

    <div v-if="loading" class="empty muted">Loading skills…</div>
    <div v-else-if="!filtered.length" class="empty">
      <span v-if="search || filterMode !== 'all'">No skills match your filters.</span>
      <span v-else>
        Library is empty.
        <button class="link" @click="openCreate">Create your first skill →</button>
      </span>
    </div>

    <div v-else class="skill-list">
      <div v-for="s in filtered" :key="s.name" class="skill-row">
        <div class="skill-icon">⚡</div>
        <div class="skill-body">
          <div class="skill-title">
            <span class="name">{{ s.name }}</span>
            <span v-if="s.is_builtin" class="badge badge-builtin">Built-in</span>
            <span v-if="s.parse_error" class="badge badge-warn" :title="s.parse_error">Parse error</span>
          </div>
          <div class="skill-desc">
            {{ s.description || (s.parse_error ? '(unparseable frontmatter)' : '—') }}
          </div>
          <div class="skill-meta">
            <span v-if="s.used_by?.length" class="used-by">
              Used by {{ s.used_by.length }}: {{ s.used_by.join(', ') }}
            </span>
            <span v-else class="used-by muted">Not attached</span>
            <button
              class="link link-detach"
              v-for="a in s.used_by || []"
              :key="a"
              type="button"
              :title="`Detach from ${a}`"
              @click="detachFromAgent(s, a)"
            >× {{ a }}</button>
          </div>
        </div>
        <div class="skill-actions">
          <div class="attach-wrap">
            <button class="btn-secondary" @click="toggleAttachMenu(s.name)">
              Attach to…
            </button>
            <div v-if="attachOpenFor === s.name" class="attach-menu" @click.stop>
              <div v-if="!unattachedAgents(s).length" class="attach-empty">
                Already attached to all agents.
              </div>
              <button
                v-for="a in unattachedAgents(s)"
                :key="a.name"
                class="attach-item"
                :disabled="attachBusy"
                @click="attachToAgent(s, a)"
              >
                <span class="attach-agent-name">{{ a.name }}</span>
                <span v-if="!a.is_card_based" class="attach-warn-pill" title="Code-based agent — change reverts on restart">
                  runtime only
                </span>
              </button>
            </div>
          </div>
          <button class="icon-btn" :title="`Edit ${s.name}`" @click="openEdit(s)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
            </svg>
          </button>
          <button
            class="icon-btn icon-btn-danger"
            :title="s.is_builtin ? 'Built-in skills cannot be deleted' : `Delete ${s.name}`"
            :disabled="s.is_builtin"
            @click="openDelete(s)"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <polyline points="3 6 5 6 21 6"/>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
            </svg>
          </button>
        </div>
      </div>
    </div>

    <SkillEditorModal
      :visible="editorVisible"
      :mode="editorMode"
      :skill-name="editorTarget"
      @close="editorVisible = false"
      @saved="onSaved"
    />
    <SkillDeleteModal
      :visible="deleteVisible"
      :skill-name="deleteTarget.name"
      :used-by="deleteTarget.usedBy"
      @close="deleteVisible = false"
      @deleted="onDeleted"
    />
  </div>
</template>

<style scoped>
.skills-library {
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px;
}
.header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}
.header h1 {
  margin: 0 0 6px;
  font-size: 22px;
  color: #f0f2f5;
}
.header p {
  margin: 0;
  font-size: 13px;
  color: #8b8fa3;
  max-width: 700px;
}
.btn-primary {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  background: #3b82f6;
  border: 1px solid #3b82f6;
  color: white;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}
.btn-primary:hover { background: #2563eb; border-color: #2563eb; }

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.search-input {
  flex: 1;
  min-width: 220px;
  padding: 8px 14px;
  background: #0c0e15;
  border: 1px solid #1a1d2e;
  border-radius: 8px;
  color: #f0f2f5;
  font-size: 13px;
}
.search-input:focus {
  outline: none;
  border-color: #3b82f6;
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
}
.filter-tabs {
  display: flex;
  background: #0c0e15;
  border: 1px solid #1a1d2e;
  border-radius: 8px;
  padding: 2px;
}
.filter-tabs button {
  padding: 6px 14px;
  background: transparent;
  border: none;
  color: #8b8fa3;
  font-size: 12px;
  font-weight: 500;
  border-radius: 6px;
  cursor: pointer;
}
.filter-tabs button:hover:not(.active) { color: #c4c8d4; }
.filter-tabs button.active {
  background: #1e2233;
  color: #f0f2f5;
}

.toast {
  margin: 0 0 12px;
  padding: 8px 12px;
  background: rgba(59, 130, 246, 0.08);
  border: 1px solid rgba(59, 130, 246, 0.2);
  border-radius: 8px;
  color: #93c5fd;
  font-size: 13px;
}
.error {
  margin: 0 0 12px;
  padding: 8px 12px;
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.2);
  border-radius: 8px;
  color: #f87171;
  font-size: 13px;
}
.error button {
  margin-left: 8px;
  background: transparent;
  border: 1px solid rgba(239, 68, 68, 0.3);
  color: #f87171;
  padding: 2px 10px;
  border-radius: 4px;
  cursor: pointer;
}

.empty {
  padding: 40px 20px;
  text-align: center;
  color: #8b8fa3;
  font-size: 13px;
  background: #0c0e15;
  border: 1px dashed #1a1d2e;
  border-radius: 12px;
}
.empty.muted { color: #555872; }

.link {
  background: none;
  border: none;
  color: #60a5fa;
  font-size: 13px;
  cursor: pointer;
  padding: 0;
}
.link:hover { color: #93c5fd; }

.skill-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.skill-row {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px 16px;
  background: #0c0e15;
  border: 1px solid #1a1d2e;
  border-radius: 12px;
  transition: border-color 0.15s;
}
.skill-row:hover { border-color: #2a3556; }
.skill-icon {
  font-size: 18px;
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 212, 170, 0.08);
  border-radius: 10px;
  flex-shrink: 0;
}
.skill-body { flex: 1; min-width: 0; }
.skill-title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 4px;
  flex-wrap: wrap;
}
.skill-title .name {
  color: #f0f2f5;
  font-weight: 600;
  font-size: 14px;
}
.badge {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 2px 7px;
  border-radius: 4px;
}
.badge-builtin {
  background: rgba(59, 130, 246, 0.15);
  color: #60a5fa;
  border: 1px solid rgba(59, 130, 246, 0.25);
}
.badge-warn {
  background: rgba(245, 158, 11, 0.15);
  color: #fbbf24;
  border: 1px solid rgba(245, 158, 11, 0.25);
}
.skill-desc {
  font-size: 12px;
  color: #8b8fa3;
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  line-clamp: 2;
}
.skill-meta {
  font-size: 12px;
  color: #555872;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.used-by { color: #c4c8d4; }
.used-by.muted { color: #555872; font-style: italic; }
.link-detach {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(239, 68, 68, 0.06);
  color: #fca5a5;
  border: 1px solid rgba(239, 68, 68, 0.2);
}
.link-detach:hover { background: rgba(239, 68, 68, 0.15); color: #f87171; }

.skill-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.btn-secondary {
  padding: 7px 14px;
  background: #111318;
  border: 1px solid #1a1d2e;
  color: #c4c8d4;
  border-radius: 8px;
  font-size: 12px;
  cursor: pointer;
  font-weight: 500;
}
.btn-secondary:hover {
  background: #1e2233;
  color: #f0f2f5;
  border-color: #2a3556;
}

.attach-wrap { position: relative; }
.attach-menu {
  position: absolute;
  right: 0;
  top: calc(100% + 4px);
  min-width: 220px;
  background: #0c0e15;
  border: 1px solid #1a1d2e;
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
  z-index: 50;
  max-height: 280px;
  overflow-y: auto;
}
.attach-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 8px 12px;
  background: transparent;
  border: none;
  color: #c4c8d4;
  font-size: 13px;
  cursor: pointer;
  text-align: left;
  gap: 10px;
}
.attach-item:hover:not(:disabled) {
  background: #1e2233;
  color: #f0f2f5;
}
.attach-item:disabled { opacity: 0.5; cursor: not-allowed; }
.attach-empty { padding: 12px; color: #555872; font-size: 12px; font-style: italic; }
.attach-agent-name { flex: 1; min-width: 0; }
.attach-warn-pill {
  font-size: 10px;
  padding: 2px 6px;
  background: rgba(245, 158, 11, 0.1);
  color: #fbbf24;
  border-radius: 4px;
  border: 1px solid rgba(245, 158, 11, 0.2);
  white-space: nowrap;
}

.icon-btn {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 1px solid transparent;
  color: #8b8fa3;
  border-radius: 7px;
  cursor: pointer;
}
.icon-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.04);
  color: #f0f2f5;
  border-color: rgba(255, 255, 255, 0.06);
}
.icon-btn:disabled { opacity: 0.35; cursor: not-allowed; }
.icon-btn-danger:hover:not(:disabled) {
  background: rgba(239, 68, 68, 0.1);
  color: #f87171;
  border-color: rgba(239, 68, 68, 0.25);
}

@media (max-width: 640px) {
  .skill-row { flex-wrap: wrap; }
  .skill-actions { margin-left: auto; }
}
</style>
