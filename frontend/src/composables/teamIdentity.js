/** Stable grouping key for a team display name within a session. */
export function teamIdentity(team) {
  return `${team?.session_id || ''}\u0000${team?.team_name || ''}`
}

/** Return a readable disambiguated team label only when names collide. */
export function teamLabel(team, teams) {
  const duplicates = teams.filter(other => other.team_name === team.team_name)
  return duplicates.length > 1 ? `${team.team_name} (${String(team.session_id || '').slice(0, 8)})` : team.team_name
}

/** Build distinct team groups so identical display names in sessions never merge. */
export function groupAgentsByTeam(agents) {
  const grouped = new Map()
  for (const agent of agents) {
    if (!agent.team_name || !agent.session_id) continue
    const key = teamIdentity(agent)
    if (!grouped.has(key)) grouped.set(key, { name: agent.team_name, team_name: agent.team_name, session_id: agent.session_id, agents: [] })
    grouped.get(key).agents.push(agent)
  }
  return [...grouped.values()]
}

/** Encode a team filter with session identity and a delimiter-safe team name. */
export function makeTeamFilter(team) {
  return `team:${team.session_id}:${encodeURIComponent(team.team_name)}`
}

/** Parse a composite team filter. */
export function decodeTeamFilter(filter) {
  const [, sessionId, ...encodedName] = filter.split(':')
  return { sessionId, teamName: decodeURIComponent(encodedName.join(':')) }
}
