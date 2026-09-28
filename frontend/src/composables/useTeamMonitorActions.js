/** Build and execute session-scoped agent injection requests. */
export async function injectToAgent(apiFetch, agent, { text = '', files = [] } = {}) {
  const name = agent?.name
  const sessionId = agent?.session_id
  if (!name) throw new Error('Agent name required')
  if (!sessionId && agent?.type !== 'builtin') throw new Error('Session identity required')
  const query = agent?.type === 'builtin'
    ? '?target=static'
    : `?session_id=${encodeURIComponent(sessionId)}`
  const url = `/api/agents/${encodeURIComponent(name)}/inject${query}`
  if (files.length) {
    const body = new FormData()
    body.append('message', text.trim())
    for (const file of files) body.append('files', file)
    return apiFetch(url, { method: 'POST', body })
  }
  return apiFetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: text.trim() }) })
}

/** Name-only DELETE must abort if target name now refers to multiple agents. */
export async function deleteAgentByName(apiFetch, getRoster, name) {
  if (getRoster().filter(agent => agent.name === name).length > 1) {
    return { blocked: true }
  }
  await apiFetch(`/api/agents/${encodeURIComponent(name)}`, { method: 'DELETE' })
  return { blocked: false }
}

/** Fan-out requests as settled results, preserving target/request index correspondence. */
export function injectToAgents(inject, agents, payload) {
  return Promise.allSettled(agents.map(agent => inject(agent, payload)))
}

/** Execute one bulk delete only when no target name collides in roster. */
export async function deleteAgentsByName(apiFetch, getRoster, agents) {
  const roster = getRoster()
  if (agents.some(target => roster.filter(agent => agent.name === target.name).length > 1)) {
    return { blocked: true, results: [] }
  }
  const results = await Promise.allSettled(agents.map(agent => apiFetch(`/api/agents/${encodeURIComponent(agent.name)}`, { method: 'DELETE' })))
  return { blocked: false, results }
}
