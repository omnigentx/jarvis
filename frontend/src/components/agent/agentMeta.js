/**
 * Pure helpers shared across the Agents views.
 *
 * Classifies live agents (from the agents store) into the three rings the
 * orbit hero needs (built-in / spawned / team-templates) and exposes a
 * role-palette lookup that mirrors the design tokens.
 *
 * Kept dependency-free (no Vue, no DOM) so node:test can cover it.
 */

const ROLE_PALETTE = {
  pm: { token: '--role-pm',  short: 'PM'  },
  sa: { token: '--role-sa',  short: 'SA'  },
  ba: { token: '--role-ba',  short: 'BA'  },
  dev: { token: '--role-dev', short: 'DEV' },
  qe: { token: '--role-qe',  short: 'QE'  },
  des: { token: '--role-des', short: 'DES' },
  dso: { token: '--role-dso', short: 'DSO' },
}

/**
 * Resolve a role short-code from an agent record. Looks at:
 *   - agent.role (explicit)
 *   - "Name [TAG]" suffix produced by the team spawner
 *   - falls back to first 3 letters of the name
 */
export function roleShortCode(agent) {
  if (!agent) return '--'
  const explicit = (agent.role || '').toString().trim().toLowerCase()
  if (ROLE_PALETTE[explicit]) return explicit
  const tagMatch = /\[([A-Za-z]+)\]/.exec(agent.name || '')
  if (tagMatch) {
    const t = tagMatch[1].toLowerCase()
    if (ROLE_PALETTE[t]) return t
  }
  return ''
}

/**
 * CSS class for the role avatar circle. Returns 'ava-jarvis' for the
 * Jarvis conductor, 'ava-<role>' for known roles, or '' otherwise.
 */
export function roleAvaClass(agent) {
  if (!agent) return ''
  if (agent.is_default || (agent.name || '').toLowerCase() === 'jarvis') {
    return 'ava-jarvis'
  }
  const r = roleShortCode(agent)
  return r ? `ava-${r}` : ''
}

/**
 * Token name (without var()) for the role color band. Falls back to the
 * neutral border token when the role can't be determined.
 */
export function roleColorToken(agent) {
  const r = roleShortCode(agent)
  return ROLE_PALETTE[r]?.token || '--border-strong'
}

/** 2-3 letter glyph used inside the avatar circle. */
export function avatarGlyph(agent) {
  if (!agent) return '?'
  if (agent.is_default || (agent.name || '').toLowerCase() === 'jarvis') return 'J'
  const r = roleShortCode(agent)
  if (r) return ROLE_PALETTE[r].short
  const name = (agent.name || '?').replace(/\W+/g, '')
  return name.slice(0, 2).toUpperCase() || '?'
}

/**
 * Classify an agent into one of the orbit rings:
 *   - 'conductor' — Jarvis itself
 *   - 'core'      — built-in static agents (Personal/IoT/Music/AudioReader/...)
 *   - 'spawned'   — runtime spawned agents OR card agents (dynamic)
 *   - 'team'      — PM-orchestrators (anchor of a team)
 *
 * Used by the orbit hero AND the tree/flow tree layout.
 */
export function classifyAgent(agent) {
  if (!agent) return 'core'
  if (agent.is_default || (agent.name || '').toLowerCase() === 'jarvis') return 'conductor'
  const type = (agent.type || '').toLowerCase()
  if (type === 'builtin') return 'core'
  // PM-orchestrator anchors a team — surface as a team-template node.
  if (roleShortCode(agent) === 'pm' && agent.team_name) return 'team'
  return 'spawned'
}

/**
 * Build the orbit hero data structure: 3 rings around Jarvis.
 *
 * Returns { conductor, core[], spawned[], teams[] } where ``teams`` is a
 * list of `{ name, color, pm, members[] }` derived from the agents that
 * share a ``team_name``.
 */
export function buildOrbitGroups(agents) {
  const list = Array.isArray(agents) ? agents : []
  const conductor = list.find(a => classifyAgent(a) === 'conductor') || null
  const core = list.filter(a => classifyAgent(a) === 'core')
  const teamMap = new Map()
  const spawned = []
  for (const a of list) {
    const k = classifyAgent(a)
    if (k === 'conductor' || k === 'core') continue
    if (a.team_name) {
      if (!teamMap.has(a.team_name)) {
        teamMap.set(a.team_name, { name: a.team_name, members: [], pm: null })
      }
      const team = teamMap.get(a.team_name)
      if (roleShortCode(a) === 'pm') team.pm = a
      team.members.push(a)
    } else {
      spawned.push(a)
    }
  }
  return {
    conductor,
    core,
    spawned,
    teams: Array.from(teamMap.values()),
  }
}

/**
 * Stable team color picker — same hash as TeamMonitor's teamColor so the
 * pill colors don't shift between views.
 */
const TEAM_COLORS = ['#6366F1', '#8B5CF6', '#EC4899', '#14B8A6', '#F97316', '#06B6D4', '#84CC16', '#E879F9']
export function teamColor(teamName) {
  if (!teamName) return TEAM_COLORS[0]
  let hash = 0
  for (let i = 0; i < teamName.length; i++) hash = ((hash << 5) - hash + teamName.charCodeAt(i)) | 0
  return TEAM_COLORS[Math.abs(hash) % TEAM_COLORS.length]
}

/**
 * Canonical agent-status → color map. Single source of truth for every
 * status dot / accent / badge in the app. Mirror of the
 * `--status-*` CSS variables defined in `assets/tokens.css`; the JS
 * string returns the `var()` reference so theme changes flow without
 * touching JS. Callers MUST consume via inline `style="{ fill: ... }"`
 * (NOT via SVG `fill="..."` attribute, which doesn't resolve var()).
 *
 * UX convention (matches design tokens + best practice):
 *   green  = healthy/active   (running, completed, resuming)
 *   gray   = quiet/inactive   (idle)
 *   amber  = transitional     (pausing)
 *   purple = paused           (deliberate, distinct from warning)
 *   cyan   = thinking         (processing)
 *   blue   = info             (spawning, starting)
 *   red    = problem          (error, blocked)
 */
const STATUS_VAR = {
  running:   'var(--status-running)',
  completed: 'var(--status-completed)',
  resuming:  'var(--status-resuming)',
  idle:      'var(--status-idle)',
  pausing:   'var(--status-pausing)',
  paused:    'var(--status-paused)',
  thinking:  'var(--status-thinking)',
  spawning:  'var(--status-spawning)',
  starting:  'var(--status-starting)',
  blocked:   'var(--status-blocked)',
  error:     'var(--status-error)',
}
export function statusColor(status) {
  return STATUS_VAR[status] || 'var(--status-unknown)'
}

/** Human-readable status label — shared so monitor/terminal/badge agree. */
const STATUS_LABEL = {
  running:   'Running',
  completed: 'Completed',
  resuming:  'Resuming…',
  idle:      'Idle',
  pausing:   'Pausing…',
  paused:    'Paused',
  thinking:  'Thinking…',
  spawning:  'Spawning',
  starting:  'Starting',
  blocked:   'Blocked',
  error:     'Error',
}
export function statusLabel(status) {
  return STATUS_LABEL[status] || 'Unknown'
}
