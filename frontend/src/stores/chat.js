import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import { apiFetch } from '../api'

const META_STORAGE_KEY = 'jarvis_chat_meta'
const TTS_STORAGE_KEY = 'jarvis_tts_enabled'

export const useChatStore = defineStore('chat', () => {
  // --- State ---
  const conversations = ref([])
  const activeConversationId = ref(null)
  const activeAgentName = ref(null)
  const isStreaming = ref(false)
  const isLoadingHistory = ref(false)
  const ttsEnabled = ref(localStorage.getItem(TTS_STORAGE_KEY) === 'true')
  const ttsPlaying = ref(false)
  const _ttsAudioRef = ref(null) // internal ref, set by ChatView

  // --- Computed ---
  const activeConversation = computed(() =>
    conversations.value.find(c => c.id === activeConversationId.value) || null
  )

  const activeMessages = computed(() =>
    activeConversation.value?.messages || []
  )

  const sortedConversations = computed(() =>
    [...conversations.value].sort((a, b) => b.updatedAt - a.updatedAt)
  )

  // --- Metadata Persistence (localStorage) ---
  // Only stores: activeConversationId, activeAgentName — no messages
  function _loadMeta() {
    try {
      const raw = localStorage.getItem(META_STORAGE_KEY)
      if (raw) {
        const meta = JSON.parse(raw)
        activeConversationId.value = meta.activeConversationId || null
        activeAgentName.value = meta.activeAgentName || null
      }
    } catch (e) {
      console.warn('[ChatStore] Failed to load metadata:', e)
    }
  }

  function _saveMeta() {
    try {
      localStorage.setItem(META_STORAGE_KEY, JSON.stringify({
        activeConversationId: activeConversationId.value,
        activeAgentName: activeAgentName.value,
      }))
    } catch (e) {
      console.warn('[ChatStore] Failed to save metadata:', e)
    }
  }

  // --- Backend API ---

  /**
   * Fetch conversation list from backend.
   * Called on page load to populate sidebar.
   */
  async function fetchConversations() {
    try {
      const data = await apiFetch('/api/conversations')
      if (!Array.isArray(data)) return

      conversations.value = data.map(c => ({
        id: c.id,
        title: c.title,
        // Backend stamps the primary agent per session (see PRIMARY_AGENT_META_KEY
        // in services/session_service.py). Fall back to Jarvis for any legacy
        // session where the backend couldn't resolve one.
        agentName: c.agent_name || 'Jarvis',
        backendConversationId: c.id,
        messages: [], // Loaded on-demand via fetchHistory
        createdAt: c.created_at * 1000,
        updatedAt: c.updated_at * 1000,
        messageCount: c.message_count,
      }))

      // Auto-select previously active conversation
      if (activeConversationId.value) {
        const exists = conversations.value.find(c => c.id === activeConversationId.value)
        if (exists && exists.messages.length === 0) {
          await fetchHistory(activeConversationId.value, exists.agentName)
        }
      }
    } catch (e) {
      console.warn('[ChatStore] Failed to fetch conversations:', e)
    }
  }

  /**
   * Fetch message history from backend for a specific conversation.
   * Returns enriched messages including tool call metadata.
   *
   * ``agentName`` is optional. When supplied, the request routes to
   * ``history_{agentName}.json`` on the backend so sub-agent conversations
   * (IoT, Music, …) render correctly. When omitted, the backend falls back
   * to the session's primary agent.
   */
  async function fetchHistory(conversationId, agentName = null) {
    if (!conversationId) return
    const conv = conversations.value.find(c => c.id === conversationId)
    if (!conv) return

    isLoadingHistory.value = true
    try {
      const params = new URLSearchParams({ conversation_id: conversationId })
      const effectiveAgent = agentName || conv.agentName
      if (effectiveAgent) params.set('agent_name', effectiveAgent)
      const history = await apiFetch(`/api/history?${params.toString()}`)
      if (!Array.isArray(history)) return

      conv.messages = history.map((msg, idx) => ({
        id: `hist-${conversationId}-${idx}`,
        role: msg.role,
        content: msg.content,
        timestamp: conv.createdAt + idx * 1000, // Approximate ordering
        isStreaming: false,
        // Each backend entry is already a paired (call, result) — emit it in
        // groupToolCalls' "already grouped" shape: `duration` as the formatted
        // string it renders from, `resultPreview` in the camelCase key it
        // reads from, and *no* isResult flag (which, when true, would make
        // groupToolCalls collapse all same-named tools into one bubble on
        // reload — that's why 13 CrawlStoriesAgent invocations used to render
        // as "1 tool used" after SSE completed).
        toolCalls: (msg.tool_calls || []).map((tc, i) => ({
          id: `tc-${conversationId}-${idx}-${i}`,
          tool: tc.tool,
          args: tc.args,
          status: tc.status || 'done',
          duration: tc.duration_ms != null ? `${(tc.duration_ms / 1000).toFixed(1)}s` : null,
          resultPreview: tc.result_preview || null,
          timestamp: conv.createdAt + idx * 1000,
        })),
      }))
    } catch (e) {
      console.warn('[ChatStore] Failed to fetch history:', e)
    } finally {
      isLoadingHistory.value = false
    }
  }

  // --- Actions ---
  function createConversation(agentName) {
    const id = crypto.randomUUID()
    const conv = {
      id,
      title: 'New Conversation',
      agentName: agentName || activeAgentName.value || 'Jarvis',
      backendConversationId: null, // Set by backend on first response
      messages: [],
      createdAt: Date.now(),
      updatedAt: Date.now(),
    }
    conversations.value.unshift(conv)
    activeConversationId.value = id
    _saveMeta()
    return conv
  }

  async function selectConversation(id) {
    activeConversationId.value = id
    const conv = conversations.value.find(c => c.id === id)
    if (conv && conv.agentName) {
      activeAgentName.value = conv.agentName
    }
    _saveMeta()

    // Load history from backend if not already loaded
    if (conv && conv.backendConversationId && conv.messages.length === 0) {
      await fetchHistory(conv.backendConversationId, conv.agentName)
    }
  }

  async function deleteConversation(id) {
    const conv = conversations.value.find(c => c.id === id)
    const backendId = conv?.backendConversationId

    if (backendId) {
      try {
        await apiFetch(`/api/conversations/${backendId}`, { method: 'DELETE' })
      } catch (e) {
        console.warn('[ChatStore] Backend delete failed:', e)
      }
    }

    conversations.value = conversations.value.filter(c => c.id !== id)
    if (activeConversationId.value === id) {
      activeConversationId.value = conversations.value[0]?.id || null
    }
    _saveMeta()
  }

  function setActiveAgent(name) {
    activeAgentName.value = name
    const conv = activeConversation.value
    if (conv && conv.agentName !== name) {
      activeConversationId.value = null
    }
    _saveMeta()
  }

  function toggleTts() {
    if (ttsPlaying.value) {
      stopTts()
    }
    ttsEnabled.value = !ttsEnabled.value
    localStorage.setItem(TTS_STORAGE_KEY, String(ttsEnabled.value))
  }

  function stopTts() {
    if (_ttsAudioRef.value) {
      _ttsAudioRef.value.pause()
      _ttsAudioRef.value.currentTime = 0
    }
    ttsPlaying.value = false
  }

  function setTtsAudioRef(el) {
    _ttsAudioRef.value = el
  }

  /**
   * Add a user message to the active conversation (in-memory only).
   */
  function addUserMessage(content) {
    const conv = activeConversation.value
    if (!conv) return
    conv.messages.push({
      id: crypto.randomUUID(),
      role: 'user',
      content,
      timestamp: Date.now(),
    })
    // Update title from first user message
    if (conv.messages.filter(m => m.role === 'user').length === 1) {
      conv.title = content.length > 40 ? content.slice(0, 40) + '...' : content
    }
    conv.updatedAt = Date.now()
  }

  /**
   * Add a streaming agent message placeholder (in-memory only).
   */
  function addAgentMessagePlaceholder() {
    const conv = activeConversation.value
    if (!conv) return null
    const id = crypto.randomUUID()
    conv.messages.push({
      id,
      role: 'assistant',
      content: '',
      timestamp: Date.now(),
      isStreaming: true,
      toolCalls: [],
    })
    return id
  }

  /**
   * Push a tool call event into the current streaming message (in-memory only).
   */
  function pushToolCall(messageId, toolCall) {
    const conv = activeConversation.value
    if (!conv) return
    const msg = conv.messages.find(m => m.id === messageId)
    if (!msg) return
    if (!msg.toolCalls) msg.toolCalls = []
    msg.toolCalls.push({
      id: crypto.randomUUID(),
      ...toolCall,
      timestamp: Date.now(),
    })
  }

  /**
   * Finalize the streaming message with the agent's response (in-memory only).
   * Saves metadata (backendConversationId) for session tracking.
   */
  function finalizeAgentMessage(messageId, content, meta = {}) {
    const conv = activeConversation.value
    if (!conv) return
    const msg = conv.messages.find(m => m.id === messageId)
    if (!msg) return
    msg.content = content
    msg.isStreaming = false
    msg.meta = meta
    conv.updatedAt = Date.now()
    if (meta.conversation_id) {
      conv.backendConversationId = meta.conversation_id
      // Update the ID to match backend for consistent lookups
      if (conv.id !== meta.conversation_id) {
        const oldId = conv.id
        conv.id = meta.conversation_id
        if (activeConversationId.value === oldId) {
          activeConversationId.value = meta.conversation_id
        }
      }
    }
    _saveMeta()
  }

  /**
   * Remove a (typically streaming-placeholder) message entirely.
   *
   * Used when an agent turn ends with no useful output and we'd rather not
   * leave a "(interrupted)"/"(no response)" bubble cluttering the thread —
   * e.g. user barge-in cancels the bot before it speaks, or the agent
   * returns an empty reply. Errors that the user should *see* still go
   * through ``setMessageError`` instead.
   */
  function removeMessage(messageId) {
    const conv = activeConversation.value
    if (!conv) return
    const idx = conv.messages.findIndex(m => m.id === messageId)
    if (idx === -1) return
    conv.messages.splice(idx, 1)
    conv.updatedAt = Date.now()
  }

  /**
   * Mark a streaming placeholder as INTERRUPTED — the LLM was cancelled
   * mid-generation (typically by a user barge-in over voice). Differs
   * from ``setMessageError`` because no error happened: the user just
   * cut the bot off. Differs from ``removeMessage`` because we keep
   * the bubble visible so the user can see *which* turn was cut.
   *
   * Per-message flag (instead of a global ``wasInterrupted`` ref) so
   * the chip lands on the right bubble — a global ref would also tag
   * any already-finalised previous message every time TTS gets cut
   * post-LLM (the false-positive bug for Case B).
   */
  function markMessageInterrupted(messageId) {
    const conv = activeConversation.value
    if (!conv) return
    const msg = conv.messages.find(m => m.id === messageId)
    if (!msg) return
    msg.isStreaming = false
    msg.isInterrupted = true
    conv.updatedAt = Date.now()
  }

  /**
   * Mark a streaming message as errored (in-memory only).
   */
  function setMessageError(messageId, errorText) {
    const conv = activeConversation.value
    if (!conv) return
    const msg = conv.messages.find(m => m.id === messageId)
    if (!msg) return
    msg.content = errorText
    msg.isStreaming = false
    msg.isError = true
    conv.updatedAt = Date.now()
  }

  // --- Cleanup legacy localStorage ---
  // Remove old message storage if present
  try {
    localStorage.removeItem('jarvis_chat_conversations')
  } catch (_) {}

  // Initialize metadata from localStorage
  _loadMeta()

  return {
    conversations,
    activeConversationId,
    activeAgentName,
    isStreaming,
    isLoadingHistory,
    ttsEnabled,
    ttsPlaying,
    activeConversation,
    activeMessages,
    sortedConversations,
    fetchConversations,
    fetchHistory,
    createConversation,
    selectConversation,
    deleteConversation,
    setActiveAgent,
    toggleTts,
    stopTts,
    setTtsAudioRef,
    addUserMessage,
    addAgentMessagePlaceholder,
    pushToolCall,
    finalizeAgentMessage,
    removeMessage,
    setMessageError,
    markMessageInterrupted,
  }
})
