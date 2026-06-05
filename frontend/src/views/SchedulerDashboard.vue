<script setup>
/**
 * Scheduler Dashboard — cron jobs + recent runs + attention queue.
 *
 * Logic preserved verbatim from the previous SchedulerDashboard
 * (REST fetch, SSE stream, create/edit/delete/pause/resume/retry).
 * Only the visual shell and tokens were rewritten.
 */
import { ref, onMounted, computed } from 'vue'
import { apiFetch, buildSSEUrl } from '../api.js'
import ConfirmModal from '../components/ConfirmModal.vue'
import { useToast } from '../composables/useToast.js'
import { useSSEConnection } from '../composables/useSSEConnection.js'

const toast = useToast()

// ─── State ───
const stats = ref({
  active_jobs: 0,
  needs_attention: 0,
  success_rate: 1.0,
  next_24h: 0,
  runs_today: 0,
  failed_today: 0,
})
const jobs = ref([])
const runs = ref([])
const loading = ref(true)
const showCreateModal = ref(false)
const timeFilter = ref('today')

const activeJobs = computed(() => jobs.value.filter(j => j.status === 'active'))
const needsAttentionJobs = computed(() =>
  jobs.value.filter(j => j.status === 'disabled' || j.fail_count >= 3 || j.last_result === 'failed')
)

const recentRuns = computed(() => {
  const now = Date.now() / 1000
  let cutoff = now - 86400
  if (timeFilter.value === 'week') cutoff = now - 7 * 86400
  else if (timeFilter.value === 'all') cutoff = 0
  return runs.value.filter(r => r.started_at >= cutoff)
})

async function fetchAll() {
  loading.value = true
  try {
    const [statsRes, jobsRes, runsRes] = await Promise.all([
      apiFetch('/api/scheduler/stats'),
      apiFetch('/api/scheduler/jobs?status=all'),
      apiFetch('/api/scheduler/runs?limit=50'),
    ])
    stats.value = statsRes
    jobs.value = jobsRes.jobs || []
    runs.value = runsRes.runs || []
  } catch (e) {
    console.error('Fetch scheduler data failed:', e)
  } finally {
    loading.value = false
  }
}

useSSEConnection(buildSSEUrl('/api/scheduler/stream'), {
  onMessage(event) {
    try {
      handleSSEEvent(JSON.parse(event.data))
    } catch (e) {
      console.warn('SSE parse error:', e)
    }
  },
})

function handleSSEEvent(event) {
  if (event.type === 'init') {
    stats.value = event.stats
    return
  }
  if (event.type === 'job_approval_changed') {
    // Approval was resolved on the Approvals page — refresh so the pending
    // badge flips and next-run reflects the now-runnable (or rejected) job.
    const name = event.job_name || 'Job'
    if (event.approval_status === 'approved') toast.success(`✅ Approved: ${name} can now run`)
    else if (event.approval_status === 'rejected') toast.info(`🚫 Rejected: ${name} will not run`)
    fetchAll()
    return
  }
  if (['job_started', 'job_completed', 'job_failed', 'reminder'].includes(event.type)) {
    const name = event.job_name || 'Job'
    if (event.type === 'reminder') {
      toast.success(`🔔 ${name}: ${event.message || ''}`)
    } else if (event.type === 'job_completed') {
      toast.success(`✅ ${name} completed (${event.duration_ms || 0}ms)`)
    } else if (event.type === 'job_failed') {
      toast.error(`❌ ${name} failed: ${(event.error || '').slice(0, 80)}`)
    } else if (event.type === 'job_started') {
      toast.info(`⏳ Running: ${name}`)
      const j = jobs.value.find(j => j.id === event.job_id)
      if (j) j.status = 'running'
    }
    fetchAll()
  }
}

async function pauseJob(jobId) {
  await apiFetch(`/api/scheduler/jobs/${jobId}/pause`, { method: 'POST' })
  await fetchAll()
}
async function resumeJob(jobId) {
  await apiFetch(`/api/scheduler/jobs/${jobId}/resume`, { method: 'POST' })
  await fetchAll()
}
async function retryJob(jobId) {
  await apiFetch(`/api/scheduler/jobs/${jobId}/retry`, { method: 'POST' })
  await fetchAll()
}

const showDeleteConfirm = ref(false)
const deleteTargetId = ref(null)
const deleteTargetName = ref('')
const isDeleting = ref(false)

function confirmDeleteJob(job) {
  deleteTargetId.value = job.id
  deleteTargetName.value = job.name
  showDeleteConfirm.value = true
}

