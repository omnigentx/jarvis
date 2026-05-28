<script setup>
/**
 * Skills Library — global view of every skill on disk.
 *
 * Logic preserved verbatim from the previous SkillsLibraryView; only
 * the markup + styles were rewritten to match the redesign (eyebrow,
 * mono labels, category filter chips, orphan badge, hairline rows).
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
const categoryFilter = ref('All')

// Editor / delete modal state.
const editorVisible = ref(false)
const editorMode = ref('edit')
const editorTarget = ref('')
const deleteVisible = ref(false)
const deleteTarget = ref({ name: '', usedBy: [] })

// Attach picker state.
const attachOpenFor = ref('')
const attachBusy = ref(false)
const attachToast = ref('')

// Categories are inferred from the skill's first used-by path segment
// (e.g. `agile/PM` → `PM`) or fall back to source bucket.
function _categoryFor(s) {
  if (!s.is_builtin) return 'User'
  const u = (s.used_by || [])
  if (!u.length) return 'Meta'
  const first = u[0]
  if (first === '*') return 'Static'
  if (first.includes('/')) return first.split('/')[1].toUpperCase()
  return 'Static'
}

const categories = computed(() => {
  const set = new Set(['All'])
  for (const s of skills.value) set.add(_categoryFor(s))
  return Array.from(set)
})

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  return skills.value.filter((s) => {
    if (filterMode.value === 'builtin' && !s.is_builtin) return false
    if (filterMode.value === 'user' && s.is_builtin) return false
    if (filterMode.value === 'orphan' && (s.used_by || []).length > 0) return false
    if (categoryFilter.value !== 'All' && _categoryFor(s) !== categoryFilter.value) return false
    if (!q) return true
    const hay = (s.name + ' ' + (s.description || '')).toLowerCase()
    return hay.includes(q)
  })
})

const stats = computed(() => ({
  total: skills.value.length,
  builtin: skills.value.filter(s => s.is_builtin).length,
  user: skills.value.filter(s => !s.is_builtin).length,
  orphan: skills.value.filter(s => (s.used_by || []).length === 0).length,
}))

const categoryCounts = computed(() => {
  const map = { All: skills.value.length }
  for (const s of skills.value) {
    const c = _categoryFor(s)
    map[c] = (map[c] || 0) + 1
  }
  return map
})

const agentChoices = computed(() => {
  return [...store.agents.values()].map((a) => ({
    name: a.name,
    type: a.type,
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

function categoryFor(skill) { return _categoryFor(skill) }
function isOrphan(skill) { return !(skill.used_by || []).length }

onMounted(() => {
  if (!store.agents.size) store.fetchAgents()
  loadSkills()
})
</script>

<template>
  <div class="skills jv">
    <!-- ─── Header ─── -->
    <div class="skills__header">
      <div class="skills__heading">
        <div class="eyebrow">SYSTEM · SKILLS LIBRARY</div>
        <h1 class="skills__title">
          <span class="grad" style="font-style: italic;">{{ stats.total }}</span> skills
          <span class="skills__title-sub">
            · {{ stats.builtin }} built-in · {{ stats.user }} user · {{ stats.orphan }} orphan
          </span>
        </h1>
        <p class="skills__desc">
          Markdown files in <code class="skills__inline-code">.fast-agent/skills/</code>.
          Editing here updates every agent that references the skill.
          Built-ins are editable but cannot be deleted.
        </p>
      </div>
      <div class="skills__header-actions">
        <button class="btn btn-primary" @click="openCreate">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
            <line x1="12" y1="5" x2="12" y2="19"/>
            <line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
          New skill
        </button>
      </div>
    </div>

    <!-- ─── Search + source filter ─── -->
    <div class="skills__toolbar">
      <div class="skills__search">
        <svg viewBox="0 0 24 24" fill="none" width="14" height="14" stroke="currentColor" stroke-width="1.8">
          <circle cx="11" cy="11" r="7"/>
          <line x1="21" y1="21" x2="16.65" y2="16.65" stroke-linecap="round"/>
        </svg>
        <input
          v-model="search"
          type="search"
          placeholder="Search skills…"
          class="skills__search-input"
        />
      </div>
      <div class="seg">
        <button :class="{ 'is-active': filterMode === 'all' }" @click="filterMode = 'all'">All</button>
        <button :class="{ 'is-active': filterMode === 'builtin' }" @click="filterMode = 'builtin'">Built-in</button>
        <button :class="{ 'is-active': filterMode === 'user' }" @click="filterMode = 'user'">User</button>
        <button :class="{ 'is-active': filterMode === 'orphan' }" @click="filterMode = 'orphan'">Orphan</button>
      </div>
    </div>

    <!-- ─── Category chips ─── -->
    <div class="skills__categories">
      <span class="mono-label">FILTER</span>
      <button
        v-for="c in categories"
        :key="c"
        class="skills__chip"
        :class="{ 'skills__chip--active': categoryFilter === c }"
        @click="categoryFilter = c"
      >
        {{ c }}
        <span class="skills__chip-count">{{ categoryCounts[c] || 0 }}</span>
      </button>
    </div>

    <p v-if="attachToast" class="skills__toast">{{ attachToast }}</p>
    <p v-if="loadError" class="skills__error">
      {{ loadError }}
      <button class="btn btn-secondary btn-icon" @click="loadSkills">Retry</button>
    </p>

    <!-- ─── List ─── -->
    <div v-if="loading" class="skills__empty">Loading skills…</div>
    <div v-else-if="!filtered.length" class="skills__empty">
      <span v-if="search || filterMode !== 'all' || categoryFilter !== 'All'">
        No skills match the current filter.
      </span>
      <span v-else>
        Library is empty.
        <button class="skills__link" @click="openCreate">Create your first skill →</button>
      </span>
    </div>

    <div v-else class="skills__list">
      <div
        v-for="s in filtered"
        :key="s.name"
        class="skill-row"
        :class="{ 'skill-row--orphan': isOrphan(s) }"
      >
        <span class="skill-row__icon">⚡</span>

        <div class="skill-row__body">
          <div class="skill-row__title">
            <code class="skill-row__name">{{ s.name }}</code>
            <span v-if="s.is_builtin" class="skill-row__pill skill-row__pill--muted">🔒 BUILTIN</span>
            <span v-else class="skill-row__pill skill-row__pill--primary">USER</span>
            <span v-if="isOrphan(s)" class="skill-row__pill skill-row__pill--warn">○ ORPHAN</span>
            <span v-if="s.parse_error" class="skill-row__pill skill-row__pill--danger" :title="s.parse_error">
              PARSE ERROR
            </span>
          </div>

          <div class="skill-row__desc">
            {{ s.description || (s.parse_error ? '(unparseable frontmatter)' : '—') }}
          </div>

          <div class="skill-row__meta">
            <span class="skill-row__cat">{{ categoryFor(s) }}</span>
            <template v-if="(s.used_by || []).length">
              <span class="skill-row__sep">·</span>
              <span class="skill-row__used">
                ● {{ (s.used_by || []).length }} agent{{ (s.used_by || []).length > 1 ? 's' : '' }}
              </span>
              <template v-if="(s.used_by || []).length <= 3 && s.used_by[0] !== '*'">
                <span class="skill-row__sep">·</span>
                <span class="skill-row__used-list">{{ s.used_by.join(', ') }}</span>
              </template>
              <button
                v-for="a in s.used_by || []"
                :key="a"
                type="button"
                class="skill-row__detach"
                :title="`Detach from ${a}`"
                @click="detachFromAgent(s, a)"
              >× {{ a }}</button>
            </template>
            <template v-else>
              <span class="skill-row__sep">·</span>
              <span class="skill-row__used-warn">○ no agents · safe to delete</span>
            </template>
          </div>
        </div>

        <div class="skill-row__actions">
          <div class="skill-row__attach">
            <button class="btn btn-ghost skill-row__attach-trigger" @click="toggleAttachMenu(s.name)">
              + Attach…
            </button>
            <div v-if="attachOpenFor === s.name" class="skill-row__attach-menu" @click.stop>
              <div v-if="!unattachedAgents(s).length" class="skill-row__attach-empty">
                Already attached to all agents.
              </div>
              <button
                v-for="a in unattachedAgents(s)"
                :key="a.name"
                class="skill-row__attach-item"
                :disabled="attachBusy"
                @click="attachToAgent(s, a)"
              >
                <span>{{ a.name }}</span>
                <span v-if="!a.is_card_based" class="skill-row__attach-warn"
                  title="Code-based agent — change reverts on restart">
                  runtime
                </span>
              </button>
            </div>
          </div>
          <button class="btn btn-icon btn-ghost" :title="`Edit ${s.name}`" @click="openEdit(s)">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
            </svg>
          </button>
          <button
            class="btn btn-icon btn-ghost skill-row__delete"
            :title="s.is_builtin ? 'Built-in skills cannot be deleted' : `Delete ${s.name}`"
            :disabled="s.is_builtin"
            @click="openDelete(s)"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
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
.skills {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
  color: var(--text);
}

/* Header */
.skills__header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 16px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border);
}
.skills__heading { display: flex; flex-direction: column; gap: 4px; }
.skills__title {
  font-family: var(--font-display);
  font-size: 22px;
  letter-spacing: -0.02em;
  margin: 4px 0 0;
}
.skills__title-sub {
  color: var(--text-muted);
  font-size: 14px;
  font-weight: 400;
  margin-left: 6px;
}
.skills__desc {
  font-size: 12.5px;
  color: var(--text-dim);
  max-width: 720px;
}
.skills__inline-code {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--accent);
}
.skills__header-actions { flex-shrink: 0; }

