import { ref } from 'vue'
import { apiFetch, getCsrfToken } from '../api'
import { useAuthStore } from '../stores/auth.js'

/**
 * Composable for streaming chat via POST /api/chat-stream.
 * Uses fetch + ReadableStream (not EventSource, because POST).
 * Supports multimodal: when files are present, sends as multipart/form-data.
 */
export function useChatStream() {
  const isStreaming = ref(false)
  const lastError = ref(null)
  // Held across the request so cancel() can abort the in-flight fetch
  // AND know which backend agent to interrupt (defaults to Jarvis).
  let _currentController = null
  let _currentAgentName = null

  /**
   * Send a message and stream SSE events back.
   * @param {string} message - User message
   * @param {string|null} conversationId - Backend conversation ID (null for new)
   * @param {(event: Object) => void} onEvent - Called for each SSE event
   * @param {File[]|null} files - Optional file attachments (images, audio)
   * @param {string|null} agentName - Target agent name (null = default/Jarvis)
   * @returns {Promise<void>}
   */
  async function send(message, conversationId, onEvent, files = null, agentName = null) {
    if (isStreaming.value) return
    isStreaming.value = true
    lastError.value = null

    const controller = new AbortController()
    _currentController = controller
    _currentAgentName = agentName || 'Jarvis'
    let aborted = false

    try {
      const headers = {}
      let body

      if (files && files.length > 0) {
        // Multipart/form-data for file uploads
        const formData = new FormData()
        formData.append('message', message)
        if (conversationId) {
          formData.append('conversation_id', conversationId)
        }
        if (agentName) {
          formData.append('agent_name', agentName)
        }
        for (const file of files) {
          formData.append('files', file)
        }
        body = formData
        // Don't set Content-Type — browser sets it with boundary
      } else {
        // JSON for text-only
        headers['Content-Type'] = 'application/json'
        body = JSON.stringify({
          message,
          conversation_id: conversationId || null,
          agent_name: agentName || null,
        })
      }

      // CSRF — chat-stream is a POST, so the double-submit header is
      // required by the session-cookie auth path. Bearer is no longer
      // sent: removing the localStorage API key was the whole point.
      const csrf = getCsrfToken()
      if (csrf) headers['X-CSRF-Token'] = csrf

      const res = await fetch('/api/chat-stream', {
        method: 'POST',
        credentials: 'include',
        headers,
        body,
        signal: controller.signal,
      })

      if (!res.ok) {
        const body = await res.text().catch(() => '')
        if (res.status === 401) {
          // Route through the auth store so the AuthGate modal opens
          // and ALL streams stop spinning — same contract as apiFetch.
          //
          // ``useAuthStore()`` here is intentionally lazy: it's
          // called inside the request handler (Vue setup scope is
          // already established by the composable's caller), and
          // hoisting it to module scope would import-time-bind a
          // store instance to a stale Pinia setup state in tests.
          // Keep this call inside the conditional.
          useAuthStore().on401('chat_stream_401')
        }
        throw new Error(`API ${res.status}: ${body || res.statusText}`)
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        // Keep the last incomplete line in buffer
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (!line.startsWith('data:')) continue
          const jsonStr = line.slice(5).trim()
          if (!jsonStr) continue

          try {
            const event = JSON.parse(jsonStr)
            onEvent(event)

            // Stop on terminal events
            if (event.type === 'done' || event.type === 'error') {
              aborted = true
              break
            }
          } catch {
            // Skip unparseable lines
          }
        }

        if (aborted) break
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        lastError.value = err.message
        onEvent({ type: 'error', data: { message: err.message } })
      }
    } finally {
      isStreaming.value = false
      if (!aborted) {
        try { controller.abort() } catch {}
      }
      if (_currentController === controller) {
        _currentController = null
        _currentAgentName = null
      }
    }
  }

  /**
   * Stop the in-flight chat stream.
   *
   * Two-tier semantics — matches `POST /api/agents/{name}/interrupt`:
   *   - `mode='soft'` (default): cancel the LLM call mid-stream. Running
   *     subagents continue their own conversations.
   *   - `mode='hard'`: also SIGTERM running subagents. Side-effects already
   *     committed are NOT rolled back (banner the caller should show).
   *
   * Returns the backend response so the caller can surface killed-pids /
   * side-effect warnings for hard mode.
   */
  async function cancel(mode = 'soft') {
    const agent = _currentAgentName
    // Abort the local fetch first — frees the UI immediately without
    // waiting for the round-trip. Backend cancellation still propagates
    // through pause_controller.interrupt() / hard_kill().
    if (_currentController) {
      try { _currentController.abort() } catch {}
    }
    isStreaming.value = false
    if (!agent) return null
    try {
      return await apiFetch(
        `/api/agents/${encodeURIComponent(agent)}/interrupt?mode=${mode}`,
        { method: 'POST' },
      )
    } catch (e) {
      lastError.value = e?.message || String(e)
      return null
    }
  }

  return { isStreaming, lastError, send, cancel }
}
