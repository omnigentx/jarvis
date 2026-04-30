/**
 * Composable for fetching and managing the meetings list.
 *
 * Provides initial fetch + SSE real-time updates via the event-driven
 * MeetingEventManager on the backend.
 */
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { apiFetch, buildSSEUrl } from '../api'

export function useMeetingList() {
  const meetings = ref([])
  const isLoading = ref(false)
  const error = ref(null)
  let eventSource = null

  // Fetch the initial list
  async function fetchMeetings() {
    isLoading.value = true
    error.value = null
    try {
      const data = await apiFetch('/api/agent/meetings')
      meetings.value = data.meetings || []
    } catch (e) {
      error.value = e.message
      console.error('[useMeetingList] Failed to fetch meetings:', e)
    } finally {
      isLoading.value = false
    }
  }

  // SSE stream for real-time updates
  function connectSSE() {
    if (eventSource) {
      eventSource.close()
    }

    const url = buildSSEUrl('/api/agent/meetings/stream')
    eventSource = new EventSource(url)

    eventSource.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data)
        if (event.type === 'heartbeat' || event.type === 'connected') return

        // Update meeting in list based on event
        handleMeetingEvent(event)
      } catch (err) {
        console.warn('[useMeetingList] Failed to parse SSE event:', err)
      }
    }

    eventSource.onerror = () => {
      console.warn('[useMeetingList] SSE connection error, reconnecting...')
      eventSource?.close()
      // Exponential backoff reconnect
      setTimeout(() => connectSSE(), 3000)
    }
  }

  function handleMeetingEvent(event) {
    const meetingId = event.meeting_id
    if (!meetingId) return

    switch (event.type) {
      case 'meeting_created': {
        const config = event.data?.config || {}
        meetings.value.unshift({
          meeting_id: meetingId,
          agenda: config.agenda || '',
          participants: config.participants || [],
          max_rounds: config.max_rounds,
          created_by: config.created_by || '',
          created_at: config.created_at || new Date().toISOString(),
          ended: false,
          started: false,
          joined: [],
          turn_count: 0,
          participant_count: config.participants?.length || 0,
          current_speaker: null,
        })
        break
      }

      case 'state_changed': {
        const idx = meetings.value.findIndex((m) => m.meeting_id === meetingId)
        if (idx >= 0) {
          const state = event.data?.state || {}
          const m = { ...meetings.value[idx] }
          if (state.ended !== undefined) m.ended = state.ended
          if (state.started !== undefined) m.started = state.started
          if (state.outcome !== undefined) m.outcome = state.outcome
          if (state.current_round !== undefined) m.current_round = state.current_round
          if (state.joined) m.joined = state.joined
          if (state.participants) {
            m.participants = state.participants
            m.participant_count = state.participants.length
            // Update current speaker
            const turn = state.current_turn ?? 0
            m.current_speaker =
              !m.ended && turn < state.participants.length ? state.participants[turn] : null
          }
          meetings.value[idx] = m
        }
        break
      }

      case 'transcript_entry': {
        const idx = meetings.value.findIndex((m) => m.meeting_id === meetingId)
        if (idx >= 0) {
          const m = { ...meetings.value[idx] }
          m.turn_count = (m.turn_count || 0) + 1
          const entry = event.data?.entry || {}
          m.last_message = {
            agent: entry.agent || '',
            content: (entry.message || '').slice(0, 100),
            timestamp: entry.timestamp || '',
          }
          meetings.value[idx] = m
        }
        break
      }

      case 'meeting_ended': {
        const idx = meetings.value.findIndex((m) => m.meeting_id === meetingId)
        if (idx >= 0) {
          const m = { ...meetings.value[idx] }
          m.ended = true
          m.outcome = event.data?.outcome || 'ended'
          meetings.value[idx] = m
        }
        break
      }
    }
  }

  // Computed: active + ended separation
  const activeMeetings = computed(() => meetings.value.filter((m) => !m.ended))
  const endedMeetings = computed(() => meetings.value.filter((m) => m.ended))

  onMounted(() => {
    fetchMeetings()
    connectSSE()
  })

  onUnmounted(() => {
    if (eventSource) {
      eventSource.close()
      eventSource = null
    }
  })

  return {
    meetings,
    activeMeetings,
    endedMeetings,
    isLoading,
    error,
    fetchMeetings,
  }
}