/* Toolbar */
.skills__toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.skills__search {
  flex: 1;
  min-width: 220px;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 34px;
  padding: 0 12px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  color: var(--text-muted);
}
.skills__search:focus-within {
  border-color: var(--primary);
  color: var(--text);
}
.skills__search-input {
  flex: 1;
  background: transparent;
  border: 0;
  outline: 0;
  font-family: var(--font-body);
  font-size: 13px;
  color: var(--text);
}
.skills__search-input::placeholder { color: var(--text-muted); }

/* Category chips */
.skills__categories {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  padding: 4px 0;
}
.skills__chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.04em;
  cursor: pointer;
}
.skills__chip:hover { border-color: var(--border-bright); color: var(--text); }
.skills__chip--active {
  background: var(--primary-bg-strong);
  border-color: var(--primary);
  color: var(--text);
}
.skills__chip-count { color: var(--text-muted); }

/* Status bars */
.skills__toast {
  padding: 8px 12px;
  background: var(--primary-bg);
  border: 1px solid var(--primary-bg-strong);
  border-radius: var(--r-md);
  color: var(--primary-hover);
  font-size: 12.5px;
}
.skills__error {
  padding: 8px 12px;
  background: var(--danger-bg);
  border: 1px solid rgba(239,68,68,0.2);
  border-radius: var(--r-md);
  color: var(--danger);
  font-size: 12.5px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.skills__empty {
  padding: 40px 20px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
  background: var(--bg-2);
  border: 1px dashed var(--border-strong);
  border-radius: var(--r-md);
}
.skills__link {
  background: none;
  border: 0;
  color: var(--primary-hover);
  font-size: 13px;
  cursor: pointer;
  padding: 0;
}

/* List */
.skills__list {
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  overflow: hidden;
}

/* Row */
.skill-row {
  display: grid;
  grid-template-columns: 32px 1fr auto;
  gap: 14px;
  padding: 14px 18px;
  align-items: center;
  border-bottom: 1px solid var(--border);
}
.skill-row:last-child { border-bottom: 0; }
.skill-row--orphan { background: rgba(245, 158, 11, 0.04); }
.skill-row:hover { background: var(--bg-2); }
.skill-row--orphan:hover { background: rgba(245, 158, 11, 0.08); }

.skill-row__icon {
  width: 32px;
  height: 32px;
  border-radius: var(--r-md);
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  color: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
}

.skill-row__body { min-width: 0; }
.skill-row__title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}
.skill-row__name {
  font-family: var(--font-mono);
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
}
.skill-row__pill {
  padding: 1px 6px;
  border-radius: 3px;
  font-family: var(--font-mono);
  font-size: 9px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  border: 1px solid var(--border-strong);
  background: var(--bg-3);
}
.skill-row__pill--muted { color: var(--text-muted); }
.skill-row__pill--primary {
  color: var(--primary-hover);
  background: var(--primary-bg);
  border-color: var(--primary-bg-strong);
}
.skill-row__pill--warn {
  color: var(--warning);
  background: rgba(245,158,11,0.10);
  border-color: rgba(245,158,11,0.30);
}
.skill-row__pill--danger {
  color: var(--danger);
  background: var(--danger-bg);
  border-color: rgba(239,68,68,0.30);
}

