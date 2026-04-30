import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import { apiFetch } from '../api'

export const useApprovalsStore = defineStore('approvals', () => {
  // --- State ---
  const approvals = ref(new Map())
  const selectedId = ref(null)
  const selectedApproval = ref(null)
  const stats = ref({ pending_count: 0, approved_today: 0, rejected_count: 0, avg_response_time: 0 })
  const isLoading = ref(false)
  const isLoadingDetail = ref(false)

  // --- Computed ---
  const approvalsList = computed(() => {
    return Array.from(approvals.value.values()).sort((a, b) => {
      // Pending first, then by urgency, then newest first
      if (a.status === 'pending' && b.status !== 'pending') return -1
      if (a.status !== 'pending' && b.status === 'pending') return 1
      const urgencyOrder = { urgent: 0, high: 1, normal: 2, low: 3 }
      const aUrg = urgencyOrder[a.urgency] ?? 2
      const bUrg = urgencyOrder[b.urgency] ?? 2
      if (aUrg !== bUrg) return aUrg - bUrg
      return (b.created_at || 0) - (a.created_at || 0)
    })
  })

  const pendingCount = computed(() => stats.value.pending_count || 0)

  // --- Actions ---
  async function fetchApprovals(status = null) {
    isLoading.value = true
    try {
      const params = status ? `?status=${status}` : ''
      const data = await apiFetch(`/api/approvals${params}`)
      const map = new Map()
      for (const item of data) {
        map.set(item.id, item)
      }
      approvals.value = map
    } catch (e) {
      console.error('[Approvals] Fetch failed:', e)
    } finally {
      isLoading.value = false
    }
  }

  async function fetchApproval(id) {
    isLoadingDetail.value = true
    try {
      const data = await apiFetch(`/api/approvals/${id}`)
      selectedApproval.value = data
      // Also update in list
      approvals.value.set(id, data)
      approvals.value = new Map(approvals.value)
      return data
    } catch (e) {
      console.error('[Approvals] Fetch detail failed:', e)
      return null
    } finally {
      isLoadingDetail.value = false
    }
  }

  async function resolveApproval(id, decision, comment = '') {
    try {
      const result = await apiFetch(`/api/approvals/${id}/resolve`, {
        method: 'PUT',
        body: JSON.stringify({ decision, comment }),
      })
      // Update local state
      if (selectedApproval.value?.id === id) {
        selectedApproval.value = { ...selectedApproval.value, ...result }
      }
      approvals.value.set(id, result)
      approvals.value = new Map(approvals.value)
      return result
    } catch (e) {
      console.error('[Approvals] Resolve failed:', e)
      throw e
    }
  }

  async function addComment(id, commentData) {
    try {
      const result = await apiFetch(`/api/approvals/${id}/comments`, {
        method: 'POST',
        body: JSON.stringify(commentData),
      })
      // Re-fetch detail to get updated comments
      await fetchApproval(id)
      return result
    } catch (e) {
      console.error('[Approvals] Add comment failed:', e)
      throw e
    }
  }

  async function updateComment(commentId, body) {
    try {
      const result = await apiFetch(`/api/approvals/comments/${commentId}`, {
        method: 'PUT',
        body: JSON.stringify({ body }),
      })
      // Re-fetch currently selected detail
      if (selectedId.value) await fetchApproval(selectedId.value)
      return result
    } catch (e) {
      console.error('[Approvals] Update comment failed:', e)
      throw e
    }
  }

  async function deleteComment(commentId) {
    try {
      const result = await apiFetch(`/api/approvals/comments/${commentId}`, {
        method: 'DELETE',
      })
      // Re-fetch currently selected detail
      if (selectedId.value) await fetchApproval(selectedId.value)
      return result
    } catch (e) {
      console.error('[Approvals] Delete comment failed:', e)
      throw e
    }
  }

  async function fetchStats() {
    try {
      const data = await apiFetch('/api/approvals/stats')
      stats.value = data
    } catch (e) {
      console.error('[Approvals] Stats failed:', e)
    }
  }

  function selectApproval(id) {
    selectedId.value = id
    if (id) fetchApproval(id)
  }

  /**
   * Process approval SSE events — called from agents store.
   */
  function processApprovalEvent(event) {
    const { event_type, data } = event

    switch (event_type) {
      case 'approval_created': {
        // Add to list (simplified record from event data)
        const id = data?.approval_id
        if (id) {
          approvals.value.set(id, {
            id,
            title: data.title,
            agent_name: data.agent_name,
            team_name: data.team_name,
            urgency: data.urgency,
            approval_type: data.approval_type,
            status: 'pending',
            created_at: event.timestamp,
            paused_agents: data.paused_agents || [],
          })
          approvals.value = new Map(approvals.value)
        }
        break
      }

      case 'approval_resolved': {
        const id = data?.approval_id
        if (id && approvals.value.has(id)) {
          const existing = approvals.value.get(id)
          approvals.value.set(id, {
            ...existing,
            status: data.decision === 'approve' ? 'approved' : 'rejected',
            user_decision: data.decision,
            user_comment: data.comment,
            resolved_at: event.timestamp,
          })
          approvals.value = new Map(approvals.value)
          // Re-fetch detail if this is the selected one
          if (selectedId.value === id) fetchApproval(id)
        }
        break
      }

      case 'approval_commented': {
        // Re-fetch detail if this is the selected approval
        const id = data?.approval_id
        if (id && selectedId.value === id) {
          fetchApproval(id)
        }
        break
      }

      case 'approval_stats':
        if (data) stats.value = data
        break
    }
  }

  return {
    approvals,
    approvalsList,
    selectedId,
    selectedApproval,
    stats,
    pendingCount,
    isLoading,
    isLoadingDetail,
    fetchApprovals,
    fetchApproval,
    fetchStats,
    resolveApproval,
    addComment,
    updateComment,
    deleteComment,
    selectApproval,
    processApprovalEvent,
  }
})
