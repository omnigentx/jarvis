<script setup>
import { ref, computed } from 'vue'
import { useChatStore } from '../../stores/chat'
import ConfirmModal from '../ConfirmModal.vue'
import { useToast } from '../../composables/useToast'
import { parseYoutubeTags } from '../../utils/youtubeTags'

/**
 * ConversationsPanel — left rail with search, new-chat, and conversation list.
 * Restyled to the new design tokens (var(--bg-1) sidebar, primary border for
 * active item, mono labels). Logic unchanged.
 */
const toast = useToast()
const chatStore = useChatStore()
const searchQuery = ref('')

const props = defineProps({
  showClose: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'select'])

const showDeleteModal = ref(false)
const deleteTarget = ref(null)
const isDeleting = ref(false)
const deleteError = ref('')

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
          class="conv-new-btn"
          @click="chatStore.createConversation(chatStore.activeAgentName)"
          title="New conversation"
        >
          <svg width="13" height="13" viewBox="0 0 14 14" fill="none">
            <line x1="7" y1="2" x2="7" y2="12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            <line x1="2" y1="7" x2="12" y2="7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
          </svg>
        </button>
        <button v-if="showClose" class="conv-close-btn" @click="emit('close')">
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
    </div>

    <div class="conv-list">
      <div
        v-for="conv in filtered"
        :key="conv.id"
        class="conv-item"
        :class="{ active: conv.id === chatStore.activeConversationId }"
        @click="chatStore.selectConversation(conv.id); emit('select')"
      >
        <div class="conv-item-top">
          <span class="conv-item-title">{{ conv.title }}</span>
          <div class="conv-item-meta">
            <span class="conv-item-time">{{ formatTime(conv.updatedAt) }}</span>
            <button class="conv-item-delete" @click.stop="confirmDelete(conv.id, conv.title)" title="Delete">
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

      <div v-if="!filtered.length" class="conv-empty">
        {{ searchQuery ? 'No matching conversations' : 'Start a new conversation' }}
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
.conv-new-btn, .conv-close-btn {
  width: 26px; height: 26px;
  display: flex; align-items: center; justify-content: center;
  background: transparent;
  border: 0;
  border-radius: var(--r-sm);
  color: var(--text-dim);
  cursor: pointer;
}
.conv-new-btn:hover, .conv-close-btn:hover { background: var(--bg-3); color: var(--text); }

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

.conv-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}
.conv-item {
  position: relative;
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
</style>
