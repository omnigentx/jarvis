<script setup>
/**
 * Approvals — plan / document review queue with inline comments.
 *
 * Logic preserved verbatim from the previous ApprovalsView: store-driven
 * queue, CodeMirror gutter click → comment popover, text selection →
 * range comment, edit / delete comments, approve / reject resolution.
 * Only the visual shell + tokens were rewritten.
 */
import { ref, computed, onMounted, onUnmounted, watch, shallowRef } from 'vue'
import { useApprovalsStore } from '../stores/approvals'
import { useBreakpoint } from '../composables/useBreakpoint'
import { Codemirror } from 'vue-codemirror'
import { markdown } from '@codemirror/lang-markdown'
import { oneDark } from '@codemirror/theme-one-dark'
import { EditorView, lineNumbers } from '@codemirror/view'
import MarkdownRenderer from '../components/MarkdownRenderer.vue'

const viewMode = ref('preview')

const store = useApprovalsStore()
const { isMobile } = useBreakpoint()

const filter = ref('pending')
const resolveComment = ref('')
const isResolving = ref(false)

const isMobileDetailView = computed(() =>
  isMobile.value && store.selectedId != null
)

const commentPopover = ref(null)
const commentDraft = ref('')

const editingCommentId = ref(null)
const editingBody = ref('')

const cmViewRef = shallowRef(null)

const filteredList = computed(() => {
  if (filter.value === 'all') return store.approvalsList
  return store.approvalsList.filter(a => a.status === filter.value)
})

const detail = computed(() => store.selectedApproval)
const inlineComments = computed(() => detail.value?.comments || [])
const isPending = computed(() => detail.value?.status === 'pending')

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

function handleMobileBack() {
  store.selectApproval(null)
}

// CodeMirror
const cmExtensions = [
  markdown(),
  oneDark,
  EditorView.editable.of(false),
  EditorView.lineWrapping,
  lineNumbers(),
  EditorView.theme({
    '&': { fontSize: '13px', fontFamily: "'Geist Mono', 'JetBrains Mono', monospace" },
    '.cm-content': { padding: '12px 0' },
    '.cm-gutters': { background: 'var(--bg-2)', borderRight: '1px solid var(--border)', cursor: 'pointer' },
    '.cm-activeLineGutter': { background: 'transparent' },
    '.cm-activeLine': { background: 'transparent' },
    '.cm-line': { padding: '0 16px' },
    '.cm-lineNumbers .cm-gutterElement': {
      cursor: 'pointer',
      userSelect: 'none',
    },
    '.cm-lineNumbers .cm-gutterElement:hover': {
      color: 'var(--accent) !important',
      background: 'var(--accent-bg)',
    },
  }),
]

const cmContent = computed(() => detail.value?.content || '')

