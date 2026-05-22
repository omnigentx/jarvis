<script setup>
import { ref, onMounted, computed } from 'vue'
import { apiFetch, buildSSEUrl } from '../api.js'
import ConfirmModal from '../components/ConfirmModal.vue'
import { useToast } from '../composables/useToast.js'
import { useSSEConnection } from '../composables/useSSEConnection.js'

const toast = useToast()

// ─── State ───────────────────────────────────────────
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

// SSE

// ─── Computed ────────────────────────────────────────
const activeJobs = computed(() => jobs.value.filter(j => j.status === 'active'))
const pausedJobs = computed(() => jobs.value.filter(j => j.status === 'paused'))
const failedJobs = computed(() => jobs.value.filter(j => j.status === 'disabled' || j.fail_count >= 3))
const needsAttentionJobs = computed(() =>
  jobs.value.filter(j => j.status === 'disabled' || j.fail_count >= 3 || j.last_result === 'failed')
)

const recentRuns = computed(() => {
  const now = Date.now() / 1000
  let cutoff = now - 86400 // default: today
  if (timeFilter.value === 'week') cutoff = now - 7 * 86400
  else if (timeFilter.value === 'all') cutoff = 0
  return runs.value.filter(r => r.started_at >= cutoff)
})

// ─── Fetch ───────────────────────────────────────────
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

// ─── SSE ─────────────────────────────────────────────
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

  // Refresh data + toast on job events
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
      // Optimistic local update — mark job as running instantly
      const j = jobs.value.find(j => j.id === event.job_id)
      if (j) j.status = 'running'
    }
    fetchAll()
  }
}

// ─── Actions ─────────────────────────────────────────
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

// ─── Create / Edit Job ───────────────────────────────
const editingJob = ref(null) // null = create mode, object = edit mode
const isEditing = computed(() => editingJob.value !== null)
const modalTitle = computed(() => isEditing.value ? 'Edit Job' : 'Create Scheduled Job')
const modalAction = computed(() => isEditing.value ? 'Save Changes' : 'Create Job')

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
  if (isEditing.value) {
    await updateJob()
  } else {
    await createJob()
  }
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

// ─── Helpers ─────────────────────────────────────────
function formatTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' })
}

function formatDateTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleString('vi-VN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function formatDuration(ms) {
  if (!ms) return '—'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

function statusColor(status) {
  const map = {
    active: '#22c55e', running: '#3b82f6', success: '#22c55e',
    paused: '#ffb547', failed: '#ef4444', error: '#ef4444',
    disabled: '#ef4444', completed: '#8b8fa3', queued: '#8b8fa3',
    skipped: '#64748b',
  }
  return map[status] || '#8b8fa3'
}

function modeLabel(mode) {
  return mode === 'agent_turn' ? 'Agent' : 'Reminder'
}

function modeColor(mode) {
  return mode === 'agent_turn' ? '#6366f1' : '#3b82f6'
}

// ─── Lifecycle ───────────────────────────────────────
onMounted(() => {
  fetchAll()
})
</script>

<template>
  <div class="scheduler-page">
    <!-- Header -->
    <div class="scheduler-header">
      <div>
        <h1 class="page-title">Scheduler</h1>
        <p class="page-subtitle">Monitor reminders and agent turns, catch failures early, and intervene with quick actions.</p>
      </div>
      <div class="header-actions">
        <button class="btn-secondary" @click="fetchAll">
          ↻ Refresh
        </button>
        <button class="btn-primary" @click="openCreateModal">
          + Create scheduled job
        </button>
      </div>
    </div>

    <!-- Stat Cards -->
    <div class="stat-row">
      <div class="stat-card">
        <div class="stat-label">Active Jobs</div>
        <div class="stat-value">{{ stats.active_jobs }}</div>
        <div class="stat-bar" style="background: #3b82f6"></div>
        <div class="stat-sub" style="color: #3b82f6">{{ activeJobs.length }} scheduled</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Failed Runs</div>
        <div class="stat-value" :style="{ color: stats.failed_today > 0 ? '#ef4444' : '#f3f6fc' }">
          {{ stats.failed_today }}
        </div>
        <div class="stat-bar" :style="{ background: stats.failed_today > 0 ? '#ef4444' : '#22c55e' }"></div>
        <div class="stat-sub" :style="{ color: stats.failed_today > 0 ? '#ef4444' : '#8b8fa3' }">
          {{ stats.failed_today > 0 ? `${stats.failed_today} need retry` : 'All clear' }}
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Next 24h</div>
        <div class="stat-value">{{ stats.next_24h }}</div>
        <div class="stat-bar" style="background: #00c896"></div>
        <div class="stat-sub" style="color: #00c896">upcoming executions</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Success Rate</div>
        <div class="stat-value">{{ Math.round(stats.success_rate * 100) }}%</div>
        <div class="stat-bar" :style="{ background: stats.success_rate >= 0.9 ? '#22c55e' : '#ffb547' }"></div>
        <div class="stat-sub" :style="{ color: stats.success_rate >= 0.9 ? '#22c55e' : '#ffb547' }">
          {{ stats.runs_today }} runs today
        </div>
      </div>
    </div>

    <!-- Mobile Attention Banner (shown only on mobile via CSS) -->
    <div v-if="needsAttentionJobs.length > 0" class="attention-banner">
      <span class="attention-banner-icon">⚠</span>
      <span class="attention-banner-text">{{ needsAttentionJobs.length }} job{{ needsAttentionJobs.length > 1 ? 's' : '' }} need attention</span>
      <span class="attention-banner-arrow">›</span>
    </div>

    <!-- Content Grid -->
    <div class="content-grid">
      <!-- Execution Overview -->
      <div class="panel execution-panel">
        <div class="panel-header">
          <h2 class="panel-title">Execution Overview</h2>
          <div class="time-filters">
            <button :class="['filter-btn', { active: timeFilter === 'today' }]" @click="timeFilter = 'today'">Today</button>
            <button :class="['filter-btn', { active: timeFilter === 'week' }]" @click="timeFilter = 'week'">This week</button>
            <button :class="['filter-btn', { active: timeFilter === 'all' }]" @click="timeFilter = 'all'">All</button>
          </div>
        </div>

        <div v-if="recentRuns.length === 0" class="empty-state">
          No executions yet
        </div>

        <div v-else class="timeline">
          <div v-for="run in recentRuns" :key="run.id" class="timeline-row">
            <span class="timeline-time">{{ formatTime(run.started_at) }}</span>
            <span class="timeline-dot" :style="{ background: statusColor(run.status) }"></span>
            <span class="timeline-name">{{ run.job_name }}</span>
            <div class="timeline-badges">
              <span class="badge" :style="{ background: modeColor(run.result_type === 'text' ? 'reminder' : 'agent_turn') + '22', color: modeColor(run.result_type === 'text' ? 'reminder' : 'agent_turn') }">
                {{ run.result_type === 'text' ? 'Reminder' : 'Agent' }}
              </span>
              <span class="badge" :style="{ background: statusColor(run.status) + '22', color: statusColor(run.status) }">
                {{ run.status === 'success' ? 'Done' : run.status }}
              </span>
            </div>
          </div>
        </div>
      </div>

      <!-- Quick Actions -->
      <div class="panel quick-panel">
        <h2 class="panel-title">Quick Actions</h2>
        <div class="quick-list">
          <div class="quick-item" @click="openCreateModal(); newJob.exec_mode = 'reminder'">
            <span>Create reminder</span>
            <span class="quick-go">Go →</span>
          </div>
          <div class="quick-item" @click="openCreateModal(); newJob.exec_mode = 'agent_turn'">
            <span>Schedule agent task</span>
            <span class="quick-go">Go →</span>
          </div>
          <div class="quick-item" @click="fetchAll">
            <span>Sync now</span>
            <span class="quick-go">Go →</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Jobs Needing Attention -->
    <div class="attention-section" v-if="needsAttentionJobs.length > 0">
      <div class="section-header">
        <h2 class="panel-title">Jobs Needing Attention</h2>
      </div>
      <div class="attention-grid">
        <div v-for="job in needsAttentionJobs" :key="job.id" class="attention-card">
          <div class="attention-header">
            <span class="attention-name">{{ job.name }}</span>
            <span class="badge" :style="{ background: statusColor(job.last_result || job.status) + '22', color: statusColor(job.last_result || job.status) }">
              {{ job.last_result === 'failed' ? 'Failed' : job.status === 'disabled' ? 'Disabled' : 'Attention' }}
            </span>
          </div>
          <div class="attention-meta">
            {{ modeLabel(job.exec_mode) }} · {{ job.schedule_cron }}
          </div>
          <div class="attention-error" v-if="job.last_error">
            {{ job.last_error.slice(0, 120) }}
          </div>
          <div class="attention-next">
            Next: {{ job.next_run_display || 'N/A' }}
          </div>
          <div class="attention-actions">
            <button class="btn-accent" @click="retryJob(job.id)">Retry</button>
            <button class="btn-outline" @click="resumeJob(job.id)" v-if="job.status !== 'active'">Resume</button>
            <button class="btn-outline btn-danger" @click="confirmDeleteJob(job)">Delete</button>
          </div>
        </div>
      </div>
    </div>

    <!-- All Jobs Table -->
    <div class="panel jobs-panel">
      <div class="panel-header">
        <h2 class="panel-title">All Jobs</h2>
        <span class="panel-count">{{ jobs.length }} total</span>
      </div>

      <div v-if="jobs.length === 0 && !loading" class="empty-state">
        No cron jobs yet. Create one to get started!
      </div>

      <div v-if="jobs.length > 0" class="jobs-table">
        <!-- Desktop table -->
        <div class="jobs-header-row">
          <span class="col-status">Status</span>
          <span class="col-name">Name</span>
          <span class="col-cron">Schedule</span>
          <span class="col-mode">Mode</span>
          <span class="col-next">Next Run</span>
          <span class="col-last">Last Run</span>
          <span class="col-runs">Runs</span>
          <span class="col-actions">Actions</span>
        </div>
        <div v-for="job in jobs" :key="job.id" class="job-row" @click="openJobDetail(job)">
          <span class="col-status">
            <span class="status-dot" :style="{ background: statusColor(job.status) }"></span>
          </span>
          <span class="col-name">
            <span class="job-name-text">{{ job.name }}</span>
            <span class="job-id" v-if="job.calendar_type === 'lunar'">🌙</span>
          </span>
          <span class="col-cron">
            <code class="cron-code">{{ job.schedule_cron }}</code>
            <span v-if="job.one_shot" class="one-shot-badge">1×</span>
          </span>
          <span class="col-mode">
            <span class="badge" :style="{ background: modeColor(job.exec_mode) + '22', color: modeColor(job.exec_mode) }">
              {{ modeLabel(job.exec_mode) }}
            </span>
          </span>
          <span class="col-next">{{ job.next_run_display || '—' }}</span>
          <span class="col-last">{{ formatDateTime(job.last_run_at) }}</span>
          <span class="col-runs">
            {{ job.run_count }}
            <span v-if="job.fail_count > 0" class="fail-indicator">⚠{{ job.fail_count }}</span>
          </span>
          <span class="col-actions">
            <button v-if="job.status === 'active'" class="action-btn" title="Pause" @click.stop="pauseJob(job.id)">⏸</button>
            <button v-if="job.status !== 'active'" class="action-btn" title="Resume" @click.stop="resumeJob(job.id)">▶</button>
            <button class="action-btn action-delete" title="Delete" @click.stop="confirmDeleteJob(job)">×</button>
          </span>
        </div>

        <!-- Mobile card list (shown via CSS @media) -->
        <div v-for="job in jobs" :key="'card-' + job.id" class="job-card" @click="openJobDetail(job)">
          <div class="job-card-top">
            <span class="status-dot" :style="{ background: statusColor(job.status) }"></span>
            <span class="job-card-name">
              {{ job.name }}
              <span v-if="job.calendar_type === 'lunar'" style="margin-left: 4px;">🌙</span>
            </span>
            <span class="badge" :style="{ background: modeColor(job.exec_mode) + '22', color: modeColor(job.exec_mode) }">
              {{ modeLabel(job.exec_mode) }}
            </span>
            <span class="badge" :style="{ background: statusColor(job.status) + '22', color: statusColor(job.status) }">
              {{ job.status }}
            </span>
          </div>
          <div class="job-card-meta">
            <code class="job-card-cron">{{ job.schedule_cron }}</code>
            <span v-if="job.one_shot" class="one-shot-badge">1×</span>
            <span class="job-card-next">Next: {{ job.next_run_display || '—' }}</span>
            <span v-if="job.fail_count > 0" class="fail-indicator">⚠ {{ job.fail_count }} fails</span>
          </div>
          <div class="job-card-actions">
            <button v-if="job.status === 'active'" class="action-btn" title="Pause" @click.stop="pauseJob(job.id)">⏸</button>
            <button v-if="job.status !== 'active'" class="action-btn" title="Resume" @click.stop="resumeJob(job.id)">▶</button>
            <button class="action-btn action-delete" title="Delete" @click.stop="confirmDeleteJob(job)">×</button>
          </div>
        </div>
      </div>
    </div>

    <!-- Create Modal -->
    <Teleport to="body">
      <div v-if="showCreateModal" class="modal-overlay" @click.self="closeModal">
        <div class="modal">
          <h2 class="modal-title">{{ modalTitle }}</h2>

          <!-- Job ID + Status (edit mode only) -->
          <div v-if="isEditing" class="edit-info">
            <span class="edit-id">ID: {{ editingJob.id }}</span>
            <span class="badge" :style="{ background: statusColor(editingJob.status) + '22', color: statusColor(editingJob.status) }">
              {{ editingJob.status }}
            </span>
            <span v-if="editingJob.run_count > 0" style="font-size: 11px; color: #8b8fa3; margin-left: auto;">
              {{ editingJob.run_count }} runs · Last: {{ formatDateTime(editingJob.last_run_at) }}
            </span>
          </div>

          <div class="form-group">
            <label>Name</label>
            <input v-model="newJob.name" placeholder="e.g. Take medicine reminder" class="form-input" />
          </div>

          <div class="form-group">
            <label>Cron Expression</label>
            <input v-model="newJob.cron_expr" placeholder="0 8 * * *  (8am daily)" class="form-input" />
            <div class="form-hint">minute hour day month weekday — example: "0 9 * * 1-5" = 9am Mon-Fri</div>
          </div>

          <div class="form-row">
            <div class="form-group">
              <label>Calendar Type</label>
              <select v-model="newJob.calendar_type" class="form-input">
                <option value="solar">☀️ Solar calendar</option>
                <option value="lunar">🌙 Lunar calendar</option>
              </select>
            </div>
            <div class="form-group">
              <label>Mode</label>
              <select v-model="newJob.exec_mode" class="form-input">
                <option value="reminder">Reminder</option>
                <option value="agent_turn">Agent Turn</option>
              </select>
            </div>
          </div>

          <div class="form-group" v-if="newJob.exec_mode === 'agent_turn'">
            <label>Agent Name</label>
            <input v-model="newJob.exec_agent" placeholder="e.g. Jarvis" class="form-input" />
          </div>

          <div class="form-group">
            <label>{{ newJob.exec_mode === 'reminder' ? 'Reminder Message' : 'Agent Command' }}</label>
            <textarea v-model="newJob.exec_payload" rows="3" :placeholder="newJob.exec_mode === 'reminder' ? 'Take your medicine!' : 'Summarize today\'s news'" class="form-input form-textarea"></textarea>
          </div>

          <div class="form-group">
            <label class="checkbox-label">
              <input type="checkbox" v-model="newJob.one_shot" />
              Run only once (one-shot)
            </label>
          </div>

          <div class="modal-actions">
            <button class="btn-secondary" @click="closeModal">Cancel</button>
            <button class="btn-primary" @click="saveJob" :disabled="!newJob.name || !newJob.cron_expr || !newJob.exec_payload">{{ modalAction }}</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Delete Confirm Modal -->
    <ConfirmModal
      :visible="showDeleteConfirm"
      title="Delete scheduled job"
      :message="'Are you sure you want to delete job \'' + deleteTargetName + '\'? This action cannot be undone.'"
      confirm-text="Delete"
      variant="danger"
      :loading="isDeleting"
      @confirm="doDeleteJob"
      @cancel="showDeleteConfirm = false"
    />
  </div>
</template>

<style scoped>
/* ─── Page Layout ─────────────────────────────────── */
.scheduler-page {
  max-width: 1200px;
}

.scheduler-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 24px;
}

