<script setup>
import { ref, computed, onMounted, onUnmounted, watch, shallowRef } from 'vue'
import { useApprovalsStore } from '../stores/approvals'
import { useBreakpoint } from '../composables/useBreakpoint'
import { Codemirror } from 'vue-codemirror'
import { markdown } from '@codemirror/lang-markdown'
import { oneDark } from '@codemirror/theme-one-dark'
import { EditorView, lineNumbers } from '@codemirror/view'

const store = useApprovalsStore()
const { isMobile } = useBreakpoint()

// --- State ---
const filter = ref('pending')
const resolveComment = ref('')
const isResolving = ref(false)

// Mobile: when user taps a queue item, show detail (stackable nav)
const isMobileDetailView = computed(() =>
  isMobile.value && store.selectedId != null
)

// Comment popover state
const commentPopover = ref(null) // { line, selection }
const commentDraft = ref('')

// Edit state
const editingCommentId = ref(null)
const editingBody = ref('')

// CodeMirror view ref
const cmViewRef = shallowRef(null)

// --- Computed ---
const filteredList = computed(() => {
  if (filter.value === 'all') return store.approvalsList
  return store.approvalsList.filter(a => a.status === filter.value)
})

const detail = computed(() => store.selectedApproval)
const inlineComments = computed(() => detail.value?.comments || [])
const isPending = computed(() => detail.value?.status === 'pending')

// Waiting time
const waitingTime = ref(0)
let waitingTimer = null

function startWaitingTimer() {
  clearInterval(waitingTimer)
  waitingTimer = setInterval(() => {
    if (detail.value?.status === 'pending' && detail.value?.created_at) {
      waitingTime.value = Math.floor(Date.now() / 1000 - detail.value.created_at)
    }
  }, 1000)
}

