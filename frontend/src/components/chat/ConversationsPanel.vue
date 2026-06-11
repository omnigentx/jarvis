<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useChatStore } from '../../stores/chat'
import ConfirmModal from '../ConfirmModal.vue'
import { useToast } from '../../composables/useToast'
import { parseYoutubeTags } from '../../utils/youtubeTags'

/**
 * ConversationsPanel — left rail with search, new-chat, the conversation list,
 * a multi-select bulk-delete mode, and infinite scroll.
 *
 * The list is server-scoped to chatStore.activeAgentName (switching agents
 * reloads it), so this component renders chatStore.sortedConversations as-is.
 * Infinite scroll pages the rest in via an IntersectionObserver on a sentinel.
 */
const toast = useToast()
const chatStore = useChatStore()
const searchQuery = ref('')

const props = defineProps({
  showClose: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'select'])

// --- Single-delete (per-row trash, non-select mode) ---
const showDeleteModal = ref(false)
const deleteTarget = ref(null)
const isDeleting = ref(false)
const deleteError = ref('')

// --- Multi-select bulk delete ---
const selectMode = ref(false)
const selectedIds = ref(new Set())
const showBulkDeleteModal = ref(false)
const isBulkDeleting = ref(false)
const bulkDeleteError = ref('')
const selectedCount = computed(() => selectedIds.value.size)

const filtered = computed(() => {
  const q = searchQuery.value.toLowerCase()
  if (!q) return chatStore.sortedConversations
  return chatStore.sortedConversations.filter(c =>
    c.title.toLowerCase().includes(q)
  )
})

function formatTime(ts) {
  if (!ts) return ''
  const diff = Date.now() - ts
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.floor(hours / 24)
  return `${days}d`
}

function getLastMessage(conv) {
  const msgs = conv.messages
  if (!msgs.length) return ''
  const last = msgs[msgs.length - 1]
  const prefix = last.role === 'assistant' ? `${conv.agentName}: ` : ''
  const cleaned = parseYoutubeTags(last.content || '').text
  const text = cleaned || '...'
  return prefix + (text.length > 60 ? text.slice(0, 60) + '...' : text)
}

// --- Select mode ---
function toggleSelectMode() {
  selectMode.value = !selectMode.value
  if (!selectMode.value) selectedIds.value = new Set()
}

function toggleSelected(id) {
  const next = new Set(selectedIds.value)
  next.has(id) ? next.delete(id) : next.add(id)
  selectedIds.value = next
}

function onRowClick(conv) {
  if (selectMode.value) {
    toggleSelected(conv.id)
    return
  }
  chatStore.selectConversation(conv.id)
  emit('select')
}

// --- Single delete ---
function confirmDelete(id, title) {
  deleteTarget.value = { id, title }
  deleteError.value = ''
  showDeleteModal.value = true
}

async function handleDeleteConfirm() {
  if (!deleteTarget.value) return
  isDeleting.value = true
  deleteError.value = ''
  const title = deleteTarget.value.title
  try {
    await chatStore.deleteConversation(deleteTarget.value.id)
    showDeleteModal.value = false
    deleteTarget.value = null
    toast.success('Conversation deleted', { description: `"${title}" has been removed` })
  } catch (e) {
    deleteError.value = e.message || 'Failed to delete'
    toast.error('Failed to delete conversation', { description: e.message })
  } finally {
    isDeleting.value = false
  }
}

function handleDeleteCancel() {
  showDeleteModal.value = false
  deleteTarget.value = null
  deleteError.value = ''
}

// --- Bulk delete ---
function requestBulkDelete() {
  if (!selectedCount.value) return
  bulkDeleteError.value = ''
  showBulkDeleteModal.value = true
}

async function handleBulkDeleteConfirm() {
  const ids = [...selectedIds.value]
  if (!ids.length) return
  isBulkDeleting.value = true
  bulkDeleteError.value = ''
  try {
    await chatStore.deleteConversations(ids)
    showBulkDeleteModal.value = false
    selectMode.value = false
    selectedIds.value = new Set()
    toast.success(`Deleted ${ids.length} conversation${ids.length !== 1 ? 's' : ''}`)
  } catch (e) {
    bulkDeleteError.value = e.message || 'Failed to delete'
    toast.error('Bulk delete failed', { description: e.message })
  } finally {
    isBulkDeleting.value = false
  }
}

function handleBulkDeleteCancel() {
  showBulkDeleteModal.value = false
  bulkDeleteError.value = ''
}

// --- Infinite scroll ---
// Observe a sentinel at the list bottom; when it enters view, ask the store
// for the next page. The store self-guards (no-op when already loading or no
// more pages), so a chatty observer is harmless.
const listEl = ref(null)
const sentinel = ref(null)
let observer = null

