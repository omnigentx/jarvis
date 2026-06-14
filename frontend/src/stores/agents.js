import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
// Explicit .js so node:test (used by src/stores/agents.test.js) can
// resolve the bare specifier without a custom loader. Vite ignores
// the extension either way.
import { apiFetch } from '../api.js'

export const useAgentsStore = defineStore('agents', () => {
  // --- State ---
  const agents = ref(new Map())
  const isLoading = ref(false)
  const error = ref(null)
  const recentEvents = ref([]) // last 50 events for activity feed
  const tokenMetrics = ref(new Map()) // per-agent token accumulation from SSE

  // --- Computed ---
  // Sort priority: running > error > default(Jarvis) > idle.
  // After the 2026-05-20 lifecycle merge, only oneshot agents transit
  // through ``completed`` and they're removed by the cleanup hook
  // immediately — non-oneshot agents settle into ``idle``. No status
  // badge for ``completed`` is rendered anywhere, so it's omitted from
  // the priority table too.
  const STATUS_PRIORITY = { running: 0, paused: 1, error: 2, idle: 3 }
  const agentsList = computed(() => {
    return Array.from(agents.value.values()).sort((a, b) => {
      const aPri = a.is_default ? 0 : (STATUS_PRIORITY[a.status] ?? 4)
      const bPri = b.is_default ? 0 : (STATUS_PRIORITY[b.status] ?? 4)
      if (aPri !== bPri) return aPri - bPri
      return a.name.localeCompare(b.name)
    })
  })

  const stats = computed(() => {
    const list = agentsList.value
    return {
      total: list.length,
      running: list.filter(a => a.status === 'running').length,
      paused: list.filter(a => a.status === 'paused').length,
      idle: list.filter(a => a.status === 'idle').length,
      error: list.filter(a => a.status === 'error').length,
    }
  })

  // --- Actions ---
  async function fetchAgents() {
    isLoading.value = true
    error.value = null
    try {
      const data = await apiFetch('/api/agents')
      const newMap = new Map()
      for (const agent of data) {
        // Preserve existing realtime state if available
        const existing = agents.value.get(agent.name)
        newMap.set(agent.name, {
          ...agent,
          status: existing?.status || agent.status || 'idle',
          lastAction: existing?.lastAction || null,
          lastError: existing?.lastError || null,
        })
      }
      agents.value = newMap
    } catch (e) {
      error.value = e.message
      console.error('[Store] Failed to fetch agents:', e)
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Fetch persisted activities for ALL agents in one bulk call.
   * Returns a Map<agentName, Event[]> of persisted events.
   */
  async function fetchAllActivities(perAgent = 20) {
    try {
      const data = await apiFetch(`/api/agents/activities/recent?per_agent=${perAgent}`)
      // data is { agentName: [{id, event_type, message, run_id, data, created_at}, ...], ... }
      return data
    } catch (e) {
      console.error('[Store] Failed to fetch activities:', e)
      return {}
    }
  }

  function upsertAgent(name, updates) {
    const existing = agents.value.get(name)
    const isNew = !existing
    agents.value.set(name, { ...(existing || { name }), ...updates })
    // Trigger reactivity
    agents.value = new Map(agents.value)

    // SSE-only spawn path: bridge broadcasts ``started`` /
    // ``lifecycle_registered`` for team members but does NOT broadcast
    // ``agent_added`` (only manually-created cards via POST /api/agents
    // get that). Without this self-heal, new team agents appear on the
    // monitor without their ``team_name`` / ``description`` / ``tools``
    // metadata until the user hard-refreshes — incident 2026-05-11:
    // PM (Elliot [PM]) spawned but only visible after Cmd+Shift+R.
    //
    // Trigger a single coalesced refetch when an unknown name shows up.
    // ``fetchAgents()`` preserves existing realtime state so this is
    // safe even for the agent we just upserted.
    if (isNew) {
      _scheduleAgentRefetch()
    }
  }

  // Coalesce burst of "agent_added" detections from a single team-spawn
  // wave (PM + N members fire within ~50ms) into one /api/agents call.
  let _refetchTimer = null
  function _scheduleAgentRefetch() {
    if (_refetchTimer) return
    _refetchTimer = setTimeout(() => {
      _refetchTimer = null
      fetchAgents().catch(() => { /* ignored — store keeps realtime data */ })
    }, 250)
  }

  function pushEvent(event) {
    recentEvents.value = [event, ...recentEvents.value].slice(0, 50)
  }

  // Pause-cycle states OWN the agent's status; non-pause events
  // (idle / response / result / message_turn / etc.) must not clobber
  // them. The pause_controller is the single authority that
  // transitions OUT of this set via ``agent_resumed`` /
  // ``agent_paused`` events. Every handler that would otherwise set
  // ``status: 'idle' | 'running'`` consults this set first.
  const PAUSE_CYCLE = new Set(['pausing', 'paused', 'resuming'])

  /**
   * Mutate agent fields but skip the ``status`` field when the agent
   * is currently in a pause-cycle state. Use this in any SSE handler
   * that would set status='idle'/'running'/etc. — only the pause
   * handlers (``agent_pausing``/``paused``/``resuming``/``resumed``)
   * should write status while pause owns it.
   */
  function upsertAgentPreservingPauseCycle(name, fields) {
    const current = agents.value.get(name)
    if (PAUSE_CYCLE.has(current?.status) && 'status' in fields) {
      const { status: _drop, ...rest } = fields
      if (Object.keys(rest).length) upsertAgent(name, rest)
      return
    }
    upsertAgent(name, fields)
  }

  /**
   * Central event processor — ALL SSE events route through here.
   * @param {Object} event - SSE event payload
   */
  function processEvent(event) {
    const { agent_name, event_type } = event
    if (!agent_name || !event_type) return

    pushEvent(event)

    switch (event_type) {
      case 'started':
      case 'resumed':
        // Pause-aware: subprocess can emit `resumed` (its own MCP
        // lifecycle, unrelated to PauseController) after a manual
        // pause — preserve the pause state instead of bouncing back
        // to running.
        upsertAgentPreservingPauseCycle(agent_name, {
          status: 'running',
          lastAction: { message: event.message, timestamp: event.timestamp },
        })
        break

      case 'thinking':
      case 'tool_call':
      case 'tool_result': {
        // In-flight progress events. After the user pauses, subprocess
        // hooks may still emit a tail of queued events (LLM stream
        // tokens, tool result echo) that arrive AFTER ``agent_paused``.
        // Without the pause-cycle guard, these flip status back to
        // 'running' a few seconds after pause → UI fights itself.
        // The previous version only checked 'paused' and missed both
        // transitional states.
        upsertAgentPreservingPauseCycle(agent_name, {
          status: 'running',
          lastAction: { message: event.message, timestamp: event.timestamp },
        })
        break
      }

      case 'result':
        // Post-2026-05-20: all agents settle into 'idle' after task done.
        // Oneshot agents are removed seconds later by the cleanup hook;
        // resumable agents stay idle until the next resume.
        // Pause-aware: skip status override when pause-cycle owns it.
        upsertAgentPreservingPauseCycle(agent_name, {
          status: 'idle',
          lastAction: { message: event.message || 'Done', timestamp: event.timestamp },
        })
        break

      case 'error':
        upsertAgent(agent_name, {
          status: 'error',
          lastError: event.data?.message || event.message,
          lastAction: { message: event.message || 'Error', timestamp: event.timestamp },
        })
        break

      case 'idle':
        upsertAgentPreservingPauseCycle(agent_name, {
          status: 'idle',
          lastAction: { message: 'Idle', timestamp: event.timestamp },
        })
        break

      case 'response':
        upsertAgentPreservingPauseCycle(agent_name, {
          status: 'idle',
          lastAction: { message: event.message, timestamp: event.timestamp },
        })
        // Reload conversation history from backend — the response was saved
        // to session but chat-stream SSE may have dropped during long waits
        import('./chat').then(({ useChatStore }) => {
          const chatStore = useChatStore()
          const conv = chatStore.activeConversation
          if (conv?.backendConversationId && chatStore.activeAgentName === agent_name) {
            chatStore.fetchHistory(conv.backendConversationId)
          }
        }).catch(() => {})
        break

      case 'agent_added':
        fetchAgents() // Full re-fetch to get complete agent data
        break

      case 'agent_removed':
        agents.value.delete(agent_name)
        agents.value = new Map(agents.value) // trigger reactivity
        break

      case 'agent_pausing': {
        // Transitional: pause request received, agent still finishing
        // its in-flight LLM/tool call. Show a "Pausing…" spinner.
        // Snapshot the pre-pause status now (not at agent_paused) because
        // by the time we hit agent_paused the agent may have moved on.
        const current = agents.value.get(agent_name)
        const prior = current?.status
        const restorable = (prior && prior !== 'paused' && prior !== 'pausing')
          ? prior : 'idle'
        upsertAgent(agent_name, {
          status: 'pausing',
          prePauseStatus: restorable,
          lastAction: { message: event.message || 'Pausing…', timestamp: event.timestamp },
        })
        break
      }

      case 'agent_paused': {
        // Terminal: agent has actually blocked at a checkpoint (or was
        // already idle when pause arrived). prePauseStatus was captured
        // on agent_pausing — preserve it.
        const current = agents.value.get(agent_name)
        const fallbackPrior = current?.status
        const restorable = current?.prePauseStatus
          || ((fallbackPrior && fallbackPrior !== 'paused' && fallbackPrior !== 'pausing')
              ? fallbackPrior : 'idle')
        upsertAgent(agent_name, {
          status: 'paused',
          prePauseStatus: restorable,
          lastAction: { message: event.message || 'Paused', timestamp: event.timestamp },
        })
        break
      }

      case 'agent_resuming': {
        // Transitional: resume request received, agent still blocked at
        // checkpoint waiting for the await to wake. Show "Resuming…".
        upsertAgent(agent_name, {
          status: 'resuming',
          lastAction: { message: event.message || 'Resuming…', timestamp: event.timestamp },
        })
        break
      }

      case 'agent_resumed': {
        // Terminal: agent has woken from the checkpoint await. Restore
        // the pre-pause status (idle/running/completed/...) instead of
        // forcing 'running'. If the agent has actual pending work, the
        // next 'thinking' / 'tool_call' / 'message_turn' event arrives
        // moments later and naturally bumps status to 'running' again.
        const current = agents.value.get(agent_name)
        const restored = current?.prePauseStatus || 'idle'
        upsertAgent(agent_name, {
          status: restored,
          prePauseStatus: undefined,
          lastAction: { message: event.message || 'Resumed', timestamp: event.timestamp },
        })
        break
      }

      case 'message_turn': {
        // Source-of-truth event derived from agent.message_history.
        // Subscribers (useAgentTurns composable) read it from
        // recentEvents — the store only needs to keep status in sync.
        // Uses the same pause-cycle guard as ``result`` / ``idle`` /
        // ``response`` — status mutations are skipped when the agent
        // is in pausing/paused/resuming.
        const msg = event.data?.message
        const stop = msg?.stop_reason
        if (msg?.role === 'assistant' && msg?.tool_calls) {
          upsertAgentPreservingPauseCycle(agent_name, { status: 'running' })
        } else if (msg?.role === 'assistant' && stop && stop !== 'toolUse') {
          upsertAgentPreservingPauseCycle(agent_name, { status: 'idle' })
        }
        break
      }

      case 'token_usage': {
        // Accumulate token metrics per agent from SSE
        const d = event.data || {}
        const prev = tokenMetrics.value.get(agent_name) || {
          total_tokens: 0, input_tokens: 0, output_tokens: 0,
          cached_tokens: 0, reasoning_tokens: 0, est_cost: 0, llm_calls: 0,
        }
        tokenMetrics.value.set(agent_name, {
          total_tokens: prev.total_tokens + (d.total_tokens || 0),
          input_tokens: prev.input_tokens + (d.input_tokens || 0),
          output_tokens: prev.output_tokens + (d.output_tokens || 0),
          cached_tokens: prev.cached_tokens + (d.cached_tokens || 0),
          reasoning_tokens: prev.reasoning_tokens + (d.reasoning_tokens || 0),
          est_cost: prev.est_cost + (d.est_cost || 0),
          llm_calls: prev.llm_calls + 1,
          model: d.model || prev.model,
        })
        tokenMetrics.value = new Map(tokenMetrics.value) // trigger reactivity
        // Also update the agent's tokenCount for card display
        upsertAgent(agent_name, {
          tokenCount: formatTokenCount(prev.total_tokens + (d.total_tokens || 0)),
        })
        break
      }

      case 'context_compaction_started':
        // Status untouched — compaction is a background maintenance step,
        // not an agent state transition (the agent stays running).
        upsertAgent(agent_name, {
          compaction: { inProgress: true, last: agents.value.get(agent_name)?.compaction?.last || null },
        })
        break

      case 'context_compaction_completed':
        upsertAgent(agent_name, {
          compaction: {
            inProgress: false,
            last: {
              status: 'completed',
              savedTokens: event.data?.saved_tokens || 0,
              reductionRatio: event.data?.reduction_ratio || 0,
              eventId: event.data?.event_id ?? null,
              timestamp: event.timestamp,
            },
          },
        })
        break

      case 'context_compaction_failed':
        upsertAgent(agent_name, {
          compaction: {
            inProgress: false,
            last: {
              status: 'failed',
              error: event.data?.error || 'unknown error',
              timestamp: event.timestamp,
            },
          },
        })
        break

      default:
        // Forward approval events to approvals store
        if (event_type.startsWith('approval_')) {
          import('./approvals').then(({ useApprovalsStore }) => {
            useApprovalsStore().processApprovalEvent(event)
          }).catch(e => console.warn('[Store] Failed to forward approval event:', e))
          break
        }
        // Forward the live-reactive memory events (spec §17 subset) to the
        // memory store: candidate badge, index refresh, degraded banner.
        if (event_type.startsWith('memory_') || event_type === 'retrieval_degraded'
            || event_type === 'retrieval_completed') {
          import('./memory').then(({ useMemoryStore }) => {
            useMemoryStore().processMemoryEvent(event)
          }).catch(e => console.warn('[Store] Failed to forward memory event:', e))
          break
        }
        // Unknown event — still track
        upsertAgent(agent_name, {
          lastAction: { message: event.message, timestamp: event.timestamp },
        })
    }
  }

  async function pauseAgent(name) {
    // Optimistic transition to 'pausing' for snappy UI (SSE for
    // 'agent_pausing' usually arrives within ~50ms but the optimistic
    // hop avoids a "nothing happened" flash if SSE is briefly delayed).
    // The SSE event handler will upgrade to terminal 'paused' once the
    // agent actually blocks at a checkpoint — do NOT pre-jump to 'paused'
    // here, that hides the in-flight transition the user needs to see.
    upsertAgent(name, {
      status: 'pausing',
      lastAction: { message: 'Pausing…', timestamp: Date.now() / 1000 },
    })
    try {
      return await apiFetch(`/api/agents/${encodeURIComponent(name)}/pause`, { method: 'POST' })
    } catch (e) {
      console.error('[Store] Failed to pause agent:', e)
      throw e
    }
  }

  async function resumeAgent(name) {
    // Optimistic transition. Roll back below if backend rejects.
    upsertAgent(name, {
      status: 'resuming',
      lastAction: { message: 'Resuming…', timestamp: Date.now() / 1000 },
    })
    try {
      return await apiFetch(`/api/agents/${encodeURIComponent(name)}/resume`, { method: 'POST' })
    } catch (e) {
      // 409 Conflict = agent is locked by a pending approval. Roll
      // the optimistic 'resuming' back to 'paused' so the badge is
      // truthful, and surface a clear error to the caller (AgentCard
      // can show a toast that points at the approval).
      const detail = e?.detail || e?.body?.detail || e?.body || {}
      if (e?.status === 409 || detail?.error === 'approval_pause_lock') {
        upsertAgent(name, {
          status: 'paused',
          lastAction: {
            message: `Cannot resume — approval ${detail.approval_id || ''} pending`,
            timestamp: Date.now() / 1000,
          },
        })
        const err = new Error(detail.message || 'Agent locked by pending approval')
        err.code = 'approval_pause_lock'
        err.approvalId = detail.approval_id
        throw err
      }
      console.error('[Store] Failed to resume agent:', e)
      throw e
    }
  }

  function formatTokenCount(n) {
    if (!n || n === 0) return '—'
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
    return String(n)
  }

  return {
    agents,
    agentsList,
    stats,
    isLoading,
    error,
    recentEvents,
    tokenMetrics,
    fetchAgents,
    fetchAllActivities,
    processEvent,
    upsertAgent,
    pauseAgent,
    resumeAgent,
    formatTokenCount,
  }
})