function handleCmReady({ view }) {
  cmViewRef.value = view

  view.dom.addEventListener('click', (e) => {
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
    }, 50)
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

const typeLabels = {
  team_plan: 'Team Plan',
  architecture: 'Architecture',
  implementation_plan: 'Impl Plan',
  budget: 'Budget',
  deploy: 'Deploy',
  brd: 'BRD',
  custom: 'Custom',
}

const typeColors = {
  team_plan: 'var(--role-pm)',
  architecture: 'var(--role-sa)',
  implementation_plan: 'var(--role-dev)',
  budget: 'var(--role-qe)',
  deploy: 'var(--role-dso)',
  brd: 'var(--role-ba)',
  custom: 'var(--role-des)',
}

// Overflow banner — when the oldest pending approval has waited > 14m.
const overflowWarning = computed(() => {
  const pending = store.approvalsList.filter(a => a.status === 'pending')
  if (!pending.length) return null
  const now = Math.floor(Date.now() / 1000)
  let maxWait = 0
  for (const a of pending) {
    if (a.created_at) {
      maxWait = Math.max(maxWait, now - a.created_at)
    }
  }
  if (maxWait < 14 * 60) return null
  const blockedCount = pending.filter(a => (a.paused_agents?.length || 0) > 0).length
  return {
    waitMin: Math.floor(maxWait / 60),
    blockedCount,
  }
})

const knownTypes = [
  { key: 'team_plan', desc: 'PM submits sprint plans — backlog, role assignments, DoD.' },
  { key: 'architecture', desc: 'SA submits ADRs / system design before DEV implements.' },
  { key: 'implementation_plan', desc: 'DEV submits an impl plan (file map, branch, scope) before writing.' },
  { key: 'brd', desc: 'BA submits a Business Requirements Doc for acceptance review.' },
]
</script>

<template>
  <div class="approvals jv" :class="{ 'approvals--mobile': isMobile }">
    <!-- ─── Header ─── -->
    <div class="approvals__header">
      <button
        v-if="isMobileDetailView"
        class="btn btn-icon btn-ghost"
        @click="handleMobileBack"
      >
        <svg viewBox="0 0 24 24" fill="none" width="14" height="14">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
      <div class="approvals__heading" v-if="!isMobileDetailView">
        <div class="eyebrow">OPERATIONS · APPROVALS</div>
        <h1 class="approvals__title">
          Plan &amp; document <span class="grad" style="font-style: italic;">review</span>
          <span class="approvals__title-sub">
            · {{ store.stats.pending_count || 0 }} pending
          </span>
        </h1>
        <p class="approvals__desc">
          Each item is a plan or doc an agent sent via
          <code class="approvals__inline-code">request_approval</code>.
          Comment by line or selection range before approving.
        </p>
      </div>
      <h1 v-else class="approvals__title approvals__title--mobile">Review approval</h1>
    </div>

    <!-- ─── Overflow warning ─── -->
    <div v-if="overflowWarning && !isMobileDetailView" class="approvals__overflow">
      <span class="approvals__overflow-dot"></span>
      <span class="approvals__overflow-text">
        Queue at {{ overflowWarning.waitMin }}m wait
        <template v-if="overflowWarning.blockedCount">
          · {{ overflowWarning.blockedCount }} blocked agent{{ overflowWarning.blockedCount > 1 ? 's' : '' }}
        </template>
      </span>
    </div>

    <!-- ─── Main two-pane ───
         Always render so the queue tabs (Pending | Approved | Rejected | All)
         stay reachable even when Pending=0. The "no pending → catalog hero"
         empty state used to REPLACE this whole block, which hid the tabs and
         left users unable to browse history. The catalog now lives inside
         the detail area for the same context (filter=pending + empty), with
         the tabs preserved in the queue panel above.  -->
    <div class="approvals__main">
      <!-- Queue -->
      <aside
        class="approvals__queue"
        :class="{ 'approvals__queue--hidden': isMobileDetailView }"
      >
        <div class="approvals__queue-head">
          <div class="seg approvals__seg">
            <button
              v-for="f in ['pending', 'approved', 'rejected', 'all']"
              :key="f"
              :class="{ 'is-active': filter === f }"
              @click="filter = f"
            >{{ f }}</button>
          </div>
        </div>
        <div class="approvals__queue-list">
          <button
            v-for="item in filteredList"
            :key="item.id"
            type="button"
            class="approvals-row"
            :class="{ 'approvals-row--active': store.selectedId === item.id }"
            @click="store.selectApproval(item.id)"
          >
            <div class="approvals-row__top">
              <span
                class="approvals-row__type"
                :style="{ borderColor: 'color-mix(in srgb, ' + (typeColors[item.approval_type] || 'var(--primary)') + ' 40%, transparent)', color: typeColors[item.approval_type] || 'var(--primary)' }"
              >
                {{ typeLabels[item.approval_type] || item.approval_type }}
              </span>
              <span
                v-if="item.status === 'pending'"
                class="approvals-row__wait"
              >blocked {{ formatTimeAgo(item.created_at) }}</span>
              <span
                v-else
                class="chip"
                :class="{
                  'chip-success': item.status === 'approved',
                  'chip-danger': item.status === 'rejected',
                }"
              ><span class="chip-dot"></span> {{ item.status }}</span>
            </div>
            <div class="approvals-row__title">{{ item.title }}</div>
            <div class="approvals-row__meta">
              <span class="approvals-row__agent">{{ item.agent_name }}</span>
              <span v-if="item.team_name">· {{ item.team_name }}</span>
            </div>
            <div class="approvals-row__foot">
              <span class="mono-label" style="font-size: 9px;">{{ formatTimeAgo(item.created_at) }}</span>
              <span v-if="(item.comments?.length || item.comment_count || 0) > 0" class="approvals-row__comments">
                💬 {{ item.comments?.length || item.comment_count }}
              </span>
            </div>
          </button>
          <div v-if="filteredList.length === 0" class="approvals__queue-empty">
            No {{ filter === 'all' ? '' : filter }} approvals
          </div>
        </div>
      </aside>

      <!-- Detail -->
      <section v-if="detail" class="approvals__detail">
        <!-- Detail header -->
        <div class="approvals__detail-head">
          <span
            class="approvals-row__type"
            :style="{ borderColor: 'color-mix(in srgb, ' + (typeColors[detail.approval_type] || 'var(--primary)') + ' 40%, transparent)', color: typeColors[detail.approval_type] || 'var(--primary)' }"
          >
            {{ typeLabels[detail.approval_type] || detail.approval_type }}
          </span>
          <div class="approvals__detail-info">
            <h2 class="approvals__detail-title">{{ detail.title }}</h2>
            <div class="mono-label" style="font-size: 9.5px;">
              <span>{{ detail.agent_name }}</span>
              <span v-if="detail.team_name"> · {{ detail.team_name }}</span>
              <span> · {{ formatTimeAgo(detail.created_at) }}</span>
              <span v-if="detail.status === 'pending'"> · blocking</span>
            </div>
          </div>
          <div class="seg" v-if="detail.content_format === 'markdown' || detail.content_format === 'text'">
            <button :class="{ 'is-active': viewMode === 'preview' }" @click="viewMode = 'preview'">Preview</button>
            <button :class="{ 'is-active': viewMode === 'source' }" @click="viewMode = 'source'">Source</button>
          </div>
        </div>

        <!-- Hint bar -->
        <div v-if="detail.status === 'pending' && viewMode === 'source'" class="approvals__hint">
          <span>● TIP</span>
          <span>Click line numbers to comment · select text to comment a range</span>
          <span class="approvals__hint-right">{{ inlineComments.length }} inline comments · ⌘+⏎ submit</span>
        </div>
        <div v-if="detail.status === 'pending'" class="approvals__wait">
          ⏳ Waiting for {{ formatDuration(waitingTime) }}
          <template v-if="detail.paused_agents?.length">
            — {{ detail.paused_agents.length }} agent(s) paused
          </template>
        </div>

        <!-- Content viewer -->
        <div class="approvals__viewer">
          <div
            v-if="viewMode === 'preview'"
            class="approvals__preview"
            :style="{ maxHeight: isMobile ? '320px' : '420px' }"
          >
            <MarkdownRenderer
              :content="cmContent"
              :content-type="detail.content_format === 'markdown' ? 'markdown' : 'text'"
            />
          </div>
          <Codemirror
            v-else
            :model-value="cmContent"
            :extensions="cmExtensions"
            :style="{ maxHeight: isMobile ? '320px' : '420px' }"
            @ready="handleCmReady"
          />
        </div>

        <!-- Comment popover -->
        <div v-if="commentPopover" class="approvals__popover">
          <div class="approvals__popover-head">
            <span v-if="commentPopover.line">💬 Comment on Line {{ commentPopover.line }}</span>
            <span v-else-if="commentPopover.selection">
              💬 Comment on Lines {{ commentPopover.selection.start_line }}–{{ commentPopover.selection.end_line }}
            </span>
            <button class="btn btn-icon btn-ghost" @click="commentPopover = null">×</button>
          </div>
          <div v-if="commentPopover.selection?.selected_text" class="approvals__popover-quoted">
            <pre>{{ commentPopover.selection.selected_text }}</pre>
          </div>
          <textarea
            v-model="commentDraft"
            class="approvals__popover-input"
            placeholder="Write your comment…"
            rows="3"
            @keydown.ctrl.enter.prevent="submitComment"
            @keydown.meta.enter.prevent="submitComment"
          ></textarea>
          <div class="approvals__popover-actions">
            <button class="btn btn-secondary" @click="commentPopover = null">Cancel</button>
            <button class="btn btn-primary" :disabled="!commentDraft.trim()" @click="submitComment">
              Comment (⌘+⏎)
            </button>
          </div>
        </div>

        <!-- Comments -->
        <div v-if="inlineComments.length" class="approvals__comments">
          <div class="approvals__comments-head">
            <span>💬 Inline Comments</span>
            <span class="approvals__comments-count">{{ inlineComments.length }}</span>
            <span class="mono-label" style="font-size: 9.5px; color: var(--text-subtle); margin-left: auto;">
              Comments are sent with resolve · agents will see them
            </span>
          </div>
          <div v-for="c in inlineComments" :key="c.id" class="approval-comment">
            <template v-if="editingCommentId !== c.id">
              <div class="approval-comment__head">
                <span class="approval-comment__author">{{ c.author || 'user' }}</span>
                <span class="mono-label approval-comment__loc">{{ getCommentLocation(c) }}</span>
                <span class="mono-label approval-comment__time">{{ formatTimeAgo(c.created_at) }}</span>
                <div class="approval-comment__actions" v-if="isPending">
                  <button class="btn btn-icon btn-ghost" @click="startEditing(c)" title="Edit">✏️</button>
                  <button class="btn btn-icon btn-ghost approval-comment__delete" @click="deleteComment(c.id)" title="Delete">🗑️</button>
                </div>
              </div>
              <div v-if="c.selection?.selected_text" class="approval-comment__quoted">
                <pre>{{ c.selection.selected_text }}</pre>
              </div>
              <div class="approval-comment__body">{{ c.body }}</div>
            </template>

            <template v-else>
              <div class="approval-comment__head">
                <span class="approval-comment__author">Editing comment</span>
                <span class="mono-label approval-comment__loc">{{ getCommentLocation(c) }}</span>
              </div>
              <textarea
                v-model="editingBody"
                class="approvals__popover-input"
                rows="3"
                @keydown.ctrl.enter.prevent="saveEditing"
                @keydown.meta.enter.prevent="saveEditing"
              ></textarea>
              <div class="approvals__popover-actions">
                <button class="btn btn-secondary" @click="cancelEditing">Cancel</button>
                <button class="btn btn-primary" :disabled="!editingBody.trim()" @click="saveEditing">Save</button>
              </div>
            </template>
          </div>
        </div>

        <!-- Resolve bar -->
        <div v-if="isPending" class="approvals__resolve">
          <textarea
            v-model="resolveComment"
            class="approvals__resolve-input"
            placeholder="Optional comment for the agent (sent with approve/reject)…"
            rows="2"
          ></textarea>
          <div class="approvals__resolve-actions">
            <button class="btn btn-primary" :disabled="isResolving" @click="doResolve('approve')">
              ✓ Approve · agent continues
            </button>
            <button class="btn btn-secondary approvals__btn-reject" :disabled="isResolving" @click="doResolve('reject')">
              ✕ Reject · agent stops
            </button>
            <span class="approvals__resolve-status">
              <span class="chip-dot" style="background: var(--warning);"></span>
              <strong>{{ detail.agent_name }}</strong> blocked {{ formatDuration(waitingTime) }} — awaiting resolve
            </span>
          </div>
        </div>

        <!-- Already resolved -->
        <div v-else class="approvals__resolved">
          <div class="approvals__resolved-info">
            <div v-if="detail.user_comment" class="approvals__resolved-comment">{{ detail.user_comment }}</div>
            <div class="approvals__resolved-time">{{ formatTimeAgo(detail.resolved_at) }}</div>
          </div>
          <span
            class="chip"
            :class="{
              'chip-success': detail.status === 'approved',
              'chip-danger': detail.status === 'rejected',
            }"
          >
            <span class="chip-dot"></span>
            {{ detail.status === 'approved' ? 'Approved' : 'Rejected' }}
          </span>
        </div>
      </section>

      <section v-else class="approvals__detail approvals__detail--empty">
        <!-- Special empty state: Pending tab + 0 items → show the request_approval
             catalog so the page still teaches what each approval type means.
             Other empty filters (Approved/Rejected/All) just show a generic
             "nothing to review" placeholder — surfacing the catalog there would
             confuse the user (they're looking at history, not waiting on input). -->
        <div
          v-if="filter === 'pending' && filteredList.length === 0"
          class="approvals__empty-catalog"
        >
          <div class="approvals__empty-catalog-head">
            <span class="chip chip-success"><span class="chip-dot pulse-dot"></span> READY</span>
            <span class="approvals__empty-catalog-text">
              No pending approvals. When an agent calls
              <code class="approvals__inline-code">request_approval</code>,
              it will appear here and the agent will block until you resolve.
            </span>
          </div>
          <div class="approvals__type-grid">
            <div
              v-for="t in knownTypes"
              :key="t.key"
              class="approvals__type-card"
            >
              <div class="approvals__type-card-head">
                <span
                  class="approvals__type-icon"
                  :style="{ background: 'color-mix(in srgb, ' + (typeColors[t.key] || 'var(--primary)') + ' 18%, transparent)', color: typeColors[t.key] || 'var(--primary)' }"
                >📋</span>
                <div>
                  <div class="approvals__type-name">{{ typeLabels[t.key] || t.key }}</div>
                  <div class="mono-label" style="font-size: 9.5px; margin-top: 1px;">approval_type · {{ t.key }}</div>
                </div>
              </div>
              <p class="approvals__type-desc">{{ t.desc }}</p>
            </div>
          </div>
          <p class="approvals__history-hint">
            Switch to <strong>Approved</strong>, <strong>Rejected</strong>, or <strong>All</strong> in the queue tabs to browse past decisions.
          </p>
        </div>
        <div v-else class="approvals__detail-placeholder">
          <div class="mono-label">SELECT AN APPROVAL</div>
          <p v-if="filteredList.length">Pick an item from the queue to review.</p>
          <p v-else>No {{ filter === 'all' ? 'past' : filter }} approvals yet.</p>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.approvals {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  min-height: 0;
  color: var(--text);
}

/* Header */
.approvals__header {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border);
}
.approvals__heading {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.approvals__title {
  font-family: var(--font-display);
  font-size: 22px;
  letter-spacing: -0.02em;
  margin: 4px 0 0;
}
.approvals__title--mobile { font-size: 18px; }
.approvals__title-sub {
  color: var(--text-muted);
  font-size: 14px;
  font-weight: 400;
  margin-left: 10px;
}
.approvals__desc {
  font-size: 12.5px;
  color: var(--text-dim);
  max-width: 720px;
}
.approvals__inline-code {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--accent);
}