.page-title {
  font-size: 24px;
  font-weight: 700;
  color: #f3f6fc;
  margin-bottom: 4px;
}

.page-subtitle {
  font-size: 13px;
  color: #b8c0d4;
}

.header-actions {
  display: flex;
  gap: 10px;
  flex-shrink: 0;
}

/* ─── Buttons ─────────────────────────────────────── */
.btn-primary {
  background: #3b82f6;
  color: #fff;
  border: none;
  padding: 8px 16px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
}
.btn-primary:hover { background: #2563eb; transform: translateY(-1px); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }

.btn-secondary {
  background: #111318;
  color: #b8c0d4;
  border: 1px solid #1e2030;
  padding: 8px 16px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
}
.btn-secondary:hover { background: #1e2233; border-color: #2a3556; }

.btn-accent {
  background: #3b82f6;
  color: #fff;
  border: none;
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}
.btn-accent:hover { background: #2563eb; }

.btn-outline {
  background: transparent;
  color: #b8c0d4;
  border: 1px solid #1e2030;
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
}
.btn-outline:hover { border-color: #3b82f6; color: #f3f6fc; }
.btn-outline.btn-danger:hover { border-color: #ef4444; color: #ef4444; }

/* ─── Stat Cards ──────────────────────────────────── */
.stat-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 24px;
}

.stat-card {
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 12px;
  padding: 16px;
  position: relative;
  overflow: hidden;
  transition: border-color 0.2s;
}
.stat-card:hover { border-color: #2a3556; }

.stat-label {
  font-size: 11px;
  font-weight: 500;
  color: #8b8fa3;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.stat-value {
  font-size: 28px;
  font-weight: 700;
  color: #f3f6fc;
  margin: 6px 0;
  line-height: 1;
}

.stat-bar {
  height: 3px;
  width: 60%;
  border-radius: 2px;
  margin: 8px 0 6px;
}

.stat-sub {
  font-size: 11px;
  font-weight: 500;
}

/* ─── Content Grid ────────────────────────────────── */
.content-grid {
  display: grid;
  grid-template-columns: 1fr 280px;
  gap: 16px;
  margin-bottom: 24px;
}

/* ─── Panels ──────────────────────────────────────── */
.panel {
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 12px;
  padding: 20px;
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.panel-title {
  font-size: 14px;
  font-weight: 600;
  color: #f3f6fc;
}

.panel-count {
  font-size: 12px;
  color: #8b8fa3;
}

/* ─── Filter Buttons ──────────────────────────────── */
.time-filters {
  display: flex;
  gap: 0;
  background: #0a0d14;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid #1e2030;
}

.filter-btn {
  padding: 6px 12px;
  border: none;
  background: transparent;
  color: #8b8fa3;
  font-size: 11px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
}
.filter-btn.active {
  background: #3b82f6;
  color: #fff;
}
.filter-btn:hover:not(.active) { color: #f3f6fc; }

/* ─── Timeline ────────────────────────────────────── */
.timeline {
  display: flex;
  flex-direction: column;
  gap: 0;
  max-height: 300px;
  overflow-y: auto;
}

.timeline-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid #1a1d2e;
}
.timeline-row:last-child { border-bottom: none; }

.timeline-time {
  font-size: 12px;
  color: #8b8fa3;
  font-family: 'JetBrains Mono', 'SF Mono', monospace;
  min-width: 44px;
}

.timeline-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.timeline-name {
  font-size: 13px;
  color: #f3f6fc;
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.timeline-badges {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

/* ─── Badges ──────────────────────────────────────── */
.badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  text-transform: capitalize;
  white-space: nowrap;
}

/* ─── Quick Actions ───────────────────────────────── */
.quick-list {
  display: flex;
  flex-direction: column;
  gap: 0;
}

.quick-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 0;
  border-bottom: 1px solid #1a1d2e;
  cursor: pointer;
  transition: all 0.15s;
  font-size: 13px;
  color: #b8c0d4;
}
.quick-item:last-child { border-bottom: none; }
.quick-item:hover { color: #f3f6fc; }

.quick-go {
  font-size: 11px;
  font-weight: 600;
  color: #22c55e;
  background: #0d1a12;
  padding: 3px 8px;
  border-radius: 4px;
}

/* ─── Attention Section ───────────────────────────── */
.attention-section {
  margin-bottom: 24px;
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.attention-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}

.attention-card {
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 12px;
  padding: 16px;
  transition: border-color 0.2s;
}
.attention-card:hover { border-color: #2a3556; }

.attention-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.attention-name {
  font-size: 13px;
  font-weight: 600;
  color: #f3f6fc;
}

.attention-meta {
  font-size: 11px;
  color: #8b8fa3;
  margin-bottom: 8px;
}

.attention-error {
  font-size: 11px;
  color: #ef4444;
  margin-bottom: 6px;
  line-height: 1.4;
}

.attention-next {
  font-size: 11px;
  color: #8b8fa3;
  margin-bottom: 12px;
}

.attention-actions {
  display: flex;
  gap: 6px;
}

/* ─── All Jobs Table ──────────────────────────────── */
.jobs-panel {
  margin-bottom: 24px;
}

.jobs-table {
  overflow-x: auto;
}

.jobs-header-row, .job-row {
  display: grid;
  grid-template-columns: 40px 1.5fr 1fr 80px 1fr 1fr 70px 80px;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
}

.jobs-header-row {
  font-size: 11px;
  font-weight: 600;
  color: #8b8fa3;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  border-bottom: 1px solid #1a1d2e;
}

.job-row {
  font-size: 12px;
  color: #b8c0d4;
  border-bottom: 1px solid rgba(26, 29, 46, 0.5);
  transition: background 0.15s;
  cursor: pointer;
}
.job-row:hover { background: rgba(59, 130, 246, 0.06); }

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}

/* Pulse animation for running jobs */
.job-row .status-dot[style*="rgb(59, 130, 246)"],
.job-row .status-dot[style*="#3b82f6"] {
  animation: pulse-running 1.5s ease-in-out infinite;
}

@keyframes pulse-running {
  0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(59, 130, 246, 0.5); }
  50% { opacity: 0.6; box-shadow: 0 0 0 4px rgba(59, 130, 246, 0); }
}

.job-name-text {
  color: #f3f6fc;
  font-weight: 500;
}

.job-id {
  font-size: 10px;
  color: #8b8fa3;
  margin-left: 4px;
}

.cron-code {
  font-family: 'JetBrains Mono', 'SF Mono', monospace;
  font-size: 11px;
  color: #b8c0d4;
  background: #0a0d14;
  padding: 2px 6px;
  border-radius: 4px;
}

.one-shot-badge {
  font-size: 10px;
  color: #ffb547;
  margin-left: 4px;
  font-weight: 700;
}

.fail-indicator {
  font-size: 10px;
  color: #ef4444;
  margin-left: 4px;
}

.action-btn {
  background: transparent;
  border: none;
  color: #8b8fa3;
  cursor: pointer;
  padding: 4px 6px;
  border-radius: 4px;
  font-size: 14px;
  transition: all 0.15s;
}
.action-btn:hover { background: #1e2233; color: #f3f6fc; }
.action-delete:hover { color: #ef4444; background: rgba(239, 68, 68, 0.1); }

/* ─── Empty State ─────────────────────────────────── */
.empty-state {
  text-align: center;
  padding: 32px;
  font-size: 13px;
  color: #8b8fa3;
}

/* ─── Modal ───────────────────────────────────────── */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.modal {
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 16px;
  padding: 28px;
  width: 520px;
  max-height: 90vh;
  overflow-y: auto;
  animation: modal-in 0.2s ease-out;
}

@keyframes modal-in {
  from { opacity: 0; transform: translateY(12px) scale(0.97); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

.modal-title {
  font-size: 18px;
  font-weight: 700;
  color: #f3f6fc;
  margin-bottom: 20px;
}

.form-group {
  margin-bottom: 16px;
}

.form-group label {
  display: block;
  font-size: 12px;
  font-weight: 500;
  color: #8b8fa3;
  margin-bottom: 6px;
}

.form-input {
  width: 100%;
  background: #0a0d14;
  border: 1px solid #1e2030;
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
  color: #f3f6fc;
  outline: none;
  transition: border-color 0.15s;
  font-family: inherit;
}
.form-input:focus { border-color: #3b82f6; }
.form-input::placeholder { color: #555872; }

.form-textarea {
  resize: vertical;
  min-height: 60px;
}

.form-hint {
  font-size: 11px;
  color: #555872;
  margin-top: 4px;
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.checkbox-label {
  display: flex !important;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  color: #b8c0d4 !important;
  font-size: 13px !important;
}

.checkbox-label input[type="checkbox"] {
  accent-color: #3b82f6;
  width: 16px;
  height: 16px;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid #1a1d2e;
}

select.form-input {
  appearance: none;
  background-image: url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3e%3cpath fill='none' stroke='%238b8fa3' stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='m2 5 6 6 6-6'/%3e%3c/svg%3e");
  background-repeat: no-repeat;
  background-position: right 12px center;
  background-size: 12px;
  padding-right: 36px;
}

.edit-info {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  background: #0a0d14;
  border: 1px solid #1a1d2e;
  border-radius: 8px;
  margin-bottom: 18px;
}

.edit-id {
  font-size: 11px;
  font-family: 'SF Mono', monospace;
  color: #8b8fa3;
}

/* ─── Mobile Attention Banner ─────────────────────── */
.attention-banner {
  display: none;
}

/* ─── Job Card (mobile card view) ────────────────── */
.job-card {
  display: none;
}

/* ═══ RESPONSIVE — Mobile ≤768px ══════════════════════════════════ */
@media (max-width: 768px) {

  /* Page wrapper — parent (.app-main__content) already pads 16px on mobile */
  .scheduler-page {
    padding: 0 0 80px;
    overflow-x: hidden;
    width: 100%;
    box-sizing: border-box;
  }

  /* Header: stack vertically */
  .scheduler-header {
    flex-direction: column;
    gap: 12px;
    margin-bottom: 16px;
    padding: 0;
  }

  .page-title {
    font-size: 20px;
  }

  .page-subtitle {
    display: none;
  }

  .header-actions {
    width: 100%;
    gap: 8px;
  }

  .header-actions .btn-secondary,
  .header-actions .btn-primary {
    flex: 1;
    text-align: center;
    font-size: 11px;
    padding: 8px 10px;
  }

  /* Stat grid: 2×2 — no extra padding since page has 16px padding */
  .stat-row {
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
    margin-bottom: 16px;
    padding: 0;
  }

  .stat-card {
    padding: 12px;
  }

  .stat-value {
    font-size: 22px;
  }

  /* Attention banner */
  .attention-banner {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 0 0 16px;
    padding: 12px 14px;
    background: rgba(239, 68, 68, 0.08);
    border: 1px solid rgba(239, 68, 68, 0.3);
    border-radius: 10px;
    font-size: 13px;
    font-weight: 600;
    color: #ef4444;
    cursor: pointer;
    transition: background 0.15s;
  }
  .attention-banner:hover {
    background: rgba(239, 68, 68, 0.14);
  }
  .attention-banner-icon { font-size: 16px; flex-shrink: 0; }
  .attention-banner-text { flex: 1; }
  .attention-banner-arrow { font-size: 14px; color: #ef4444; }

  /* Content grid: single column, no padding (page already has 16px) */
  .content-grid {
    grid-template-columns: 1fr;
    gap: 12px;
    margin-bottom: 16px;
    padding: 0;
  }

  /* Hide quick actions panel on mobile */
  .quick-panel {
    display: none;
  }

  .panel {
    padding: 14px;
    overflow: hidden;
  }

  /* Panel header: wrap filter buttons below title on mobile */
  .panel-header {
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }

  /* Time filters fit in remaining space */
  .time-filters {
    flex-shrink: 0;
    max-width: 100%;
  }

  .filter-btn {
    padding: 5px 10px;
    font-size: 10px;
  }

  /* Timeline: clip overflow */
  .timeline {
    max-height: 260px;
    overflow-y: auto;
    overflow-x: hidden;
  }

  .timeline-row {
    min-width: 0;
    overflow: hidden;
    gap: 8px;
  }

  .timeline-name {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
    flex: 1;
    font-size: 12px;
  }

  .timeline-time {
    min-width: 36px;
    font-size: 11px;
    flex-shrink: 0;
  }

  /* Show only first badge on mobile (hide status badge to save space) */
  .timeline-badges {
    gap: 4px;
    flex-shrink: 0;
  }

  .timeline-badges .badge:last-child {
    display: none;
  }

  .timeline-badges .badge {
    font-size: 9px;
    padding: 1px 5px;
  }

  /* Attention section: no extra padding */
  .attention-section {
    padding: 0;
    margin-bottom: 16px;
  }

  .attention-grid {
    grid-template-columns: 1fr;
    gap: 10px;
  }

  /* Jobs panel — no extra padding */
  .jobs-panel {
    padding: 0;
    margin-bottom: 16px;
  }

  .jobs-panel .panel {
    padding: 14px;
  }

  /* Hide desktop table rows */
  .jobs-table .jobs-header-row,
  .jobs-table .job-row {
    display: none;
  }

  /* Show mobile job cards */
  .job-card {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    background: #111318;
    border: 1px solid #1e2030;
    border-radius: 10px;
    margin-bottom: 8px;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
  }
  .job-card:hover {
    border-color: #2a3556;
    background: rgba(59, 130, 246, 0.04);
  }
  .job-card:last-child {
    margin-bottom: 0;
  }

  .job-card-top {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .job-card-name {
    font-size: 13px;
    font-weight: 600;
    color: #f3f6fc;
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .job-card-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    min-width: 0;
  }

  .job-card-cron {
    font-family: 'JetBrains Mono', 'SF Mono', monospace;
    font-size: 10px;
    color: #8b8fa3;
    background: #0a0d14;
    padding: 2px 6px;
    border-radius: 4px;
  }

  .job-card-next {
    font-size: 10px;
    color: #8b8fa3;
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .job-card-actions {
    display: flex;
    gap: 6px;
    justify-content: flex-end;
  }

  .job-card-actions .action-btn {
    padding: 6px 8px;
    font-size: 16px;
  }

  /* Modal: bottom sheet on mobile */
  .modal-overlay {
    align-items: flex-end;
    padding: 0;
  }

  .modal {
    width: 100% !important;
    max-width: 100% !important;
    border-radius: 16px 16px 0 0 !important;
    max-height: 92vh;
    overflow-y: auto;
    padding: 20px 16px 32px !important;
  }

  .form-row {
    flex-direction: column !important;
    gap: 0 !important;
  }
}
</style>
