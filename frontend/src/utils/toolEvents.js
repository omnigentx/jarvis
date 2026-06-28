/**
 * Helpers for translating chat-stream SSE tool events into the
 * `pushToolCall` shape the chat store expects.
 *
 * Centralised here because both text chat (ChatView.vue) and voice
 * chat (useVoiceSession.js) consume the same SSE events and previously
 * had two near-identical handlers that drifted: one ignored parallel
 * tool_calls entirely, the other miscounted batch durations. Both
 * were silently dropping every tool past `tools[0]` when a single LLM
 * turn produced multiple tool_calls (e.g. Jarvis emits two
 * `spawn_and_run_isolated` calls in one assistant message).
 */

/**
 * Expand a `tool_call` / `tool_request` SSE event into one or more
 * pushToolCall payloads. Returns an array — the caller iterates and
 * pushes each entry separately so groupToolCalls in ChatMessages can
 * later pair each call with its result.
 *
 * Backend shape: `{ tools: [{ name, args }, ...], message, ... }`.
 * Legacy fallback: `{ tool | server, ... }` for events that predate
 * the batched `tools` field. We default the singleton case to
 * `tools: [...]` so the iteration is uniform.
 */
export function expandToolRequest(event) {
  const tools = event.tools?.length
    ? event.tools
    : [{ name: event.tool || event.server || 'tool', args: null }]
  return tools.map((t) => ({
    tool: t.name || 'tool',
    command: event.message || '',
    args: t.args || null,
  }))
}

/**
 * Expand a `tool_result` / `tool_done` SSE event the same way. Each
 * entry inherits the batch's `duration_ms` because the backend doesn't
 * split per-tool durations on parallel calls — that's a known
 * over-count in totalDuration; per-call accuracy lives in the
 * server-side hook, not the event payload. A future
 * `tools[*].duration_ms` field would let us split exactly.
 */
export function expandToolDone(event) {
  const tools = event.tools?.length
    ? event.tools
    : [{ name: event.tool || 'tool' }]
  const duration = event.duration_ms
    ? `${(event.duration_ms / 1000).toFixed(1)}s`
    : undefined
  return tools.map((t) => ({
    tool: t.name || 'tool',
    command: event.message || 'result',
    isResult: true,
    duration,
    // Each tool's OWN result — the backend now sends `tools[*].result_preview`.
    // Previously every tool inherited the batch-level `event.result_preview`
    // (the first tool's output), so e.g. get_current_time showed memory_remember's
    // result. Fall back to the batch field for older single-tool events.
    resultPreview: t.result_preview ?? event.result_preview ?? null,
  }))
}