.skill-row__desc {
  font-size: 12px;
  color: var(--text-dim);
  margin-bottom: 5px;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.skill-row__meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-muted);
  flex-wrap: wrap;
}
.skill-row__cat {
  padding: 1px 5px;
  border-radius: 3px;
  background: var(--bg-3);
  color: var(--text-dim);
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.skill-row__used { color: var(--success); }
.skill-row__used-warn { color: var(--warning); }
.skill-row__sep { color: var(--text-subtle); }
.skill-row__detach {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--danger-bg);
  color: var(--danger);
  border: 1px solid rgba(239,68,68,0.2);
  font-size: 10px;
  cursor: pointer;
}
.skill-row__detach:hover { background: rgba(239, 68, 68, 0.18); }

/* Actions */
.skill-row__actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}
.skill-row__attach { position: relative; }
.skill-row__attach-trigger {
  height: 26px;
  padding: 0 10px;
  font-size: 11.5px;
  color: var(--primary-hover);
}
.skill-row__attach-menu {
  position: absolute;
  right: 0;
  top: calc(100% + 4px);
  min-width: 220px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-md);
  z-index: 50;
  max-height: 280px;
  overflow-y: auto;
}
.skill-row__attach-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 8px 12px;
  background: transparent;
  border: 0;
  color: var(--text-dim);
  font-size: 13px;
  cursor: pointer;
  text-align: left;
  gap: 10px;
}
.skill-row__attach-item:hover:not(:disabled) {
  background: var(--bg-3);
  color: var(--text);
}
.skill-row__attach-item:disabled { opacity: 0.5; cursor: not-allowed; }
.skill-row__attach-empty {
  padding: 12px;
  color: var(--text-subtle);
  font-size: 12px;
  font-style: italic;
}
.skill-row__attach-warn {
  font-family: var(--font-mono);
  font-size: 9.5px;
  padding: 1px 5px;
  background: rgba(245, 158, 11, 0.10);
  color: var(--warning);
  border-radius: 3px;
  border: 1px solid rgba(245, 158, 11, 0.25);
}

.skill-row__delete:hover:not(:disabled) {
  background: var(--danger-bg);
  color: var(--danger);
}

@media (max-width: 640px) {
  .skill-row { grid-template-columns: 32px 1fr; }
  .skill-row__actions {
    grid-column: 1 / -1;
    justify-content: flex-end;
  }
}
</style>
