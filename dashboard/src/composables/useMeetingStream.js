/**
 * Composable for streaming a single meeting's transcript in real time.
 *
 * Uses the event-driven SSE endpoint — no polling.
 * Receives initial transcript snapshot, then live push events.
 */
import { ref, onUnmounted, watch } from 'vue'
import { buildSSEUrl, apiFetch } from '../api'

export function useMeetingStream() {
  const transcript = ref([])
  const meetingState = ref({
    ended: false,
    started: false,
    outcome: null,
    current_round: 1,
    current_turn: 0,
    joined: [],
    participants: [],
  })
  const isConnecting = ref(false)
  const isConnected = ref(false)
  const error = ref(null)

  let eventSource = null
  let currentMeetingId = null
  let reconnectTimer = null
  let reconnectDelay = 1000

  /**
   * Connect to a meeting's SSE stream.
   * @param {string} meetingId
   */
  function connect(meetingId) {
    if (!meetingId) return
    disconnect()

    currentMeetingId = meetingId
    transcript.value = []
    meetingState.value = {
      ended: false,
      started: false,
      outcome: null,
      current_round: 1,
      current_turn: 0,
      joined: [],
      participants: [],
    }
    isConnecting.value = true
    error.value = null

    const url = buildSSEUrl(`/api/agent/meetings/${meetingId}/stream`)
    eventSource = new EventSource(url)

    eventSource.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data)
        handleEvent(event)
      } catch (err) {
        console.warn('[useMeetingStream] Parse error:', err)
      }
    }

    eventSource.onerror = () => {
      isConnected.value = false
      if (meetingState.value.ended) {
        // Meeting is done, no need to reconnect
        eventSource?.close()
        return
      }
      console.warn('[useMeetingStream] SSE error, will reconnect...')
      eventSource?.close()
      scheduleReconnect()
    }
  }

  function handleEvent(event) {
    switch (event.type) {
      case 'connected':
        isConnecting.value = false
        isConnected.value = true
        reconnectDelay = 1000 // Reset backoff on successful connect
        break

      case 'transcript_entry': {
        const entry = event.data?.entry || event.data || {}
        // Dedupe by turn number
        if (entry.turn && transcript.value.some((t) => t.turn === entry.turn)) {
          break
        }
        transcript.value.push({
          turn: entry.turn,
          round: entry.round,
          agent: entry.agent,
          message: entry.message,
          timestamp: entry.timestamp,
          type: entry.type || entry.entry_type || 'speak',
        })
        break
      }

      case 'state_changed': {
        const state = event.data?.state || {}
        if (state.ended !== undefined) meetingState.value.ended = state.ended
        if (state.started !== undefined) meetingState.value.started = state.started
        if (state.outcome !== undefined) meetingState.value.outcome = state.outcome
        if (state.current_round !== undefined) meetingState.value.current_round = state.current_round
        if (state.current_turn !== undefined) meetingState.value.current_turn = state.current_turn
        if (state.joined) meetingState.value.joined = state.joined
        if (state.participants) meetingState.value.participants = state.participants
        break
      }

      case 'turn_advanced': {
        if (event.data?.round) meetingState.value.current_round = event.data.round
        break
      }

      case 'meeting_ended': {
        meetingState.value.ended = true
        meetingState.value.outcome = event.data?.outcome || 'ended'
        break
      }

      case 'participant_joined': {
        const name = event.data?.agent_name
        if (name && !meetingState.value.joined.includes(name)) {
          meetingState.value.joined.push(name)
        }
        if (event.data?.all_joined) {
          meetingState.value.started = true
        }
        break
      }

      case 'heartbeat':
        // Keep-alive, nothing to do
        break

      default:
        // Log unknown events for debugging
        console.debug('[useMeetingStream] Unknown event:', event.type)
    }
  }

  function scheduleReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer)
    reconnectTimer = setTimeout(() => {
      if (currentMeetingId && !meetingState.value.ended) {
        reconnectDelay = Math.min(reconnectDelay * 2, 30000) // Max 30s
        connect(currentMeetingId)
      }
    }, reconnectDelay)
  }

  function disconnect() {
    if (eventSource) {
      eventSource.close()
      eventSource = null
    }
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    isConnected.value = false
    isConnecting.value = false
    currentMeetingId = null
  }

  onUnmounted(() => disconnect())

  return {
    transcript,
    meetingState,
    isConnecting,
    isConnected,
    error,
    connect,
    disconnect,
  }
}