onMounted(() => {
  observer = new IntersectionObserver(
    (entries) => {
      if (entries.some(e => e.isIntersecting)) chatStore.loadMoreConversations()
    },
    { root: listEl.value, rootMargin: '160px' },
  )
  if (sentinel.value) observer.observe(sentinel.value)
})

// The sentinel is v-if'd on convHasMore, so (re)observe whenever it mounts.
watch(sentinel, (el, prev) => {
  if (!observer) return
  if (prev) observer.unobserve(prev)
  if (el) observer.observe(el)
})

onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <aside
    data-testid="conversations-panel"
    class="conv-rail"
    :class="{ 'conv-rail-mobile': showClose }"
  >
    <div class="conv-head">
      <div class="conv-head-row">
        <span class="conv-eyebrow">CONVERSATIONS</span>
        <button
          class="conv-icon-btn"
          :class="{ active: selectMode }"
          data-testid="conv-select-toggle"
          @click="toggleSelectMode"
          :title="selectMode ? 'Exit select mode' : 'Select conversations'"
        >
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
            <rect x="2" y="2" width="12" height="12" rx="2.5" stroke="currentColor" stroke-width="1.4"/>
            <path v-if="selectMode" d="M5 8.2l2 2 4-4.4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </button>
        <button
          v-if="!selectMode"
          class="conv-icon-btn"
          @click="chatStore.createConversation(chatStore.activeAgentName)"
          title="New conversation"
        >
          <svg width="13" height="13" viewBox="0 0 14 14" fill="none">
            <line x1="7" y1="2" x2="7" y2="12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            <line x1="2" y1="7" x2="12" y2="7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
          </svg>
        </button>
        <button v-if="showClose" class="conv-icon-btn" @click="emit('close')" title="Close">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <line x1="3" y1="3" x2="11" y2="11" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            <line x1="11" y1="3" x2="3" y2="11" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
          </svg>
        </button>
      </div>

      <div class="conv-search">
        <svg width="13" height="13" viewBox="0 0 14 14" fill="none">
          <circle cx="6" cy="6" r="5" stroke="currentColor" stroke-width="1.5"/>
          <line x1="10" y1="10" x2="13" y2="13" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
        </svg>
        <input
          v-model="searchQuery"
          type="text"
          placeholder="Search..."
          class="conv-search-input"
        />
      </div>

      <!-- Bulk action bar (select mode) -->
      <div v-if="selectMode" class="conv-bulk-bar">
        <span class="conv-bulk-count">{{ selectedCount }} selected</span>
        <button
          class="conv-bulk-delete"
          data-testid="conv-bulk-delete"
          :disabled="!selectedCount"
          @click="requestBulkDelete"
        >
          Delete
        </button>
        <button class="conv-bulk-cancel" @click="toggleSelectMode">Cancel</button>
      </div>
    </div>

    <div ref="listEl" class="conv-list">
      <div
        v-for="conv in filtered"
        :key="conv.id"
        class="conv-item"
        :class="{
          active: !selectMode && conv.id === chatStore.activeConversationId,
          selected: selectMode && selectedIds.has(conv.id),
        }"
        @click="onRowClick(conv)"
      >
        <span
          v-if="selectMode"
          class="conv-check"
          :class="{ on: selectedIds.has(conv.id) }"
          aria-hidden="true"
        >
          <svg v-if="selectedIds.has(conv.id)" width="11" height="11" viewBox="0 0 16 16" fill="none">
            <path d="M3.5 8.2l2.7 2.7L12.5 5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </span>
        <div class="conv-item-body">
          <div class="conv-item-top">
            <span class="conv-item-title">{{ conv.title }}</span>
            <div class="conv-item-meta">
              <span class="conv-item-time">{{ formatTime(conv.updatedAt) }}</span>
              <button
                v-if="!selectMode"
                class="conv-item-delete"
                @click.stop="confirmDelete(conv.id, conv.title)"
                title="Delete"
              >
                <svg width="13" height="13" viewBox="0 0 14 14" fill="none">
                  <path d="M3 4h8M5.5 4V3a1 1 0 011-1h1a1 1 0 011 1v1M4.5 4v7a1 1 0 001 1h3a1 1 0 001-1V4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
              </button>
            </div>
          </div>
          <div class="conv-item-preview">
            {{ getLastMessage(conv) || 'No messages yet' }}
          </div>
        </div>
      </div>

      <div v-if="!filtered.length" class="conv-empty">
        {{ searchQuery ? 'No matching conversations' : 'Start a new conversation' }}
      </div>

      <!-- Infinite-scroll sentinel + loading footer -->
      <div
        v-if="chatStore.convHasMore && !searchQuery"
        ref="sentinel"
        data-testid="conv-sentinel"
        class="conv-sentinel"
      >
        <span v-if="chatStore.isLoadingMoreConversations" class="conv-loading">Loading…</span>
      </div>
    </div>

    <ConfirmModal
      :visible="showDeleteModal"
      title="Delete Conversation"
      :message="`Are you sure you want to delete &quot;${deleteTarget?.title || ''}&quot;? This action cannot be undone.`"
      confirm-text="Delete"
      variant="danger"
      :loading="isDeleting"
      :error="deleteError"
      @confirm="handleDeleteConfirm"
      @cancel="handleDeleteCancel"
    />

    <ConfirmModal
      :visible="showBulkDeleteModal"
      title="Delete Conversations"
      :message="`Delete ${selectedCount} conversation${selectedCount !== 1 ? 's' : ''}? This action cannot be undone.`"
      confirm-text="Delete"
      variant="danger"
      :loading="isBulkDeleting"
      :error="bulkDeleteError"
      @confirm="handleBulkDeleteConfirm"
      @cancel="handleBulkDeleteCancel"
    />
  </aside>
