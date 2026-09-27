/** Stable session-scoped identity used by roster views and turn history. */
export function agentIdentity(agent) {
  return agent?.session_id ? `${agent.session_id}\u0000${agent?.name || ''}` : (agent?.name || '')
}

/** Resolve identity only when unambiguous; never guess among same-name teams. */
export function findAgentByIdentity(agents, name, sessionId) {
  if (sessionId) return agents.find(agent => agent.name === name && agent.session_id === sessionId)
  const matches = agents.filter(agent => agent.name === name)
  return matches.length === 1 ? matches[0] : undefined
}