async function doDeleteJob() {
  if (!deleteTargetId.value) return
  isDeleting.value = true
  try {
    const name = deleteTargetName.value
    await apiFetch(`/api/scheduler/jobs/${deleteTargetId.value}`, { method: 'DELETE' })
    showDeleteConfirm.value = false
    deleteTargetId.value = null
    toast.success(`Deleted job '${name}'`)
    await fetchAll()
  } finally {
    isDeleting.value = false
  }
}

// Create / edit
const editingJob = ref(null)
const isEditing = computed(() => editingJob.value !== null)
const modalTitle = computed(() => isEditing.value ? 'Edit job' : 'Create scheduled job')
const modalAction = computed(() => isEditing.value ? 'Save changes' : 'Create job')

const newJob = ref({
  name: '',
  cron_expr: '',
  exec_mode: 'reminder',
  exec_payload: '',
  calendar_type: 'solar',
  one_shot: false,
  exec_agent: '',
})

function resetJobForm() {
  newJob.value = { name: '', cron_expr: '', exec_mode: 'reminder', exec_payload: '', calendar_type: 'solar', one_shot: false, exec_agent: '' }
  editingJob.value = null
}

function openJobDetail(job) {
  editingJob.value = job
  newJob.value = {
    name: job.name,
    cron_expr: job.schedule_cron,
    exec_mode: job.exec_mode,
    exec_payload: job.exec_payload || '',
    calendar_type: job.calendar_type || 'solar',
    one_shot: !!job.one_shot,
    exec_agent: job.exec_agent || '',
  }
  showCreateModal.value = true
}

function openCreateModal() {
  resetJobForm()
  showCreateModal.value = true
}

function closeModal() {
  showCreateModal.value = false
  resetJobForm()
}

async function saveJob() {
  if (isEditing.value) await updateJob()
  else await createJob()
}

async function createJob() {
  try {
    const body = { ...newJob.value }
    if (body.exec_mode === 'reminder') delete body.exec_agent
    await apiFetch('/api/scheduler/jobs', {
      method: 'POST',
      body: JSON.stringify(body),
    })
    closeModal()
    toast.success('Job created successfully')
    await fetchAll()
  } catch (e) {
    toast.error('Failed to create job: ' + e.message)
  }
}

