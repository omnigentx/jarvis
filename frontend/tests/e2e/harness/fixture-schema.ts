/**
 * Fixture schema — strict validation so typos fail at load, not at assertion.
 *
 * Each fixture describes ONE user flow's server side:
 *   - `responses`: keyed by "METHOD /api/path" → response JSON/text/status
 *   - `sse_streams`: keyed by SSE endpoint URL → timeline of events
 *   - `backend_source`: REQUIRED — citation (file:line or function) proving
 *     this fixture matches real backend behavior. Reviewers check this.
 *
 * Example:
 *
 *   backend_source:
 *     - backend/routes/setup.py::get_status
 *     - backend/middleware/setup_gate.py::SetupGateMiddleware
 *   responses:
 *     "GET /api/setup/status":
 *       status: 200
 *       json: { completed: false, step: "api_key" }
 *   sse_streams:
 *     "/api/chat/stream":
 *       - { delay_ms: 50, event: "token_delta", data: { text: "hello" } }
 *       - { delay_ms: 100, event: "end", data: {} }
 */

export type FixtureResponse = {
  status?: number
  json?: unknown
  text?: string
  headers?: Record<string, string>
}

export type SseEvent = {
  delay_ms?: number
  event?: string
  data: unknown
  /** Optional SSE id field for resume semantics. */
  id?: string
}

export type Fixture = {
  backend_source: string[]
  /**
   * Keyed by "METHOD /path" (method uppercase). Paths are literal, not regex.
   * Value is a single response OR a list of responses consumed in order —
   * useful for tests that trigger a state flip (e.g. DELETE then a re-fetch
   * that should now return the flipped state). The LAST entry is reused for
   * any further calls so tests don't need to count exact invocations.
   */
  responses: Record<string, FixtureResponse | FixtureResponse[]>
  /** Keyed by SSE endpoint path. Each stream is pushed sequentially. */
  sse_streams?: Record<string, SseEvent[]>
  /** Optional metadata — notes about drift risk, etc. Not validated. */
  notes?: string
}

const ALLOWED_TOP_KEYS = new Set([
  'backend_source',
  'responses',
  'sse_streams',
  'notes',
])
const ALLOWED_RESPONSE_KEYS = new Set(['status', 'json', 'text', 'headers'])
const ALLOWED_SSE_EVENT_KEYS = new Set(['delay_ms', 'event', 'data', 'id'])

export function validateFixture(path: string, raw: unknown): Fixture {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error(`Fixture ${path}: top-level must be a mapping`)
  }
  const obj = raw as Record<string, unknown>

  for (const key of Object.keys(obj)) {
    if (!ALLOWED_TOP_KEYS.has(key)) {
      throw new Error(`Fixture ${path}: unknown top-level key '${key}'`)
    }
  }

  const source = obj.backend_source
  if (
    !Array.isArray(source) ||
    source.length === 0 ||
    !source.every((s) => typeof s === 'string' && s.length > 0)
  ) {
    throw new Error(
      `Fixture ${path}: 'backend_source' must be a non-empty list of strings ` +
        `citing the backend code this fixture mirrors (e.g. 'backend/routes/x.py::fn').`
    )
  }

  const responses = obj.responses
  if (!responses || typeof responses !== 'object' || Array.isArray(responses)) {
    throw new Error(`Fixture ${path}: 'responses' must be a mapping`)
  }
  for (const [key, val] of Object.entries(responses as Record<string, unknown>)) {
    if (!/^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) \/.+/.test(key)) {
      throw new Error(
        `Fixture ${path}: response key '${key}' must match 'METHOD /path'`
      )
    }
    const items = Array.isArray(val) ? val : [val]
    if (Array.isArray(val) && val.length === 0) {
      throw new Error(
        `Fixture ${path}: response '${key}' is an empty list — give at least one response`
      )
    }
    for (let i = 0; i < items.length; i++) {
      const item = items[i]
      const label = Array.isArray(val) ? `${key}[${i}]` : key
      if (!item || typeof item !== 'object' || Array.isArray(item)) {
        throw new Error(`Fixture ${path}: response '${label}' must be a mapping`)
      }
      for (const k of Object.keys(item as Record<string, unknown>)) {
        if (!ALLOWED_RESPONSE_KEYS.has(k)) {
          throw new Error(
            `Fixture ${path}: response '${label}' has unknown field '${k}'`
          )
        }
      }
      const v = item as FixtureResponse
      if (v.json !== undefined && v.text !== undefined) {
        throw new Error(
          `Fixture ${path}: response '${label}' has both 'json' and 'text' — pick one`
        )
      }
    }
  }

  const streams = obj.sse_streams
  if (streams !== undefined) {
    if (!streams || typeof streams !== 'object' || Array.isArray(streams)) {
      throw new Error(`Fixture ${path}: 'sse_streams' must be a mapping`)
    }
    for (const [url, events] of Object.entries(
      streams as Record<string, unknown>
    )) {
      if (!url.startsWith('/')) {
        throw new Error(
          `Fixture ${path}: sse_streams key '${url}' must be a path (start with /)`
        )
      }
      if (!Array.isArray(events) || events.length === 0) {
        throw new Error(
          `Fixture ${path}: sse_streams['${url}'] must be a non-empty list`
        )
      }
      events.forEach((ev: unknown, i: number) => {
        if (!ev || typeof ev !== 'object' || Array.isArray(ev)) {
          throw new Error(
            `Fixture ${path}: sse_streams['${url}'][${i}] must be a mapping`
          )
        }
        for (const k of Object.keys(ev as Record<string, unknown>)) {
          if (!ALLOWED_SSE_EVENT_KEYS.has(k)) {
            throw new Error(
              `Fixture ${path}: sse_streams['${url}'][${i}] has unknown field '${k}'`
            )
          }
        }
        if ((ev as SseEvent).data === undefined) {
          throw new Error(
            `Fixture ${path}: sse_streams['${url}'][${i}] missing required 'data'`
          )
        }
      })
    }
  }

  return obj as Fixture
}