/* Overflow */
.approvals__overflow {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  background: rgba(245, 158, 11, 0.10);
  border: 1px solid rgba(245, 158, 11, 0.30);
  border-radius: var(--r-md);
  color: var(--warning);
  font-size: 12.5px;
  font-weight: 500;
}
.approvals__overflow-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--warning);
  box-shadow: 0 0 6px rgba(245, 158, 11, 0.5);
}

/* Empty catalog */
.approvals__empty-catalog {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 8px 0;
}
.approvals__empty-catalog-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 18px;
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
}
.approvals__empty-catalog-text {
  font-size: 13px;
  color: var(--text-dim);
}
.approvals__type-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}
.approvals__type-card {
  padding: 16px;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
}
.approvals__type-card-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.approvals__type-icon {
  width: 28px;
  height: 28px;
  border-radius: var(--r-md);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
}
.approvals__type-name { font-size: 13px; font-weight: 500; }
.approvals__type-desc {
  font-size: 12px;
  color: var(--text-dim);
  line-height: 1.55;
}

/* Main two-pane */
.approvals__main {
  flex: 1;
  display: grid;
  grid-template-columns: 340px 1fr;
  gap: 12px;
  min-height: 0;
}

/* Queue */
.approvals__queue {
  display: flex;
  flex-direction: column;
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  overflow: hidden;
  min-height: 0;
}
.approvals__queue-head {
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
}
.approvals__seg { width: 100%; }
.approvals__seg button { flex: 1; text-transform: capitalize; }
.approvals__queue-list {
  flex: 1;
  overflow-y: auto;
}
.approvals__queue-empty {
  padding: 30px 16px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
}

