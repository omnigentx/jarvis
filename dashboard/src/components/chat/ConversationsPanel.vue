<script setup>
import { ref, computed } from 'vue'
import { useChatStore } from '../../stores/chat'
import ConfirmModal from '../ConfirmModal.vue'
import { useToast } from '../../composables/useToast'
import { parseYoutubeTags } from '../../utils/youtubeTags'

const toast = useToast()
const chatStore = useChatStore()
const searchQuery = ref('')

const props = defineProps({
  showClose: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'select'])

// Delete modal state
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
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

function getLastMessage(conv) {
  const msgs = conv.messages
  if (!msgs.length) return ''
  const last = msgs[msgs.length - 1]
  const prefix = last.role === 'assistant' ? `${conv.agentName}: ` : ''
  // Strip [[[PLAY: id]]] tags so the sidebar preview never shows raw
  // protocol markup. Same util as the chat bubble renderer.
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
    toast.success('Conversation deleted', {
      description: `"${title}" has been removed`,
    })
  } catch (e) {
    deleteError.value = e.message || 'Failed to delete'
    toast.error('Failed to delete conversation', {
      description: e.message,
    })
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
  <div
    data-testid="conversations-panel"
    class="flex flex-col shrink-0 h-full overflow-hidden"
    :style="{
      width: props.showClose ? '100%' : '280px',
      background: 'var(--bg-sidebar)',
      borderRight: props.showClose ? 'none' : '1px solid var(--border-sidebar)',
    }"
  >
    <!-- Title -->
    <div class="flex items-center justify-between" style="padding: 20px 16px 0;">
      <div style="font-size: 16px; font-weight: 700; color: var(--text-primary); line-height: 19px;">
        Conversations
      </div>
      <!-- Close button (mobile overlay) -->
      <button
        v-if="props.showClose"
        class="flex items-center justify-center cursor-pointer"
        :style="{
          width: '28px',
          height: '28px',
          borderRadius: '8px',
          background: 'var(--bg-card)',
          border: 'none',
        }"
        @click="emit('close')"
      >
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
          <line x1="3" y1="3" x2="11" y2="11" stroke="var(--text-sub)" stroke-width="1.5" stroke-linecap="round"/>
          <line x1="11" y1="3" x2="3" y2="11" stroke="var(--text-sub)" stroke-width="1.5" stroke-linecap="round"/>
        </svg>
      </button>
    </div>

    <!-- Search -->
    <div style="padding: 12px 16px 0;">
      <div
        class="flex items-center"
        :style="{
          height: '36px',
          background: 'var(--bg-input)',
          border: '1px solid var(--border-input)',
          borderRadius: '8px',
          padding: '0 12px',
          gap: '8px',
        }"
      >
        <!-- Search icon -->
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
          <circle cx="6" cy="6" r="5" stroke="#555872" stroke-width="1.5"/>
          <line x1="10" y1="10" x2="13" y2="13" stroke="#555872" stroke-width="1.5" stroke-linecap="round"/>
        </svg>
        <input
          v-model="searchQuery"
          type="text"
          placeholder="Search conversations..."
          class="flex-1 bg-transparent text-[12px] outline-none"
          style="color: var(--text-primary); font-family: Inter;"
          :style="{ '::placeholder': { color: 'var(--text-sub)' } }"
        />
      </div>
    </div>

    <!-- New Conversation Button -->
    <div style="padding: 8px 16px 0;">
      <button
        class="flex items-center justify-center w-full transition-colors"
        :style="{
          height: '36px',
          background: 'var(--bg-button)',
          borderRadius: '8px',
          border: 'none',
          cursor: 'pointer',
          gap: '6px',
        }"
        @click="chatStore.createConversation(chatStore.activeAgentName)"
      >
        <!-- Plus icon -->
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
          <line x1="7" y1="2" x2="7" y2="12" stroke="#8b8fa3" stroke-width="1.5" stroke-linecap="round"/>
          <line x1="2" y1="7" x2="12" y2="7" stroke="#8b8fa3" stroke-width="1.5" stroke-linecap="round"/>
        </svg>
        <span style="font-size: 12px; font-weight: 500; color: var(--text-nav);">
          New Conversation
        </span>
      </button>
    </div>

    <!-- Conversation List -->
    <div class="flex-1 overflow-y-auto" style="padding: 8px 0;">
      <div
        v-for="conv in filtered"
        :key="conv.id"
        class="conv-item cursor-pointer transition-colors"
        :class="{ 'conv-active': conv.id === chatStore.activeConversationId }"
        :style="{
          padding: '10px 16px',
          margin: '0 8px',
          borderRadius: '10px',
          background: conv.id === chatStore.activeConversationId ? 'var(--bg-active)' : 'transparent',
          border: conv.id === chatStore.activeConversationId ? '1px solid var(--border-active)' : '1px solid transparent',
          position: 'relative',
        }"
        @click="chatStore.selectConversation(conv.id); emit('select')"
      >
        <!-- Title + Time + Delete -->
        <div class="flex items-center justify-between">
          <div
            :style="{
              fontSize: '13px',
              fontWeight: conv.id === chatStore.activeConversationId ? '600' : '500',
              color: conv.id === chatStore.activeConversationId ? 'var(--text-primary)' : 'var(--text-secondary)',
              lineHeight: '16px',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              flex: '1',
              minWidth: '0',
            }"
          >
            {{ conv.title }}
          </div>
          <div class="flex items-center gap-1" style="flex-shrink: 0;">
            <div
              style="font-size: 10px; font-weight: 400; color: var(--text-sub); line-height: 12px;"
            >
              {{ formatTime(conv.updatedAt) }}
            </div>
            <!-- Delete button (visible on hover) -->
            <button
              class="conv-delete-btn"
              title="Delete conversation"
              @click.stop="confirmDelete(conv.id, conv.title)"
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path d="M3 4h8M5.5 4V3a1 1 0 011-1h1a1 1 0 011 1v1M4.5 4v7a1 1 0 001 1h3a1 1 0 001-1V4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>
          </div>
        </div>

        <!-- Preview -->
        <div
          style="
            font-size: 11px;
            font-weight: 400;
            color: var(--text-nav);
            line-height: 13px;
            margin-top: 6px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
          "
        >
          {{ getLastMessage(conv) || 'No messages yet' }}
        </div>
      </div>

      <!-- Empty state -->
      <div
        v-if="!filtered.length"
        class="flex items-center justify-center"
        style="padding: 40px 16px; color: var(--text-sub); font-size: 12px; text-align: center;"
      >
        {{ searchQuery ? 'No matching conversations' : 'Start a new conversation' }}
      </div>
    </div>
  </div>

  <!-- Delete Confirmation Modal -->
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
</template>

<style scoped>
.conv-delete-btn {
  display: none;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-sub);
  cursor: pointer;
  padding: 0;
  transition: all 0.15s ease;
}

.conv-item:hover .conv-delete-btn {
  display: flex;
}

.conv-delete-btn:hover {
  background: rgba(239, 68, 68, 0.15);
  color: var(--error);
}
</style>
