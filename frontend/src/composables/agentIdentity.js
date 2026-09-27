/** Stable identity for a roster agent; team sessions may reuse display names. */
export function agentIdentity(agent) {
  return `${agent?.session_id || ''}\u0000${agent?.name || ''}`
}