function formatDuration(s) {
  if (!s) return '0s'
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${sec}s`
  return `${sec}s`
}

function formatTimeAgo(ts) {
  if (!ts) return ''
  const diff = Math.floor(Date.now() / 1000 - ts)
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

// Mobile back: deselect → go back to queue
function handleMobileBack() {
  store.selectApproval(null)
}

// --- CodeMirror Setup ---
const cmExtensions = [
  markdown(),
  oneDark,
  EditorView.editable.of(false),
  EditorView.lineWrapping,
  lineNumbers(),
  EditorView.theme({
    '&': { fontSize: '13px', fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace" },
    '.cm-content': { padding: '12px 0' },
    '.cm-gutters': { background: '#111318', borderRight: '1px solid #1a1d2e', cursor: 'pointer' },
    '.cm-activeLineGutter': { background: 'transparent' },
    '.cm-activeLine': { background: 'transparent' },
    '.cm-line': { padding: '0 16px' },
    '.cm-lineNumbers .cm-gutterElement': {
      cursor: 'pointer',
      userSelect: 'none',
    },
    '.cm-lineNumbers .cm-gutterElement:hover': {
      color: '#3b82f6 !important',
      background: 'rgba(59,130,246,0.1)',
    },
  }),
]

const cmContent = computed(() => detail.value?.content || '')

function handleCmReady({ view }) {
  cmViewRef.value = view

  // Gutter click handler: detect click on line number elements
  view.dom.addEventListener('click', (e) => {
    // Check if clicked on a gutter element (line number)
    const gutterEl = e.target.closest('.cm-lineNumbers .cm-gutterElement')
    if (gutterEl) {
      const lineNum = parseInt(gutterEl.textContent, 10)
      if (!isNaN(lineNum) && lineNum > 0) {
        openCommentForLine(lineNum)
      }
      e.preventDefault()
      e.stopPropagation()
      return
    }
  })

  // Text selection handler
  view.dom.addEventListener('mouseup', () => {
    setTimeout(() => {
      const sel = view.state.selection.main
      if (sel.from !== sel.to) {
        const fromLine = view.state.doc.lineAt(sel.from)
        const toLine = view.state.doc.lineAt(sel.to)
        const text = view.state.sliceDoc(sel.from, sel.to)
        openCommentForSelection({
          start_line: fromLine.number,
          end_line: toLine.number,
          start_offset: sel.from - fromLine.from,
          end_offset: sel.to - toLine.from,
          selected_text: text,
        })
      }
    }, 50) // small delay to let selection settle
  })
}

function openCommentForLine(lineNumber) {
  commentDraft.value = ''
  commentPopover.value = { line: lineNumber, selection: null }
}

function openCommentForSelection(selection) {
  commentDraft.value = ''
  commentPopover.value = { line: null, selection }
}

async function submitComment() {
  if (!commentDraft.value.trim() || !detail.value) return
  const pop = commentPopover.value
  try {
    await store.addComment(detail.value.id, {
      line_number: pop.line || null,
      selection: pop.selection || null,
      body: commentDraft.value.trim(),
      author: 'user',
    })
    commentPopover.value = null
    commentDraft.value = ''
  } catch (e) {
    console.error('Comment submit failed:', e)
  }
}

// --- Edit/Delete Comment ---
function startEditing(comment) {
  editingCommentId.value = comment.id
  editingBody.value = comment.body
}

function cancelEditing() {
  editingCommentId.value = null
  editingBody.value = ''
}

async function saveEditing() {
  if (!editingBody.value.trim()) return
  try {
    await store.updateComment(editingCommentId.value, editingBody.value.trim())
    editingCommentId.value = null
    editingBody.value = ''
  } catch (e) {
    console.error('Edit comment failed:', e)
  }
}

async function deleteComment(commentId) {
  try {
    await store.deleteComment(commentId)
  } catch (e) {
    console.error('Delete comment failed:', e)
  }
}

// --- Actions ---
async function doResolve(decision) {
  if (!detail.value) return
  isResolving.value = true
  try {
    await store.resolveApproval(detail.value.id, decision, resolveComment.value)
    resolveComment.value = ''
    await store.fetchStats()
    await store.fetchApprovals()
  } finally {
    isResolving.value = false
  }
}

function getCommentLocation(c) {
  if (c.line_number) return `Line ${c.line_number}`
  if (c.selection) return `Lines ${c.selection.start_line}–${c.selection.end_line}`
  return ''
}

// --- Lifecycle ---
onMounted(() => {
  store.fetchApprovals()
  store.fetchStats()
  startWaitingTimer()
})

onUnmounted(() => {
  clearInterval(waitingTimer)
})

watch(() => store.selectedId, () => {
  startWaitingTimer()
  commentPopover.value = null
  editingCommentId.value = null
})

// Urgency config
const urgencyConfig = {
  urgent: { color: '#ef4444', bg: '#3b1111', label: 'URGENT' },
  high:   { color: '#f59e0b', bg: '#3b2e11', label: 'HIGH' },
  normal: { color: '#3b82f6', bg: '#11243b', label: 'NORMAL' },
  low:    { color: '#6b7280', bg: '#1f2937', label: 'LOW' },
}

const typeLabels = {
  team_plan: 'Team Plan',
  architecture: 'Architecture',
  implementation_plan: 'Impl Plan',
  budget: 'Budget',
  deploy: 'Deploy',
  custom: 'Custom',
}
</script>

<template>
  <div class="approvals-page" :class="{ 'approvals-page--mobile': isMobile }">
    <!-- Page Header -->
    <div class="page-header">
      <!-- Mobile detail: show back button -->
      <button
        v-if="isMobileDetailView"
        class="mobile-back-btn"
        @click="handleMobileBack"
      >
        <svg viewBox="0 0 24 24" fill="none" width="18" height="18">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
      <h1>{{ isMobileDetailView ? 'Review Approval' : 'Approvals' }}</h1>
      <p v-if="!isMobileDetailView" class="page-desc">Review and approve agent requests</p>
    </div>

    <!-- Stats Cards (hidden on mobile detail view) -->
    <div v-if="!isMobileDetailView" class="stats-row">
      <div class="stat-card stat-pending">
        <div class="stat-value">{{ store.stats.pending_count || 0 }}</div>
        <div class="stat-label">Pending</div>
      </div>
      <div class="stat-card stat-approved">
        <div class="stat-value">{{ store.stats.approved_today || 0 }}</div>
        <div class="stat-label">Approved Today</div>
      </div>
      <div class="stat-card stat-rejected">
        <div class="stat-value">{{ store.stats.rejected_count || 0 }}</div>
        <div class="stat-label">Rejected</div>
      </div>
      <div class="stat-card stat-time">
        <div class="stat-value">{{ formatDuration(Math.round(store.stats.avg_response_time || 0)) }}</div>
        <div class="stat-label">Avg Response</div>
      </div>
    </div>

    <!-- Main Content -->
    <div class="main-content">
      <!-- Left: Queue (hidden on mobile when detail is shown) -->
      <div
        class="queue-panel"
        :class="{ 'queue-panel--hidden': isMobileDetailView }"
      >
        <div class="queue-header">
          <span class="queue-title">Queue</span>
          <div class="filter-tabs">
            <button
              v-for="f in ['pending', 'approved', 'rejected', 'all']"
              :key="f"
              :class="['filter-btn', { active: filter === f }]"
              @click="filter = f"
            >{{ f }}</button>
          </div>
        </div>
        <div class="queue-list">
          <div
            v-for="item in filteredList"
            :key="item.id"
            :class="['queue-item', { selected: store.selectedId === item.id }]"
            @click="store.selectApproval(item.id)"
          >
            <div class="qi-top">
              <span
                class="qi-urgency"
                :style="{ color: urgencyConfig[item.urgency]?.color, background: urgencyConfig[item.urgency]?.bg }"
              >{{ urgencyConfig[item.urgency]?.label || 'NORMAL' }}</span>
              <span class="qi-agent">{{ item.agent_name }}</span>
            </div>
            <div class="qi-title">{{ item.title }}</div>
            <div class="qi-meta">
              <span class="qi-type">{{ typeLabels[item.approval_type] || item.approval_type }}</span>
              <span class="qi-dot">•</span>
              <span>{{ formatTimeAgo(item.created_at) }}</span>
              <template v-if="item.status === 'pending'">
                <span class="qi-dot">•</span>
                <span class="qi-waiting">⏳ waiting</span>
              </template>
            </div>
          </div>
          <div v-if="filteredList.length === 0" class="queue-empty">
            No {{ filter === 'all' ? '' : filter }} approvals
          </div>
        </div>
      </div>

      <!-- Right: Detail -->
      <div class="detail-panel" v-if="detail">
        <!-- Detail Header -->
        <div class="detail-header">
          <div class="dh-top">
            <h2 class="dh-title">{{ detail.title }}</h2>
            <span :class="['status-badge', `status-${detail.status}`]">{{ detail.status }}</span>
          </div>
          <div class="dh-meta">
            <span class="dh-type-badge">{{ typeLabels[detail.approval_type] || detail.approval_type }}</span>
            <span class="dh-sep">•</span>
            <span>{{ detail.agent_name }}</span>
            <span class="dh-sep">•</span>
            <span>{{ formatTimeAgo(detail.created_at) }}</span>
            <template v-if="detail.team_name">
              <span class="dh-sep">•</span>
              <span class="dh-team">Team: {{ detail.team_name }}</span>
            </template>
          </div>
          <div v-if="detail.status === 'pending'" class="dh-waiting">
            ⏳ Waiting for {{ formatDuration(waitingTime) }}
            <template v-if="detail.paused_agents?.length">
              — {{ detail.paused_agents.length }} agent(s) paused
            </template>
          </div>
        </div>

        <!-- CodeMirror Viewer -->
        <div class="cm-container">
          <div class="cm-toolbar">
            <span class="cm-toolbar-label">📄 Content ({{ detail.content_format || 'text' }})</span>
            <span class="cm-toolbar-hint">Click line number or select text to comment</span>
          </div>
          <Codemirror
            :model-value="cmContent"
            :extensions="cmExtensions"
            :style="{ maxHeight: isMobile ? '280px' : '400px' }"
            @ready="handleCmReady"
          />
        </div>

        <!-- Comment Popover -->
        <div v-if="commentPopover" class="comment-popover">
          <div class="cp-header">
            <span v-if="commentPopover.line">💬 Comment on Line {{ commentPopover.line }}</span>
            <span v-else-if="commentPopover.selection">
              💬 Comment on Lines {{ commentPopover.selection.start_line }}–{{ commentPopover.selection.end_line }}
            </span>
            <button class="cp-close" @click="commentPopover = null">✕</button>
          </div>
          <div v-if="commentPopover.selection?.selected_text" class="cp-quoted">
            <pre>{{ commentPopover.selection.selected_text }}</pre>
          </div>
          <textarea
            v-model="commentDraft"
            class="cp-input"
            placeholder="Write your comment..."
            rows="3"
            @keydown.ctrl.enter.prevent="submitComment"
            @keydown.meta.enter.prevent="submitComment"
          ></textarea>
          <div class="cp-actions">
            <button class="btn-secondary" @click="commentPopover = null">Cancel</button>
            <button class="btn-primary" :disabled="!commentDraft.trim()" @click="submitComment">
              Submit (Ctrl+Enter)
            </button>
          </div>
        </div>

        <!-- Inline Comments List -->
        <div v-if="inlineComments.length" class="comments-section">
          <h3 class="section-title">💬 Inline Comments ({{ inlineComments.length }})</h3>
          <div v-for="c in inlineComments" :key="c.id" class="comment-card">
            <!-- Normal view -->
            <template v-if="editingCommentId !== c.id">
              <div class="cc-header">
                <span class="cc-author">{{ c.author || 'user' }}</span>
                <span class="cc-location">{{ getCommentLocation(c) }}</span>
                <span class="cc-time">{{ formatTimeAgo(c.created_at) }}</span>
                <div class="cc-actions" v-if="isPending">
                  <button class="cc-btn" @click="startEditing(c)" title="Edit">✏️</button>
                  <button class="cc-btn cc-btn-delete" @click="deleteComment(c.id)" title="Delete">🗑️</button>
                </div>
              </div>
              <div v-if="c.selection?.selected_text" class="cc-quoted">
                <pre>{{ c.selection.selected_text }}</pre>
              </div>
              <div class="cc-body">{{ c.body }}</div>
            </template>

            <!-- Edit mode -->
            <template v-else>
              <div class="cc-header">
                <span class="cc-author">Editing comment</span>
                <span class="cc-location">{{ getCommentLocation(c) }}</span>
              </div>
              <textarea
                v-model="editingBody"
                class="cc-edit-input"
                rows="3"
                @keydown.ctrl.enter.prevent="saveEditing"
                @keydown.meta.enter.prevent="saveEditing"
              ></textarea>
              <div class="cc-edit-actions">
                <button class="btn-secondary btn-sm" @click="cancelEditing">Cancel</button>
                <button class="btn-primary btn-sm" :disabled="!editingBody.trim()" @click="saveEditing">Save</button>
              </div>
            </template>
          </div>
        </div>

        <!-- Impact Analysis -->
        <div v-if="detail.impact_files || detail.impact_services || detail.impact_downtime || detail.impact_risk"
             class="impact-section">
          <h3 class="section-title">📊 Impact Analysis</h3>
          <div class="impact-grid">
            <div v-if="detail.impact_files != null" class="impact-box">
              <div class="ib-value">{{ detail.impact_files }}</div>
              <div class="ib-label">Files</div>
            </div>
            <div v-if="detail.impact_services != null" class="impact-box">
              <div class="ib-value">{{ detail.impact_services }}</div>
              <div class="ib-label">Services</div>
            </div>
            <div v-if="detail.impact_downtime" class="impact-box">
              <div class="ib-value">{{ detail.impact_downtime }}</div>
              <div class="ib-label">Downtime</div>
            </div>
            <div v-if="detail.impact_risk" class="impact-box">
              <div class="ib-value risk" :class="'risk-' + detail.impact_risk">{{ detail.impact_risk }}</div>
              <div class="ib-label">Risk Level</div>
            </div>
          </div>
        </div>

        <!-- Resolve Actions -->
        <div v-if="isPending" class="resolve-section">
          <textarea
            v-model="resolveComment"
            class="resolve-input"
            placeholder="Optional comment for the agent..."
            rows="2"
          ></textarea>
          <div class="resolve-actions">
            <button class="btn-reject" :disabled="isResolving" @click="doResolve('reject')">
              ✕ Reject
            </button>
            <button class="btn-approve" :disabled="isResolving" @click="doResolve('approve')">
              ✓ Approve
            </button>
          </div>
        </div>

        <!-- Already resolved -->
        <div v-else class="resolved-section">
          <div class="resolved-info">
            <div v-if="detail.user_comment" class="resolved-comment">
              {{ detail.user_comment }}
            </div>
            <div class="resolved-time">{{ formatTimeAgo(detail.resolved_at) }}</div>
          </div>
          <div class="resolved-badge" :class="detail.status">
            {{ detail.status === 'approved' ? '✓ Approved' : '✕ Rejected' }}
          </div>
        </div>
      </div>

      <!-- Empty State -->
      <div v-else class="detail-panel detail-empty">
        <div class="empty-state">
          <div class="empty-icon">📋</div>
          <div class="empty-text">Select an approval to review</div>
          <div class="empty-sub">Approvals from agents will appear in the queue</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.approvals-page { color: var(--text-primary, #f0f2f5); }

.page-header { margin-bottom: 20px; display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.page-header h1 { font-size: 20px; font-weight: 700; margin: 0; }
.page-desc { font-size: 13px; color: var(--text-muted, #8b8fa3); margin: 0; width: 100%; }

/* Mobile back button */
.mobile-back-btn {
  display: flex; align-items: center; justify-content: center;
  width: 36px; height: 36px; border-radius: 8px;
  background: transparent; border: 1px solid var(--border-primary, #1a1d2e);
  color: var(--accent-blue, #3b82f6); cursor: pointer;
  transition: all 0.15s; flex-shrink: 0;
}
.mobile-back-btn:active { background: rgba(59,130,246,0.1); }

/* Stats */
.stats-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 20px;
}
.stat-card {
  background: var(--bg-card, #0c0e15);
  border: 1px solid var(--border-primary, #1a1d2e);
  border-radius: 10px;
  padding: 16px 18px;
}
.stat-value { font-size: 24px; font-weight: 700; line-height: 1.2; }
.stat-label { font-size: 12px; color: var(--text-muted, #8b8fa3); margin-top: 4px; }
.stat-pending .stat-value { color: #f59e0b; }
.stat-approved .stat-value { color: #10b981; }
.stat-rejected .stat-value { color: #ef4444; }
.stat-time .stat-value { color: #3b82f6; }

/* Main Layout */
.main-content {
  display: flex;
  gap: 16px;
  height: calc(100vh - 230px);
  min-height: 500px;
}

/* Queue Panel */
.queue-panel {
  width: 340px;
  min-width: 340px;
  background: var(--bg-card, #0c0e15);
  border: 1px solid var(--border-primary, #1a1d2e);
  border-radius: 10px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.queue-header {
  padding: 14px 16px 10px;
  border-bottom: 1px solid var(--border-primary, #1a1d2e);
}
.queue-title { font-size: 14px; font-weight: 600; }
.filter-tabs { display: flex; gap: 4px; margin-top: 8px; }
.filter-btn {
  font-size: 11px; padding: 4px 10px; border-radius: 6px;
  background: transparent; color: var(--text-muted, #8b8fa3);
  border: 1px solid transparent; cursor: pointer; text-transform: capitalize;
  transition: all 0.15s;
}
.filter-btn:hover { color: var(--text-primary, #f0f2f5); background: rgba(255,255,255,0.05); }
.filter-btn.active { color: var(--text-primary, #f0f2f5); background: rgba(59,130,246,0.15); border-color: rgba(59,130,246,0.3); }

.queue-list { flex: 1; overflow-y: auto; padding: 8px; }
.queue-item {
  padding: 12px; border-radius: 8px; cursor: pointer;
  border: 1px solid transparent; margin-bottom: 4px; transition: all 0.15s;
}
.queue-item:hover { background: rgba(255,255,255,0.03); }
.queue-item.selected { background: rgba(59,130,246,0.08); border-color: rgba(59,130,246,0.25); }

.qi-top { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.qi-urgency { font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; letter-spacing: 0.5px; }
.qi-agent { font-size: 12px; color: var(--text-secondary, #c4c8d4); }
.qi-title { font-size: 13px; font-weight: 500; margin-bottom: 4px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.qi-meta { font-size: 11px; color: var(--text-muted, #8b8fa3); display: flex; align-items: center; gap: 6px; }
.qi-type { color: var(--text-secondary, #c4c8d4); }
.qi-dot { opacity: 0.4; }
.qi-waiting { color: #f59e0b; }
.queue-empty { padding: 24px; text-align: center; color: var(--text-muted, #8b8fa3); font-size: 13px; }

/* Detail Panel */
.detail-panel {
  flex: 1; background: var(--bg-card, #0c0e15);
  border: 1px solid var(--border-primary, #1a1d2e);
  border-radius: 10px; padding: 20px; overflow-y: auto;
}
.detail-empty { display: flex; align-items: center; justify-content: center; }
.empty-state { text-align: center; }
.empty-icon { font-size: 48px; margin-bottom: 12px; opacity: 0.5; }
.empty-text { font-size: 16px; color: var(--text-secondary, #c4c8d4); }
.empty-sub { font-size: 13px; color: var(--text-muted, #8b8fa3); margin-top: 4px; }

/* Detail Header */
.detail-header { margin-bottom: 16px; }
.dh-top { display: flex; align-items: center; gap: 12px; }
.dh-title { font-size: 18px; font-weight: 600; margin: 0; flex: 1; }
.status-badge {
  font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 6px;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.status-pending { background: rgba(245,158,11,0.15); color: #f59e0b; }
.status-approved { background: rgba(16,185,129,0.15); color: #10b981; }
.status-rejected { background: rgba(239,68,68,0.15); color: #ef4444; }

.dh-meta { font-size: 12px; color: var(--text-muted, #8b8fa3); margin-top: 8px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.dh-type-badge { color: var(--accent-blue, #3b82f6); font-weight: 500; }
.dh-sep { opacity: 0.3; }
.dh-team { color: #6366f1; }
.dh-waiting {
  margin-top: 8px; font-size: 12px; color: #f59e0b;
  background: rgba(245,158,11,0.08); padding: 6px 12px; border-radius: 6px;
}

/* CodeMirror Container */
.cm-container {
  border: 1px solid var(--border-primary, #1a1d2e);
  border-radius: 8px; overflow: hidden; margin-bottom: 16px;
}
.cm-toolbar {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 12px; background: #111318;
  border-bottom: 1px solid var(--border-primary, #1a1d2e);
}
.cm-toolbar-label { font-size: 12px; font-weight: 500; }
.cm-toolbar-hint { font-size: 11px; color: var(--text-muted, #8b8fa3); }

/* Comment Popover */
.comment-popover {
  background: #111318;
  border: 1px solid rgba(59,130,246,0.3);
  border-radius: 10px; padding: 14px; margin-bottom: 16px;
  box-shadow: 0 4px 24px rgba(0,0,0,0.3);
}
.cp-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; font-size: 13px; font-weight: 500; }
.cp-close { background: none; border: none; color: var(--text-muted); cursor: pointer; font-size: 16px; padding: 4px; }
.cp-close:hover { color: var(--text-primary); }
.cp-quoted {
  background: rgba(255,255,255,0.03); border-left: 3px solid #3b82f6;
  padding: 8px 12px; margin-bottom: 10px; border-radius: 4px;
  max-height: 80px; overflow-y: auto;
}
.cp-quoted pre { margin: 0; font-size: 12px; color: var(--text-secondary); white-space: pre-wrap; font-family: monospace; }
.cp-input {
  width: 100%; background: rgba(255,255,255,0.04);
  border: 1px solid #1e2030; border-radius: 6px;
  padding: 8px 12px; color: var(--text-primary); font-size: 13px;
  resize: vertical; font-family: inherit;
  box-sizing: border-box;
}
.cp-input:focus { outline: none; border-color: #3b82f6; }
.cp-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 10px; }

/* Comments Section */
.comments-section { margin-bottom: 16px; }
.section-title { font-size: 14px; font-weight: 600; margin: 0 0 10px; }
.comment-card {
  background: rgba(255,255,255,0.02);
  border: 1px solid var(--border-primary, #1a1d2e);
  border-radius: 8px; padding: 10px 14px; margin-bottom: 6px;
  transition: border-color 0.15s;
}
.comment-card:hover { border-color: rgba(255,255,255,0.1); }

.cc-header { display: flex; align-items: center; gap: 8px; font-size: 12px; margin-bottom: 6px; }
.cc-author { font-weight: 600; color: #6366f1; }
.cc-location { color: #3b82f6; font-family: monospace; }
.cc-time { color: var(--text-muted); margin-left: auto; }
.cc-actions { display: flex; gap: 2px; margin-left: 8px; }
.cc-btn {
  background: none; border: none; cursor: pointer;
  font-size: 12px; padding: 2px 4px; border-radius: 4px;
  opacity: 0.5; transition: opacity 0.15s;
}
.cc-btn:hover { opacity: 1; }
.cc-btn-delete:hover { background: rgba(239,68,68,0.15); }

.cc-quoted {
  background: rgba(255,255,255,0.03); border-left: 2px solid #555;
  padding: 4px 10px; margin-bottom: 6px; border-radius: 3px;
}
.cc-quoted pre { margin: 0; font-size: 11px; color: var(--text-muted); white-space: pre-wrap; font-family: monospace; }
.cc-body { font-size: 13px; line-height: 1.5; }

.cc-edit-input {
  width: 100%; background: rgba(255,255,255,0.04);
  border: 1px solid #3b82f6; border-radius: 6px;
  padding: 8px 12px; color: var(--text-primary); font-size: 13px;
  resize: vertical; font-family: inherit; margin-top: 6px;
  box-sizing: border-box;
}
.cc-edit-input:focus { outline: none; }
.cc-edit-actions { display: flex; justify-content: flex-end; gap: 6px; margin-top: 8px; }

/* Impact */
.impact-section { margin-bottom: 16px; }
.impact-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.impact-box {
  background: rgba(255,255,255,0.03);
  border: 1px solid var(--border-primary, #1a1d2e);
  border-radius: 8px; padding: 12px; text-align: center;
}
.ib-value { font-size: 18px; font-weight: 700; }
.ib-label { font-size: 11px; color: var(--text-muted); margin-top: 4px; }
.risk-low { color: #10b981; }
.risk-medium { color: #f59e0b; }
.risk-high { color: #ef4444; }
.risk-critical { color: #ef4444; text-shadow: 0 0 8px rgba(239,68,68,0.5); }

/* Resolve Section */
.resolve-section { border-top: 1px solid var(--border-primary); padding-top: 16px; }
.resolve-input {
  width: 100%; background: rgba(255,255,255,0.04);
  border: 1px solid #1e2030; border-radius: 8px;
  padding: 10px 14px; color: var(--text-primary); font-size: 13px;
  resize: none; font-family: inherit; box-sizing: border-box;
}
.resolve-input:focus { outline: none; border-color: #3b82f6; }
.resolve-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 12px; }

/* Buttons */
.btn-primary {
  padding: 6px 16px; border-radius: 6px; border: none;
  font-size: 12px; font-weight: 600; cursor: pointer;
  background: #3b82f6; color: #fff; transition: all 0.15s;
}
.btn-primary:hover { background: #2563eb; }
.btn-primary:disabled { opacity: 0.5; cursor: default; }

.btn-secondary {
  padding: 6px 16px; border-radius: 6px;
  border: 1px solid #333; font-size: 12px; font-weight: 500;
  cursor: pointer; background: transparent;
  color: var(--text-secondary); transition: all 0.15s;
}
.btn-secondary:hover { border-color: #555; color: var(--text-primary); }

.btn-sm { padding: 4px 12px; font-size: 11px; }

.btn-reject {
  padding: 10px 28px; border-radius: 8px;
  border: 1px solid #ef4444; font-size: 13px; font-weight: 600;
  cursor: pointer; background: transparent; color: #ef4444;
  transition: all 0.15s;
}
.btn-reject:hover { background: rgba(239,68,68,0.1); }
.btn-reject:disabled { opacity: 0.5; cursor: default; }

.btn-approve {
  padding: 10px 28px; border-radius: 8px; border: none;
  font-size: 13px; font-weight: 600; cursor: pointer;
  background: linear-gradient(135deg, #10b981, #059669);
  color: #fff; transition: all 0.15s;
  box-shadow: 0 2px 12px rgba(16,185,129,0.25);
}
.btn-approve:hover { box-shadow: 0 4px 20px rgba(16,185,129,0.35); transform: translateY(-1px); }
.btn-approve:disabled { opacity: 0.5; cursor: default; transform: none; box-shadow: none; }

/* Resolved */
.resolved-section {
  border-top: 1px solid var(--border-primary); padding-top: 16px;
  display: flex; align-items: flex-start; gap: 14px;
}
.resolved-badge {
  display: inline-block; font-size: 13px; font-weight: 700;
  padding: 6px 16px; border-radius: 8px; white-space: nowrap; flex-shrink: 0;
}
.resolved-badge.approved { background: rgba(16,185,129,0.15); color: #10b981; }
.resolved-badge.rejected { background: rgba(239,68,68,0.15); color: #ef4444; }
.resolved-info { flex: 1; min-width: 0; }
.resolved-comment { font-size: 13px; color: var(--text-secondary); line-height: 1.5; }
.resolved-time { font-size: 12px; color: var(--text-muted); margin-top: 4px; }

/* ═══════════════════════════════════════
   MOBILE RESPONSIVE (< 768px)
   ═══════════════════════════════════════ */
@media (max-width: 767px) {
  /* Stats grid: 2×2 */
  .stats-row {
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
    margin-bottom: 12px;
  }
  .stat-card {
    padding: 12px 14px;
  }
  .stat-value { font-size: 20px; }
  .stat-label { font-size: 11px; }

  /* Main content: stack instead of side-by-side */
  .main-content {
    flex-direction: column;
    height: auto;
    min-height: 0;
    gap: 0;
  }

  /* Queue takes full width on mobile */
  .queue-panel {
    width: 100%;
    min-width: 0;
    border-radius: 10px;
    max-height: none;
    flex: 1;
  }

  /* Hide queue when detail shown on mobile */
  .queue-panel--hidden { display: none; }

  /* Queue items: larger touch targets */
  .queue-item {
    padding: 14px;
    min-height: 44px;
  }
  .qi-title { font-size: 14px; }

  /* Detail panel: full width, no border-radius at edges */
  .detail-panel {
    border-radius: 10px;
    padding: 16px;
    border: none;
    background: var(--bg-base, #0a0d14);
  }

  /* Detail header tweaks */
  .dh-title { font-size: 16px; }
  .dh-meta { font-size: 11px; }
  .dh-waiting { font-size: 11px; padding: 6px 10px; }

  /* CM toolbar: stack on mobile */
  .cm-toolbar { flex-direction: column; align-items: flex-start; gap: 2px; padding: 6px 10px; }
  .cm-toolbar-label { font-size: 11px; }
  .cm-toolbar-hint { font-size: 10px; }

  /* Impact grid 2×2 on mobile */
  .impact-grid {
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
  }
  .impact-box { padding: 10px; }
  .ib-value { font-size: 16px; }

  /* Resolve section: sticky bottom bar */
  .resolve-section {
    position: sticky;
    bottom: 0;
    background: var(--bg-card, #0c0e15);
    border-top: 1px solid var(--border-primary, #1a1d2e);
    padding: 12px 16px;
    margin: 16px -16px -16px; /* bleed to edges */
    border-radius: 0;
    z-index: 10;
  }
  .resolve-input { rows: 1; font-size: 12px; padding: 8px 12px; }
  .resolve-actions {
    justify-content: stretch;
    gap: 8px;
  }
  .btn-reject, .btn-approve {
    flex: 1;
    padding: 12px 0;
    text-align: center;
    min-height: 44px;
  }

  /* Comment actions on mobile: always visible */
  .cc-btn { opacity: 1; }

  /* Empty state */
  .detail-empty { display: none; }

  /* Page header adjustments */
  .page-header h1 { font-size: 18px; }
}
</style>