async function updateJob() {
  try {
    const body = { ...newJob.value }
    if (body.exec_mode === 'reminder') delete body.exec_agent
    await apiFetch(`/api/scheduler/jobs/${editingJob.value.id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    })
    closeModal()
    toast.success('Job updated successfully')
    await fetchAll()
  } catch (e) {
    toast.error('Failed to update job: ' + e.message)
  }
}

// Formatters
function formatTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}
function formatDateTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleString([], { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function statusColor(status) {
  const map = {
    active: 'var(--success)',
    running: 'var(--primary)',
    success: 'var(--success)',
    paused: 'var(--warning)',
    failed: 'var(--danger)',
    error: 'var(--danger)',
    disabled: 'var(--danger)',
    completed: 'var(--text-muted)',
    queued: 'var(--text-muted)',
    skipped: 'var(--text-subtle)',
  }
  return map[status] || 'var(--text-muted)'
}

function modeLabel(mode) {
  return mode === 'agent_turn' ? 'Agent' : 'Reminder'
}
function modeColor(mode) {
  return mode === 'agent_turn' ? 'var(--primary-hover)' : 'var(--accent)'
}

onMounted(() => {
  fetchAll()
})
</script>

<template>
  <div class="scheduler jv">
    <!-- ─── Header ─── -->
    <div class="scheduler__header">
      <div class="scheduler__heading">
        <div class="eyebrow">OPERATIONS · SCHEDULER</div>
        <h1 class="scheduler__title">
          <span class="grad" style="font-style: italic;">{{ stats.active_jobs || 0 }}</span> active jobs
          <span class="scheduler__title-sub">
            · {{ stats.next_24h || 0 }} next 24h · {{ Math.round((stats.success_rate || 0) * 100) }}% success
          </span>
        </h1>
        <p class="scheduler__desc">
          Cron jobs and reminders. Catch failures early; intervene with quick actions.
        </p>
      </div>
      <div class="scheduler__header-actions">
        <button class="btn btn-secondary" @click="fetchAll">↻ Refresh</button>
        <button class="btn btn-primary" @click="openCreateModal">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
            <line x1="12" y1="5" x2="12" y2="19"/>
            <line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
          New scheduled job
        </button>
      </div>
    </div>

    <!-- ─── KPI cards ─── -->
    <div class="scheduler__kpi">
      <div class="card scheduler__kpi-card">
        <div class="mono-label">ACTIVE</div>
        <div class="scheduler__kpi-value">{{ stats.active_jobs }}</div>
        <div class="scheduler__kpi-sub" style="color: var(--primary-hover);">{{ activeJobs.length }} scheduled</div>
      </div>
      <div class="card scheduler__kpi-card">
        <div class="mono-label">FAILED · TODAY</div>
        <div
          class="scheduler__kpi-value"
          :style="{ color: stats.failed_today > 0 ? 'var(--danger)' : 'var(--text)' }"
        >{{ stats.failed_today }}</div>
        <div
          class="scheduler__kpi-sub"
          :style="{ color: stats.failed_today > 0 ? 'var(--danger)' : 'var(--success)' }"
        >
          {{ stats.failed_today > 0 ? `${stats.failed_today} need retry` : 'all clear' }}
        </div>
      </div>
      <div class="card scheduler__kpi-card">
        <div class="mono-label">NEXT 24H</div>
        <div class="scheduler__kpi-value">{{ stats.next_24h }}</div>
        <div class="scheduler__kpi-sub" style="color: var(--accent);">upcoming runs</div>
      </div>
      <div class="card scheduler__kpi-card">
        <div class="mono-label">SUCCESS RATE</div>
        <div class="scheduler__kpi-value">{{ Math.round(stats.success_rate * 100) }}%</div>
        <div
          class="scheduler__kpi-sub"
          :style="{ color: stats.success_rate >= 0.9 ? 'var(--success)' : 'var(--warning)' }"
        >{{ stats.runs_today }} runs today</div>
      </div>
    </div>

    <!-- ─── Attention banner ─── -->
    <div v-if="needsAttentionJobs.length > 0" class="scheduler__attention">
      <span class="scheduler__attention-icon">⚠</span>
      <span class="scheduler__attention-text">
        {{ needsAttentionJobs.length }} job{{ needsAttentionJobs.length > 1 ? 's' : '' }} need attention
      </span>
    </div>

    <!-- ─── Content grid ─── -->
    <div class="scheduler__grid">
      <!-- Execution overview -->
      <div class="card scheduler__panel">
        <div class="scheduler__panel-head">
          <h2 class="scheduler__panel-title">Execution overview</h2>
          <div class="seg">
            <button :class="{ 'is-active': timeFilter === 'today' }" @click="timeFilter = 'today'">Today</button>
            <button :class="{ 'is-active': timeFilter === 'week' }" @click="timeFilter = 'week'">This week</button>
            <button :class="{ 'is-active': timeFilter === 'all' }" @click="timeFilter = 'all'">All</button>
          </div>
        </div>

        <div v-if="recentRuns.length === 0" class="scheduler__empty">No executions yet</div>

        <div v-else class="scheduler__timeline">
          <div v-for="run in recentRuns" :key="run.id" class="scheduler__timeline-row">
            <span class="scheduler__timeline-time">{{ formatTime(run.started_at) }}</span>
            <span class="scheduler__timeline-dot" :style="{ background: statusColor(run.status) }"></span>
            <span class="scheduler__timeline-name">{{ run.job_name }}</span>
            <div class="scheduler__timeline-badges">
              <span
                class="scheduler__badge"
                :style="{ borderColor: 'color-mix(in srgb, ' + modeColor(run.result_type === 'text' ? 'reminder' : 'agent_turn') + ' 30%, transparent)', color: modeColor(run.result_type === 'text' ? 'reminder' : 'agent_turn') }"
              >
                {{ run.result_type === 'text' ? 'Reminder' : 'Agent' }}
              </span>
              <span
                class="scheduler__badge"
                :style="{ borderColor: 'color-mix(in srgb, ' + statusColor(run.status) + ' 30%, transparent)', color: statusColor(run.status) }"
              >
                {{ run.status === 'success' ? 'Done' : run.status }}
              </span>
            </div>
          </div>
        </div>
      </div>

      <!-- Quick actions -->
      <div class="card scheduler__panel scheduler__panel--narrow">
        <h2 class="scheduler__panel-title">Quick actions</h2>
        <div class="scheduler__quick">
          <button class="scheduler__quick-item" @click="openCreateModal(); newJob.exec_mode = 'reminder'">
            <span>Create reminder</span>
            <span class="scheduler__quick-go">Go →</span>
          </button>
          <button class="scheduler__quick-item" @click="openCreateModal(); newJob.exec_mode = 'agent_turn'">
            <span>Schedule agent task</span>
            <span class="scheduler__quick-go">Go →</span>
          </button>
          <button class="scheduler__quick-item" @click="fetchAll">
            <span>Sync now</span>
            <span class="scheduler__quick-go">Go →</span>
          </button>
        </div>
      </div>
    </div>

    <!-- ─── Jobs needing attention ─── -->
    <div v-if="needsAttentionJobs.length > 0" class="scheduler__attention-section">
      <h2 class="scheduler__section-title">Jobs needing attention</h2>
      <div class="scheduler__attention-grid">
        <div v-for="job in needsAttentionJobs" :key="job.id" class="card scheduler__attention-card">
          <div class="scheduler__attention-head">
            <span class="scheduler__attention-name">{{ job.name }}</span>
            <span
              class="scheduler__badge"
              :style="{ borderColor: 'color-mix(in srgb, ' + statusColor(job.last_result || job.status) + ' 30%, transparent)', color: statusColor(job.last_result || job.status) }"
            >
              {{ job.last_result === 'failed' ? 'Failed' : job.status === 'disabled' ? 'Disabled' : 'Attention' }}
            </span>
          </div>
          <div class="scheduler__attention-meta">
            {{ modeLabel(job.exec_mode) }} · {{ job.schedule_cron }}
          </div>
          <div v-if="job.last_error" class="scheduler__attention-err">{{ job.last_error.slice(0, 120) }}</div>
          <div class="scheduler__attention-next">Next: {{ job.next_run_display || 'N/A' }}</div>
          <div class="scheduler__attention-actions">
            <button class="btn btn-primary" @click="retryJob(job.id)">Retry</button>
            <button v-if="job.status !== 'active'" class="btn btn-secondary" @click="resumeJob(job.id)">Resume</button>
            <button class="btn btn-secondary scheduler__btn-danger" @click="confirmDeleteJob(job)">Delete</button>
          </div>
        </div>
      </div>
    </div>

    <!-- ─── All jobs table ─── -->
    <div class="card scheduler__jobs">
      <div class="scheduler__panel-head">
        <h2 class="scheduler__panel-title">All jobs</h2>
        <span class="scheduler__panel-count">{{ jobs.length }} total</span>
      </div>

      <div v-if="jobs.length === 0 && !loading" class="scheduler__empty">
        No cron jobs yet. Create one to get started.
      </div>

      <div v-if="jobs.length > 0" class="scheduler__jobs-body">
        <!-- Desktop table -->
        <div class="scheduler__table-head">
          <span>Status</span>
          <span>Name</span>
          <span>Schedule</span>
          <span>Mode</span>
          <span>Next run</span>
          <span>Last run</span>
          <span>Runs</span>
          <span>Actions</span>
        </div>
        <div v-for="job in jobs" :key="job.id" class="scheduler__table-row" @click="openJobDetail(job)">
          <span class="scheduler__col-status">
            <span class="scheduler__status-dot" :style="{ background: statusColor(job.status) }"></span>
          </span>
          <span class="scheduler__col-name">
            <span class="scheduler__job-name">{{ job.name }}</span>
            <span v-if="job.calendar_type === 'lunar'" class="scheduler__job-tag">🌙</span>
            <span v-if="job.approval_status === 'pending'" class="scheduler__approval-badge" title="Created by an agent — awaiting your approval before it can run">⏳ Awaiting approval</span>
            <span v-else-if="job.approval_status === 'rejected'" class="scheduler__approval-badge scheduler__approval-badge--rejected" title="Approval rejected — this job will not run">🚫 Rejected</span>
          </span>
          <span class="scheduler__col-cron">
            <code class="scheduler__cron">{{ job.schedule_cron }}</code>
            <span v-if="job.one_shot" class="scheduler__one-shot">1×</span>
          </span>
          <span class="scheduler__col-mode">
            <span
              class="scheduler__badge"
              :style="{ borderColor: 'color-mix(in srgb, ' + modeColor(job.exec_mode) + ' 30%, transparent)', color: modeColor(job.exec_mode) }"
            >{{ modeLabel(job.exec_mode) }}</span>
          </span>
          <span class="scheduler__col-next">{{ job.next_run_display || '—' }}</span>
          <span class="scheduler__col-last">{{ formatDateTime(job.last_run_at) }}</span>
          <span class="scheduler__col-runs">
            {{ job.run_count }}
            <span v-if="job.fail_count > 0" class="scheduler__fail-indicator">⚠{{ job.fail_count }}</span>
          </span>
          <span class="scheduler__col-actions">
            <button v-if="job.status === 'active'" class="btn btn-icon btn-ghost" title="Pause" @click.stop="pauseJob(job.id)">⏸</button>
            <button v-else class="btn btn-icon btn-ghost" title="Resume" @click.stop="resumeJob(job.id)">▶</button>
            <button class="btn btn-icon btn-ghost scheduler__btn-delete" title="Delete" @click.stop="confirmDeleteJob(job)">×</button>
          </span>
        </div>

        <!-- Mobile cards -->
        <div v-for="job in jobs" :key="'card-' + job.id" class="scheduler__job-card" @click="openJobDetail(job)">
          <div class="scheduler__job-card-top">
            <span class="scheduler__status-dot" :style="{ background: statusColor(job.status) }"></span>
            <span class="scheduler__job-card-name">
              {{ job.name }}
              <span v-if="job.calendar_type === 'lunar'" style="margin-left: 4px;">🌙</span>
              <span v-if="job.approval_status === 'pending'" class="scheduler__approval-badge">⏳ Awaiting approval</span>
              <span v-else-if="job.approval_status === 'rejected'" class="scheduler__approval-badge scheduler__approval-badge--rejected">🚫 Rejected</span>
            </span>
            <span
              class="scheduler__badge"
              :style="{ borderColor: 'color-mix(in srgb, ' + modeColor(job.exec_mode) + ' 30%, transparent)', color: modeColor(job.exec_mode) }"
            >{{ modeLabel(job.exec_mode) }}</span>
          </div>
          <div class="scheduler__job-card-meta">
            <code class="scheduler__cron">{{ job.schedule_cron }}</code>
            <span v-if="job.one_shot" class="scheduler__one-shot">1×</span>
            <span class="scheduler__job-card-next">Next: {{ job.next_run_display || '—' }}</span>
            <span v-if="job.fail_count > 0" class="scheduler__fail-indicator">⚠ {{ job.fail_count }} fails</span>
          </div>
          <div class="scheduler__job-card-actions">
            <button v-if="job.status === 'active'" class="btn btn-icon btn-ghost" title="Pause" @click.stop="pauseJob(job.id)">⏸</button>
            <button v-else class="btn btn-icon btn-ghost" title="Resume" @click.stop="resumeJob(job.id)">▶</button>
            <button class="btn btn-icon btn-ghost scheduler__btn-delete" title="Delete" @click.stop="confirmDeleteJob(job)">×</button>
          </div>
        </div>
      </div>
    </div>

    <!-- ─── Create / Edit modal ─── -->
    <Teleport to="body">
      <!-- The ``jv`` class is REQUIRED on the teleported root: tokens.css
           scopes most global styling (`.jv .btn`, `.jv h2`, base color /
           bg) under `.jv`. Without it, teleporting to body strips the
           modal of the page-level wrapper class and buttons + heading
           fall back to browser defaults — invisible "Cancel / Create
           job" + faded "Create scheduled job" title in light theme
           (2026-05-27 contrast bug). -->
      <div v-if="showCreateModal" class="scheduler-modal-overlay jv" @click.self="closeModal">
        <div class="scheduler-modal">
          <h2 class="scheduler-modal__title">{{ modalTitle }}</h2>

          <div v-if="isEditing" class="scheduler-modal__edit-info">
            <span class="scheduler-modal__edit-id">ID: {{ editingJob.id }}</span>
            <span
              class="scheduler__badge"
              :style="{ borderColor: 'color-mix(in srgb, ' + statusColor(editingJob.status) + ' 30%, transparent)', color: statusColor(editingJob.status) }"
            >{{ editingJob.status }}</span>
            <span v-if="editingJob.run_count > 0" class="scheduler-modal__edit-runs">
              {{ editingJob.run_count }} runs · Last: {{ formatDateTime(editingJob.last_run_at) }}
            </span>
          </div>

          <div class="scheduler-modal__form">
            <label class="scheduler-modal__field">
              <span>Name</span>
              <input v-model="newJob.name" placeholder="e.g. Take medicine reminder" class="scheduler-modal__input" />
            </label>

            <label class="scheduler-modal__field">
              <span>Cron expression</span>
              <input v-model="newJob.cron_expr" placeholder="0 8 * * *   (8am daily)" class="scheduler-modal__input scheduler-modal__input--mono" />
              <div class="scheduler-modal__hint">
                minute hour day month weekday — e.g. <code>0 9 * * 1-5</code> = 9am Mon–Fri
              </div>
            </label>

            <div class="scheduler-modal__row">
              <label class="scheduler-modal__field">
                <span>Calendar</span>
                <select v-model="newJob.calendar_type" class="scheduler-modal__input">
                  <option value="solar">☀️ Solar</option>
                  <option value="lunar">🌙 Lunar</option>
                </select>
              </label>
              <label class="scheduler-modal__field">
                <span>Mode</span>
                <select v-model="newJob.exec_mode" class="scheduler-modal__input">
                  <option value="reminder">Reminder</option>
                  <option value="agent_turn">Agent turn</option>
                </select>
              </label>
            </div>

            <label v-if="newJob.exec_mode === 'agent_turn'" class="scheduler-modal__field">
              <span>Agent name</span>
              <input v-model="newJob.exec_agent" placeholder="e.g. Jarvis" class="scheduler-modal__input" />
            </label>

            <label class="scheduler-modal__field">
              <span>{{ newJob.exec_mode === 'reminder' ? 'Reminder message' : 'Agent command' }}</span>
              <textarea
                v-model="newJob.exec_payload"
                rows="3"
                :placeholder="newJob.exec_mode === 'reminder' ? 'Take your medicine!' : 'Summarize today\'s news'"
                class="scheduler-modal__input scheduler-modal__textarea"
              ></textarea>
            </label>

            <label class="scheduler-modal__checkbox">
              <input type="checkbox" v-model="newJob.one_shot" />
              Run only once (one-shot)
            </label>
          </div>

          <div class="scheduler-modal__actions">
            <button class="btn btn-secondary" @click="closeModal">Cancel</button>
            <button
              class="btn btn-primary"
              @click="saveJob"
              :disabled="!newJob.name || !newJob.cron_expr || !newJob.exec_payload"
            >{{ modalAction }}</button>
          </div>
        </div>
      </div>
    </Teleport>

    <ConfirmModal
      :visible="showDeleteConfirm"
      title="Delete scheduled job"
      :message="'Are you sure you want to delete job \'' + deleteTargetName + '\'? This cannot be undone.'"
      confirm-text="Delete"
      variant="danger"
      :loading="isDeleting"
      @confirm="doDeleteJob"
      @cancel="showDeleteConfirm = false"
    />
  </div>
</template>

<style scoped>
.scheduler {
  max-width: 1200px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  color: var(--text);
  /* min-width:0 stops a long mono cron expression or job-name from
     forcing the flex column wider than its parent, which would push
     the whole page into horizontal overflow on narrow viewports. */
  min-width: 0;
  /* Belt-and-suspenders: if any descendant escapes containment (a
     fixed-px badge row, an un-wrappable mono string, a residual
     desktop-table column track), clip rather than scrolling the whole
     page. `clip` over `hidden` because `clip` doesn't create a new
     scrolling box (sticky descendants keep working). The scheduler
     surface has no legit horizontal-scroll children on mobile — the
     desktop table is `display:none` here — so this clip is safe. */
  overflow-x: clip;
}

/* Header */
.scheduler__header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 16px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border);
}
.scheduler__heading { display: flex; flex-direction: column; gap: 4px; }
.scheduler__title {
  font-family: var(--font-display);
  font-size: 22px;
  letter-spacing: -0.02em;
  margin: 4px 0 0;
}
.scheduler__title-sub {
  color: var(--text-muted);
  font-size: 14px;
  font-weight: 400;
  margin-left: 6px;
}
.scheduler__desc { font-size: 12.5px; color: var(--text-dim); }
.scheduler__header-actions { display: flex; gap: 8px; flex-shrink: 0; }

/* KPI */
.scheduler__kpi {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}
.scheduler__kpi-card {
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.scheduler__kpi-value {
  font-family: var(--font-mono);
  font-size: 24px;
  color: var(--text);
  line-height: 1.1;
}
.scheduler__kpi-sub {
  font-family: var(--font-mono);
  font-size: 10.5px;
  letter-spacing: 0.06em;
}

/* Attention banner */
.scheduler__attention {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  background: var(--danger-bg);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: var(--r-md);
  color: var(--danger);
  font-size: 13px;
  font-weight: 500;
}

/* Grid */
.scheduler__grid {
  display: grid;
  grid-template-columns: 1fr 300px;
  gap: 14px;
}
.scheduler__panel {
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.scheduler__panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.scheduler__panel-title {
  font-size: 14px;
  font-weight: 600;
  margin: 0;
}
.scheduler__panel-count {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
}

/* Timeline */
.scheduler__timeline {
  display: flex;
  flex-direction: column;
  max-height: 320px;
  overflow-y: auto;
}
.scheduler__timeline-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
}
.scheduler__timeline-row:last-child { border-bottom: 0; }
.scheduler__timeline-time {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  min-width: 44px;
}
.scheduler__timeline-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.scheduler__timeline-name {
  flex: 1;
  font-size: 13px;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.scheduler__timeline-badges {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

/* Badge */
.scheduler__badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 3px;
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  border: 1px solid var(--border-strong);
  background: var(--bg-3);
}

/* Quick actions */
.scheduler__panel--narrow .scheduler__panel-title { margin-bottom: 4px; }
.scheduler__quick {
  display: flex;
  flex-direction: column;
}
.scheduler__quick-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 0;
  background: transparent;
  border: 0;
  border-bottom: 1px solid var(--border);
  color: var(--text-dim);
  cursor: pointer;
  font-size: 13px;
  text-align: left;
}
.scheduler__quick-item:last-child { border-bottom: 0; }
.scheduler__quick-item:hover { color: var(--text); }
.scheduler__quick-go {
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 600;
  color: var(--success);
  background: var(--success-bg);
  padding: 2px 8px;
  border-radius: 3px;
}

/* Empty */
.scheduler__empty {
  padding: 30px 16px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
}

/* Attention section */
.scheduler__attention-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.scheduler__section-title {
  font-size: 14px;
  font-weight: 600;
  margin: 0;
}
.scheduler__attention-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}
.scheduler__attention-card {
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.scheduler__attention-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.scheduler__attention-name { font-size: 13px; font-weight: 600; color: var(--text); }
.scheduler__attention-meta { font-family: var(--font-mono); font-size: 11px; color: var(--text-muted); }
.scheduler__attention-err { font-size: 11px; color: var(--danger); line-height: 1.4; }
.scheduler__attention-next { font-family: var(--font-mono); font-size: 11px; color: var(--text-muted); }
.scheduler__attention-actions { display: flex; gap: 6px; margin-top: 4px; flex-wrap: wrap; }
.scheduler__btn-danger { color: var(--danger); border-color: rgba(239,68,68,0.30); }
.scheduler__btn-danger:hover { background: var(--danger-bg); }

/* Jobs table */
.scheduler__jobs { padding: 18px; }
.scheduler__jobs-body {
  /* Desktop: horizontal-scroll if the 8-col grid (770px min) exceeds
     panel width. Mobile: table is display:none, only the card stack
     renders — and the cards must NOT trigger a horizontal scrollbox
     because their inner `flex-wrap: wrap` already handles overflow. */
  overflow-x: auto;
  margin-top: 12px;
  min-width: 0;
}
@media (max-width: 767px) {
  .scheduler__jobs-body { overflow-x: visible; }
}

.scheduler__table-head,
.scheduler__table-row {
  display: grid;
  grid-template-columns: 40px minmax(160px, 1.5fr) minmax(120px, 1fr) 80px minmax(110px, 1fr) minmax(110px, 1fr) 70px 80px;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
}
.scheduler__table-head {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  text-transform: uppercase;
  border-bottom: 1px solid var(--border);
}
.scheduler__table-row {
  font-size: 12.5px;
  color: var(--text-dim);
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  transition: background 0.12s var(--ease-out);
}
.scheduler__table-row:hover { background: var(--bg-2); }
.scheduler__table-row:last-child { border-bottom: 0; }

.scheduler__status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.scheduler__job-name { color: var(--text); font-weight: 500; }
.scheduler__job-tag { margin-left: 4px; font-size: 10px; color: var(--text-muted); }
.scheduler__approval-badge {
  margin-left: 6px;
  font-size: 10px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 10px;
  white-space: nowrap;
  background: var(--warning-bg);
  color: var(--warning);
  border: 1px solid rgba(245, 158, 11, 0.3);
}
.scheduler__approval-badge--rejected {
  background: var(--danger-bg);
  color: var(--danger);
  border-color: rgba(239, 68, 68, 0.3);
}
.scheduler__cron {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-dim);
  background: var(--bg-2);
  padding: 2px 6px;
  border-radius: 3px;
}
.scheduler__one-shot {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--warning);
  margin-left: 4px;
  font-weight: 700;
}
.scheduler__fail-indicator {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--danger);
  margin-left: 4px;
}
.scheduler__col-actions {
  display: flex;
  gap: 2px;
  justify-content: flex-end;
}
.scheduler__btn-delete:hover { color: var(--danger); background: var(--danger-bg); }

/* Mobile card */
.scheduler__job-card { display: none; }

/* Modal */
.scheduler-modal-overlay {
  position: fixed;
  inset: 0;
  background: var(--bg-overlay);
  backdrop-filter: blur(8px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.scheduler-modal {
  background: var(--bg-1);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  padding: 22px;
  width: min(540px, 92vw);
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: var(--shadow-lg);
}
.scheduler-modal__title {
  font-size: 18px;
  font-weight: 600;
  margin: 0 0 16px;
}
.scheduler-modal__edit-info {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  margin-bottom: 14px;
  font-size: 12px;
}
.scheduler-modal__edit-id {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
}
.scheduler-modal__edit-runs {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  margin-left: auto;
}

.scheduler-modal__form { display: flex; flex-direction: column; gap: 12px; }
.scheduler-modal__row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.scheduler-modal__field { display: flex; flex-direction: column; gap: 6px; }
.scheduler-modal__field > span {
  font-family: var(--font-mono);
  font-size: 10.5px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-muted);
}
.scheduler-modal__input {
  padding: 9px 12px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
  outline: none;
}
.scheduler-modal__input:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-bg);
}
.scheduler-modal__input--mono { font-family: var(--font-mono); font-size: 12px; }
.scheduler-modal__textarea { min-height: 60px; resize: vertical; }
.scheduler-modal__hint {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-subtle);
}
.scheduler-modal__hint code {
  background: var(--bg-3);
  padding: 1px 4px;
  border-radius: 3px;
}
.scheduler-modal__checkbox {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-size: 13px;
  color: var(--text-dim);
}
.scheduler-modal__checkbox input { accent-color: var(--primary); width: 16px; height: 16px; }
.scheduler-modal__actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
}

select.scheduler-modal__input {
  appearance: none;
  background-image: url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3e%3cpath fill='none' stroke='%237B8094' stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='m2 5 6 6 6-6'/%3e%3c/svg%3e");
  background-repeat: no-repeat;
  background-position: right 12px center;
  background-size: 12px;
  padding-right: 36px;
}

@media (max-width: 900px) {
  .scheduler__kpi { grid-template-columns: repeat(2, 1fr); }
  .scheduler__grid { grid-template-columns: 1fr; }
}

/* Very narrow: KEEP the 2-col layout (4-col attempted earlier
   overflowed horizontally because each card's "SUCCESS RATE" min-
   content + padding totaled ~100px, and 4 of those exceed a 339px
   content area). Trim padding + font sizes vertically so the 2-col
   grid eats less vertical space without breaking width. */
@media (max-width: 480px) {
  .scheduler__kpi-card {
    padding: 10px 12px;
    gap: 2px;
    /* min-width:0 lets `1fr` columns shrink below their intrinsic
       min-content width — without it, long labels like "SUCCESS RATE"
       silently expand the grid past viewport. */
    min-width: 0;
  }
  .scheduler__kpi-value { font-size: 18px; }
  .scheduler__kpi-sub { font-size: 10px; }
}

@media (max-width: 768px) {
  .scheduler__header { flex-direction: column; align-items: flex-start; }
  /* Action row was the dominant horizontal-overflow source on phone:
     [↻ Refresh] + [+ New scheduled job] each have white-space:nowrap
     and together exceed a 339px content area. Make the row claim full
     width AND let buttons grow to fill 50/50 so they read as
     parallel CTAs instead of bleeding off-screen. */
  .scheduler__header-actions { width: 100%; flex-wrap: wrap; gap: 8px; }
  .scheduler__header-actions > .btn { flex: 1; min-width: 0; }
  /* Title can get long when stats run high (`1284 active jobs · ...`).
     Allow it to wrap and shrink so it doesn't push the page wider. */
  .scheduler__title { font-size: 18px; }
  .scheduler__title-sub { display: block; margin-left: 0; font-size: 12px; }
  /* Panel head: title + .seg (Today/Week/All) on one row exceeds the
     303px-ish content area (title ~150 + seg ~200 + gap = 362). Force
     them to stack so .seg lands on its own row at full width — the
     existing `flex-wrap: wrap` doesn't break early enough because
     .seg's buttons have white-space:nowrap. */
  .scheduler__panel-head { flex-direction: column; align-items: stretch; gap: 8px; }
  .scheduler__panel-head .seg { align-self: flex-start; }
  /* Timeline rows: keep them as a single compact line (time + dot +
     truncated name + small badges). Wrap-to-multi-row was producing a
     3-line stack with badges indented arbitrarily — looks broken when
     many entries list together. Shrink badge typography instead so
     two badges + a name fit beside the time on one row. */
  .scheduler__timeline-row { gap: 8px; padding: 10px 0; }
  .scheduler__timeline-time { min-width: 52px; font-size: 10.5px; }
  .scheduler__timeline-name { font-size: 12.5px; }
  .scheduler__badge {
    padding: 1px 6px;
    font-size: 9px;
    letter-spacing: 0.04em;
  }
  .scheduler__table-head,
  .scheduler__table-row { display: none; }
  .scheduler__job-card {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    background: var(--bg-2);
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    margin-bottom: 8px;
    cursor: pointer;
  }
  .scheduler__job-card:hover { border-color: var(--border-strong); }
  .scheduler__job-card-top {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .scheduler__job-card-name {
    font-size: 13px;
    font-weight: 600;
    color: var(--text);
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .scheduler__job-card-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    font-family: var(--font-mono);
    font-size: 10.5px;
    color: var(--text-muted);
  }
  .scheduler__job-card-actions { display: flex; gap: 6px; justify-content: flex-end; }
  .scheduler-modal__row { grid-template-columns: 1fr; }
}
</style>