</template>

<style scoped>
.conv-rail {
  width: 260px;
  flex-shrink: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--bg-1);
  border-right: 1px solid var(--border);
  overflow: hidden;
}
.conv-rail-mobile { width: 100%; border-right: 0; }

.conv-head {
  padding: 14px 14px 10px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.conv-head-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.conv-eyebrow {
  flex: 1;
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.16em;
  color: var(--text-muted);
}
.conv-icon-btn {
  width: 26px; height: 26px;
  display: flex; align-items: center; justify-content: center;
  background: transparent;
  border: 0;
  border-radius: var(--r-sm);
  color: var(--text-dim);
  cursor: pointer;
}
.conv-icon-btn:hover { background: var(--bg-3); color: var(--text); }
.conv-icon-btn.active { background: var(--primary-bg); color: var(--primary); }

.conv-search {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 10px;
  height: 30px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  color: var(--text-muted);
}
.conv-search-input {
  flex: 1;
  background: transparent;
  border: 0;
  outline: 0;
  font: inherit;
  font-size: 12px;
  color: var(--text);
}
.conv-search-input::placeholder { color: var(--text-muted); }

/* Bulk action bar */
.conv-bulk-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
}
.conv-bulk-count {
  flex: 1;
  font-size: 11.5px;
  color: var(--text-dim);
}
.conv-bulk-delete, .conv-bulk-cancel {
  font: inherit;
  font-size: 11.5px;
  font-weight: 500;
  padding: 4px 12px;
  border-radius: var(--r-sm);
  cursor: pointer;
  border: 1px solid var(--border-strong);
  background: var(--bg-2);
  color: var(--text);
}
.conv-bulk-delete {
  border-color: transparent;
  background: var(--danger);
  color: #fff;
}
.conv-bulk-delete:disabled { opacity: 0.45; cursor: not-allowed; }
.conv-bulk-cancel:hover { background: var(--bg-3); }

.conv-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}
.conv-item {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border-radius: var(--r-md);
  border-left: 2px solid transparent;
  cursor: pointer;
  margin-bottom: 2px;
  transition: background 0.15s var(--ease-out);
}
.conv-item:hover { background: var(--bg-2); }
.conv-item.active {
  background: var(--primary-bg);
  border-left-color: var(--primary);
}
.conv-item.selected { background: var(--primary-bg); }

.conv-check {
  flex-shrink: 0;
  width: 16px; height: 16px;
  margin-top: 1px;
  border: 1.4px solid var(--border-strong);
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  transition: background 0.12s var(--ease-out), border-color 0.12s var(--ease-out);
}
.conv-check.on {
  background: var(--primary);
  border-color: var(--primary);
}

.conv-item-body { flex: 1; min-width: 0; }
.conv-item-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.conv-item-title {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conv-item-meta {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 4px;
}
.conv-item-time {
  font-family: var(--font-mono);
  font-size: 9.5px;
  color: var(--text-subtle);
  letter-spacing: 0.06em;
}
.conv-item-delete {
  display: none;
  align-items: center;
  justify-content: center;
  width: 20px; height: 20px;
  border: 0;
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--text-subtle);
  cursor: pointer;
}
.conv-item:hover .conv-item-delete { display: flex; }
.conv-item-delete:hover { background: var(--danger-bg); color: var(--danger); }
.conv-item-preview {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conv-empty {
  padding: 40px 16px;
  font-size: 12px;
  color: var(--text-muted);
  text-align: center;
}
.conv-sentinel {
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.conv-loading {
  font-size: 11px;
  color: var(--text-muted);
  font-family: var(--font-mono);
}
</style>
