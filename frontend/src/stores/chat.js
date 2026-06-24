import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import { apiFetch } from '../api.js'
import { useAudioPlayerStore } from './audioPlayer.js'

const META_STORAGE_KEY = 'jarvis_chat_meta'
const TTS_STORAGE_KEY = 'jarvis_tts_enabled'
// Page size for the conversation sidebar's infinite scroll. The sidebar is
// scoped to one agent at a time, so 20 is plenty per fetch.
const CONV_PAGE_SIZE = 20

export const useChatStore = defineStore('chat', () => {
  // --- State ---
  const conversations = ref([])
  const activeConversationId = ref(null)
  const activeAgentName = ref(null)
  const isStreaming = ref(false)
  const isLoadingHistory = ref(false)
  // Conversation-list pagination (server-side, scoped to activeAgentName).
  // convOffset == number of backend rows already loaded for the current agent;
  // convTotal == total backend rows for that agent. hasMore drives infinite
  // scroll. Both reset whenever the agent changes.
  const convOffset = ref(0)
  const convTotal = ref(0)
  const isLoadingConversations = ref(false)
  const isLoadingMoreConversations = ref(false)
  const convHasMore = computed(() => convOffset.value < convTotal.value)
  const ttsEnabled = ref(localStorage.getItem(TTS_STORAGE_KEY) === 'true')
  // Chat TTS plays through the singleton audio player (single source of
  // truth) — ttsPlaying just mirrors it so chat UI reacts without owning
  // an audio element of its own. See audioPlayer.playFromChat().
  const ttsPlaying = computed(() => {
    const audio = useAudioPlayerStore()
    return audio.playbackType === 'chatTts' && audio.isPlaying
  })

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

  function _mapConv(c) {
    return {
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
    }
  }

  /**
   * Fetch the conversation list from backend, scoped to the active agent and
   * paginated. ``reset`` replaces the list (initial load / agent switch);
   * otherwise it appends the next page (infinite scroll).
   *
   * Server-side filtering means the loaded list already contains ONLY the
   * active agent's conversations — the sidebar renders it directly, no
   * client-side agent filter needed. Returns early until an agent is known
   * so we never flash a cross-agent list on first mount.
   */
  async function fetchConversations({ reset = true } = {}) {
    if (!activeAgentName.value) return
    if (reset) {
      if (isLoadingConversations.value) return
      isLoadingConversations.value = true
      convOffset.value = 0
    } else {
      if (isLoadingMoreConversations.value || !convHasMore.value) return
      isLoadingMoreConversations.value = true
    }

    const agentAtRequest = activeAgentName.value
    try {
      const params = new URLSearchParams({
        agent_name: agentAtRequest,
        limit: String(CONV_PAGE_SIZE),
        offset: String(convOffset.value),
      })
      const data = await apiFetch(`/api/conversations?${params.toString()}`)
      const items = Array.isArray(data?.items) ? data.items.map(_mapConv) : []

      // Guard against a stale response landing after the user switched agents.
      if (activeAgentName.value !== agentAtRequest) return

      convTotal.value = Number.isFinite(data?.total) ? data.total : items.length

      if (reset) {
        // Keep any in-memory, never-sent conversation for THIS agent pinned on
        // top (created via "+" but not yet persisted, so absent from backend).
        const unsent = conversations.value.filter(
          c => !c.backendConversationId && c.agentName === agentAtRequest,
        )
        conversations.value = [...unsent, ...items]
      } else {
        const seen = new Set(conversations.value.map(c => c.id))
        conversations.value.push(...items.filter(c => !seen.has(c.id)))
      }
      convOffset.value += items.length

      // Auto-load history for the restored active conversation (initial load).
      if (reset && activeConversationId.value) {
        const exists = conversations.value.find(c => c.id === activeConversationId.value)
        if (exists && exists.backendConversationId && exists.messages.length === 0) {
          await fetchHistory(activeConversationId.value, exists.agentName)
        }
      }
    } catch (e) {
      console.warn('[ChatStore] Failed to fetch conversations:', e)
    } finally {
      if (reset) isLoadingConversations.value = false
      else isLoadingMoreConversations.value = false
    }
  }

  /** Load the next page of conversations (infinite scroll). */
  async function loadMoreConversations() {
    await fetchConversations({ reset: false })
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
        // Per-memory retrieval-lane provenance for recall blocks (one list per
        // recalled line, same order). Durable debug surface — see ChatMessages.
        recallLanes: msg.recall_lanes || null,
        // Per-memory RAW scores ({rel, conf, authority}) — same order as lines.
        recallScores: msg.recall_scores || null,
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
    // Resolve the agent ONCE, then make it the single source of truth for both
    // the conversation record AND the active-agent header. Previously only
    // conv.agentName got the 'Jarvis' fallback while activeAgentName was left
    // as-is — so if you hit "+" before the agent roster finished loading
    // (activeAgentName still null), the new conversation belonged to Jarvis but
    // the header read the null activeAgentName and rendered "No Agent".
    const resolvedAgent = agentName || activeAgentName.value || 'Jarvis'
    const conv = {
      id,
      title: 'New Conversation',
      agentName: resolvedAgent,
      backendConversationId: null, // Set by backend on first response
      messages: [],
      createdAt: Date.now(),
      updatedAt: Date.now(),
    }
    conversations.value.unshift(conv)
    activeConversationId.value = id
    activeAgentName.value = resolvedAgent
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
      // Re-point to the next conversation AND sync the active agent to it.
      // Previously only activeConversationId moved; activeAgentName kept the
      // deleted conversation's agent (or went stale), so the two truths
      // diverged — and deleting the LAST conversation left activeConversationId
      // null while the header still read a now-orphaned agent. Keep both in
      // lockstep: the next conversation's agent, or fall back to Jarvis.
      const next = conversations.value[0] || null
      activeConversationId.value = next?.id || null
      activeAgentName.value = next?.agentName || 'Jarvis'
    }
    _saveMeta()
  }

  /**
   * Bulk-delete conversations (the sidebar's multi-select). Sends one
   * request for all backend-persisted ids; purely in-memory (never-sent)
   * conversations are just dropped locally. Re-points the active selection
   * if it was among the deleted.
   */
  async function deleteConversations(ids) {
    const idSet = new Set(ids)
    const backendIds = conversations.value
      .filter(c => idSet.has(c.id) && c.backendConversationId)
      .map(c => c.backendConversationId)

    if (backendIds.length) {
      await apiFetch('/api/conversations/bulk-delete', {
        method: 'POST',
        body: JSON.stringify({ ids: backendIds }),
      })
    }

    conversations.value = conversations.value.filter(c => !idSet.has(c.id))
    // Keep the paging counters honest so convHasMore stays correct.
    convTotal.value = Math.max(0, convTotal.value - backendIds.length)
    convOffset.value = Math.max(0, convOffset.value - backendIds.length)

    if (idSet.has(activeConversationId.value)) {
      const next = conversations.value[0] || null
      activeConversationId.value = next?.id || null
      if (next) activeAgentName.value = next.agentName
      _saveMeta()
    }
  }

  async function setActiveAgent(name) {
    if (activeAgentName.value === name) return
    activeAgentName.value = name
    // The active conversation belonged to the previous agent — drop it.
    const conv = activeConversation.value
    if (conv && conv.agentName !== name) {
      activeConversationId.value = null
    }
    // The list is server-scoped per agent: clear the old agent's rows for
    // instant feedback (keep this agent's unsent draft if any), reset paging,
    // then reload the new agent's first page.
    conversations.value = conversations.value.filter(
      c => c.agentName === name && !c.backendConversationId,
    )
    convOffset.value = 0
    convTotal.value = 0
    _saveMeta()
    await fetchConversations({ reset: true })
  }

  function toggleTts() {
    if (ttsPlaying.value) {
      stopTts()
    }
    ttsEnabled.value = !ttsEnabled.value
    localStorage.setItem(TTS_STORAGE_KEY, String(ttsEnabled.value))
  }

  function stopTts() {
    // Chat TTS now plays through the singleton player; stop it there.
    const audio = useAudioPlayerStore()
    if (audio.playbackType === 'chatTts') {
      audio.stopAndReset()
    }
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
    // A new user turn starts a fresh "memory saved" group, so saves triggered by
    // THIS turn batch into one chip (and don't append to the previous turn's).
    conv._savedBlockId = null
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

  // A memory SSE event (recall/saved) fans out per-agent, but one agent owns
  // many conversations. The backend stamps the originating conversation id; drop
  // an event whose id doesn't match this conversation so a recall/saved chip from
  // conversation A never paints into conversation B (another tab, same agent).
  // Only excludes when BOTH ids are known — a brand-new conversation has no
  // backendConversationId until its first reply, so its first turn still shows
  // live (back-compat with events that carry no conversation_id).
  function belongsToConversation(data, conv) {
    return !(data.conversation_id && conv.backendConversationId
      && data.conversation_id !== conv.backendConversationId)
  }

  /**
   * Insert a live "memories used" recall block from the `memory_recalled` SSE
   * event so the chip shows DURING the turn (previously only after a page
   * reload, because the chip was built solely from fetched history). The payload
   * mirrors the persisted block (content + recall_lanes + recall_scores), so on
   * the next history fetch the persisted block replaces this one with no visible
   * change. (fetchHistory is a FULL replace — `conv.messages = history.map(...)`,
   * not a merge — so the `mem-live-*` block is discarded, never duplicated.)
   * Inserted BEFORE the in-flight assistant placeholder so it renders above the
   * reply, matching the persisted order.
   */
  function addMemoryRecallBlock(data, agentName) {
    if (!data || !data.content) return
    const conv = activeConversation.value
    // Only the conversation currently streaming this agent's turn gets the live
    // block; persisted history is the source of truth and fixes any drift on
    // reload. Guard on agent + streaming so a background agent's recall never
    // lands in the wrong open conversation.
    if (!conv || !isStreaming.value) return
    if (agentName && conv.agentName && agentName !== conv.agentName) return
    if (!belongsToConversation(data, conv)) return
    const block = {
      id: `mem-live-${crypto.randomUUID()}`,
      role: 'user',
      content: data.content,
      recallLanes: data.recall_lanes || null,
      recallScores: data.recall_scores || null,
      timestamp: Date.now(),
      isStreaming: false,
      toolCalls: [],
    }
    // Place it just before the last assistant message (the streaming placeholder).
    const msgs = conv.messages
    let i = msgs.length - 1
    while (i >= 0 && msgs[i].role !== 'assistant') i--
    if (i >= 0) msgs.splice(i, 0, block)
    else msgs.push(block)
  }

  /**
   * Live "memory saved" chip from the `memory_saved` SSE event — so the user
   * KNOWS the moment Jarvis stores something (auto-approved) or proposes one
   * (pending), in context, with inline undo/approve/reject — instead of only
   * discovering it later in the Memory tab. Items are keyed by `candidateId`, so
   * a later transition (pending→saved/rejected) updates the SAME item in place.
   * This block is a session-only NOTIFICATION (not LLM context, not persisted to
   * chat history) — the durable record lives in the Memory tab; on reload the
   * chip is simply gone. Auto-capture lands AFTER the reply (async), so no
   * isStreaming guard — saves group into the current turn's chip at the tail.
   */
  function addMemorySavedBlock(data, agentName) {
    if (!data || !data.candidate_id) return
    const conv = activeConversation.value
    if (!conv) return
    if (agentName && conv.agentName && agentName !== conv.agentName) return
    if (!belongsToConversation(data, conv)) return
    // Transition: update the existing item in place (status / arriving record_id).
    for (const m of conv.messages) {
      if (!m.memorySaved) continue
      const it = m.memorySaved.find(x => x.candidateId === data.candidate_id)
      if (it) {
        it.status = data.status
        if (data.record_id) it.recordId = data.record_id
        return
      }
    }
    if (data.status === 'rejected' || !data.content) return  // nothing to surface
    const item = {
      candidateId: data.candidate_id,
      recordId: data.record_id || null,
      content: data.content,
      memoryType: data.memory_type || 'semantic',
      status: data.status,            // 'saved' | 'pending'
      sensitive: !!data.sensitive,
    }
    let block = conv._savedBlockId
      ? conv.messages.find(m => m.id === conv._savedBlockId)
      : null
    if (!block) {
      block = {
        id: `mem-saved-${crypto.randomUUID()}`,
        role: 'system',
        isMemorySaved: true,
        memorySaved: [],
        timestamp: Date.now(),
      }
      conv.messages.push(block)
      conv._savedBlockId = block.id
    }
    block.memorySaved.push(item)
  }

  // Inline chip actions. Optimistic (flip status now); the resulting SSE is
  // idempotent (keyed by candidateId) so a confirming echo is a no-op.
  async function archiveSavedMemory(item) {
    const conv = activeConversation.value
    if (!conv || !item || !item.recordId) return
    const prev = item.status
    item.status = 'archived'
    try {
      await apiFetch(`/api/agents/${encodeURIComponent(conv.agentName)}/memories/${item.recordId}/archive`,
        { method: 'POST' })
    } catch (e) { item.status = prev; console.warn('[chat] archive memory failed:', e) }
  }
  async function approveSavedMemory(item) {
    const conv = activeConversation.value
    if (!conv || !item || !item.candidateId) return
    const prev = item.status
    item.status = 'saved'
    try {
      await apiFetch(`/api/agents/${encodeURIComponent(conv.agentName)}/memory-candidates/${item.candidateId}/approve`,
        { method: 'POST' })
    } catch (e) { item.status = prev; console.warn('[chat] approve memory failed:', e) }
  }
  async function rejectSavedMemory(item) {
    const conv = activeConversation.value
    if (!conv || !item || !item.candidateId) return
    const prev = item.status
    item.status = 'rejected'
    try {
      await apiFetch(`/api/agents/${encodeURIComponent(conv.agentName)}/memory-candidates/${item.candidateId}/reject`,
        { method: 'POST' })
    } catch (e) { item.status = prev; console.warn('[chat] reject memory failed:', e) }
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
    isLoadingConversations,
    isLoadingMoreConversations,
    convHasMore,
    ttsEnabled,
    ttsPlaying,
    activeConversation,
    activeMessages,
    sortedConversations,
    fetchConversations,
    loadMoreConversations,
    fetchHistory,
    createConversation,
    selectConversation,
    deleteConversation,
    deleteConversations,
    setActiveAgent,
    toggleTts,
    stopTts,
    addUserMessage,
    addAgentMessagePlaceholder,
    addMemoryRecallBlock,
    addMemorySavedBlock,
    archiveSavedMemory,
    approveSavedMemory,
    rejectSavedMemory,
    pushToolCall,
    finalizeAgentMessage,
    removeMessage,
    setMessageError,
    markMessageInterrupted,
  }
})