.approvals-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  padding: 12px 14px;
  background: transparent;
  border: 0;
  border-bottom: 1px solid var(--border);
  border-left: 3px solid transparent;
  text-align: left;
  cursor: pointer;
  transition: background 0.12s var(--ease-out);
}
.approvals-row:hover { background: var(--bg-2); }
.approvals-row--active {
  background: var(--primary-bg);
  border-left-color: var(--primary);
}

.approvals-row__top {
  display: flex;
  align-items: center;
  gap: 6px;
}
.approvals-row__type {
  padding: 2px 8px;
  height: 20px;
  border-radius: 999px;
  background: var(--bg-3);
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  border: 1px solid var(--border-strong);
  display: inline-flex;
  align-items: center;
}
.approvals-row__wait {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--warning);
}

.approvals-row__title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
  line-height: 1.4;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.approvals-row__meta {
  display: flex;
  gap: 6px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-muted);
}
.approvals-row__agent { color: var(--text-dim); }
.approvals-row__foot {
  display: flex;
  align-items: center;
  gap: 10px;
}
.approvals-row__comments {
  font-size: 11px;
  color: var(--accent);
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

/* Detail */
.approvals__detail {
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  padding: 16px;
  overflow-y: auto;
  min-height: 0;
}
.approvals__detail--empty {
  display: flex;
  align-items: stretch;
  justify-content: stretch;
}
.approvals__detail-placeholder {
  text-align: center;
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: center;
  justify-content: center;
  flex: 1;
}
/* "Switch to Approved / Rejected / All" cue — surfaced at the bottom of
   the empty catalog so users still discover history view even when the
   primary tab (Pending) is empty. Kept low-contrast so it doesn't
   compete with the catalog cards. */
.approvals__history-hint {
  margin-top: auto;
  padding: 10px 14px;
  font-size: 12px;
  color: var(--text-muted);
  background: var(--bg-2);
  border: 1px dashed var(--border-strong);
  border-radius: var(--r-md);
}
.approvals__history-hint strong {
  color: var(--text);
  font-weight: 600;
}

.approvals__detail-head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border);
}
.approvals__detail-info { flex: 1; min-width: 0; }
.approvals__detail-title { font-size: 16px; font-weight: 600; margin: 0 0 4px; }

