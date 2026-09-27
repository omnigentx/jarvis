/** Consistent session-scoped key for roster agents; preserve legacy unscoped identities. */
export function agentKey(agent) {
  return agent?.session_id ? `${agent.session_id}\u0000${agent?.name || ''}` : (agent?.name || '')
}

export function findAgent(map, name, sessionId) {
  if (sessionId) return map.get(agentKey({ name, session_id: sessionId }))
  return map.get(agentKey({ name })) || [...map.values()].find(agent => agent.name === name)
}