.approvals__hint {
  padding: 8px 14px;
  background: var(--accent-bg);
  border: 1px solid rgba(34, 211, 238, 0.30);
  border-radius: var(--r-md);
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--accent);
  letter-spacing: 0.04em;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.approvals__hint-right { margin-left: auto; color: var(--text-muted); }

.approvals__wait {
  padding: 6px 12px;
  background: rgba(245, 158, 11, 0.08);
  border: 1px solid rgba(245, 158, 11, 0.25);
  border-radius: var(--r-md);
  font-size: 12.5px;
  color: var(--warning);
}

.approvals__viewer {
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  overflow: hidden;
}
.approvals__preview {
  padding: 14px 18px;
  overflow-y: auto;
  background: var(--bg-2);
}

/* Popover */
.approvals__popover {
  padding: 14px;
  background: var(--bg-2);
  border: 1px solid var(--primary);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-md);
}
.approvals__popover-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  font-size: 12.5px;
  font-weight: 500;
}
.approvals__popover-head button { margin-left: auto; }
.approvals__popover-quoted {
  padding: 6px 10px;
  background: var(--bg-1);
  border-left: 2px solid var(--accent);
  border-radius: var(--r-sm);
  margin-bottom: 8px;
  max-height: 90px;
  overflow-y: auto;
}
.approvals__popover-quoted pre {
  margin: 0;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  white-space: pre-wrap;
}
.approvals__popover-input {
  width: 100%;
  padding: 8px 10px;
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  color: var(--text);
  font-family: var(--font-body);
  font-size: 13px;
  resize: vertical;
  box-sizing: border-box;
  outline: none;
}
.approvals__popover-input:focus { border-color: var(--primary); }
.approvals__popover-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
}

/* Comments */
.approvals__comments {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.approvals__comments-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 500;
  margin-bottom: 4px;
}
.approvals__comments-count {
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--bg-3);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 10px;
}

.approval-comment {
  padding: 10px 12px;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
}
.approval-comment:hover { border-color: var(--border-strong); }
.approval-comment__head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}
.approval-comment__author { font-weight: 600; color: var(--primary-hover); }
.approval-comment__loc { color: var(--accent); }
.approval-comment__time { color: var(--text-muted); margin-left: auto; }
.approval-comment__actions { display: flex; gap: 2px; }
.approval-comment__quoted {
  background: var(--bg-1);
  border-left: 2px solid var(--text-subtle);
  padding: 4px 10px;
  margin-bottom: 6px;
  border-radius: var(--r-sm);
}
.approval-comment__quoted pre {
  margin: 0;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  white-space: pre-wrap;
}
.approval-comment__body { font-size: 13px; line-height: 1.55; }
.approval-comment__delete:hover { color: var(--danger); background: var(--danger-bg); }

/* Resolve bar */
.approvals__resolve {
  padding-top: 12px;
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.approvals__resolve-input {
  width: 100%;
  padding: 10px 14px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  color: var(--text);
  font-family: var(--font-body);
  font-size: 13px;
  resize: vertical;
  outline: none;
  box-sizing: border-box;
}
.approvals__resolve-input:focus { border-color: var(--primary); }
.approvals__resolve-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.approvals__btn-reject { color: var(--danger); border-color: rgba(239,68,68,0.30); }
.approvals__btn-reject:hover { background: var(--danger-bg); }
.approvals__resolve-status {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-muted);
}
.approvals__resolve-status strong { color: var(--warning); }

/* Resolved */
.approvals__resolved {
  padding-top: 12px;
  border-top: 1px solid var(--border);
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.approvals__resolved-info { flex: 1; min-width: 0; }
.approvals__resolved-comment { font-size: 13px; color: var(--text-dim); line-height: 1.55; }
.approvals__resolved-time { font-size: 11px; color: var(--text-muted); margin-top: 4px; }

/* Mobile */
@media (max-width: 768px) {
  .approvals__main {
    grid-template-columns: 1fr;
  }
  .approvals__queue--hidden { display: none; }
  .approvals__detail { max-height: none; }
  .approvals__type-grid { grid-template-columns: 1fr; }

  /* When the action row wraps, the resolve-status (using
     margin-left:auto) ended up alone on its own row right-aligned,
     visually disconnected from the buttons that triggered it. Remove
     the push on mobile so status sits inline with buttons or wraps
     naturally below them. */
  .approvals__resolve-status { margin-left: 0; width: 100%; }

  /* Long button labels ("✓ Approve · agent continues") overflowed the
     card on narrow phones. Stack full-width so neither button clips. */
  .approvals__resolve-actions { flex-direction: column; align-items: stretch; }
  .approvals__resolve-actions .btn { width: 100%; }
}
</style>
