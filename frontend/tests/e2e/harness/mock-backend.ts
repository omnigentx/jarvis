/**
 * Mock backend — intercept /api/* and serve responses from a fixture YAML.
 *
 * Usage:
 *   const backend = await mockBackend(page, 'fixtures/setup_wizard_fresh.yaml')
 *   // backend.fulfilled: list of (method, path) actually intercepted
 *   // backend.unexpected: requests that had no fixture match (FAIL-LOUD)
 *
 * Strict by design:
 *   - Unknown request paths are rejected with status 599 + descriptive body.
 *   - Fixture exhaustion (response listed but consumed zero times) reported by
 *     `backend.assertAllFulfilled()`.
 *   - SSE streams end with explicit `[DONE]` marker so tests detect truncation.
 */

import { readFileSync } from 'node:fs'
import { load as yamlLoad } from 'js-yaml'
import type { Page, Route } from '@playwright/test'

import type { Fixture, FixtureResponse, SseEvent } from './fixture-schema'
import { validateFixture } from './fixture-schema'

export type RequestRecord = {
  method: string
  path: string
  status: number
  matchedFixture: string | null
}

export type MockBackend = {
  fulfilled: RequestRecord[]
  unexpected: RequestRecord[]
  assertAllFulfilled: () => void
  /** Which fixture keys matched at least once. Useful for coverage sanity. */
  matchedKeys: () => string[]
}

export async function mockBackend(
  page: Page,
  fixturePath: string | string[]
): Promise<MockBackend> {
  // Accept a single fixture OR an array — array order is precedence
  // (LATER entries WIN). Typical use: `[noiseFixture, flowFixture]` so the
  // flow fixture can override noise defaults when needed.
  const paths = Array.isArray(fixturePath) ? fixturePath : [fixturePath]
  if (paths.length === 0) {
    throw new Error('mockBackend: at least one fixture path is required')
  }
  const fixture: Fixture = mergeFixtures(
    paths.map((p) => ({ path: p, fx: validateFixture(p, yamlLoad(readFileSync(p, 'utf-8'))) }))
  )

  // Pin the UI locale to English before the app boots. Flow specs assert
  // English copy, but the app defaults to Vietnamese (jarvis_lang), so without
  // this every text locator would miss once i18n landed. Runs on every
  // navigation, ahead of app scripts; specs that exercise the toggle itself
  // still change it at runtime after load.
  await page.addInitScript(() => {
    try {
      localStorage.setItem('jarvis_lang', 'en')
    } catch {
      /* storage unavailable in this context — ignore */
    }
  })

  const matchedCounts = new Map<string, number>()
  for (const key of Object.keys(fixture.responses)) matchedCounts.set(key, 0)

  const fulfilled: RequestRecord[] = []
  const unexpected: RequestRecord[] = []

  await page.route('**/api/**', async (route: Route) => {
    const req = route.request()
    const method = req.method().toUpperCase()
    const url = new URL(req.url())
    const path = url.pathname + url.search
    const key = `${method} ${url.pathname}`

    // SSE streams have their own matcher (streams keyed by path only, not method).
    const sseEvents = fixture.sse_streams?.[url.pathname]
    if (sseEvents) {
      await fulfillSSE(route, sseEvents)
      fulfilled.push({
        method,
        path,
        status: 200,
        matchedFixture: `SSE ${url.pathname}`,
      })
      return
    }

    const entry = fixture.responses[key]
    if (!entry) {
      const record: RequestRecord = {
        method,
        path,
        status: 599,
        matchedFixture: null,
      }
      unexpected.push(record)
      await route.fulfill({
        status: 599,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'mock_backend_miss',
          method,
          path: url.pathname,
          message:
            `No fixture entry for '${key}'. Add it to the fixture or check ` +
            `the component is calling the right endpoint.`,
          available_keys: Object.keys(fixture.responses),
        }),
      })
      return
    }

    // Sequential response support: when fixture declares an array, consume
    // entries in order and reuse the LAST one for any subsequent calls. The
    // count tracked here drives which entry to serve + whether the fixture
    // was exhaustively used.
    const hitCount = matchedCounts.get(key) ?? 0
    const match = Array.isArray(entry)
      ? entry[Math.min(hitCount, entry.length - 1)]
      : entry
    matchedCounts.set(key, hitCount + 1)
    await fulfillResponse(route, match)
    fulfilled.push({
      method,
      path,
      status: match.status ?? 200,
      matchedFixture: key,
    })
  })

  return {
    fulfilled,
    unexpected,
    matchedKeys: () =>
      [...matchedCounts.entries()].filter(([, n]) => n > 0).map(([k]) => k),
    assertAllFulfilled() {
      if (unexpected.length > 0) {
        const list = unexpected
          .map((r) => `  ${r.method} ${r.path}`)
          .join('\n')
        throw new Error(
          `Mock backend received ${unexpected.length} request(s) with no ` +
            `fixture match:\n${list}\n\n` +
            `Either add them to the fixture or fix the component.`
        )
      }
      const unused = [...matchedCounts.entries()]
        .filter(([, n]) => n === 0)
        .map(([k]) => k)
      if (unused.length > 0) {
        throw new Error(
          `Fixture declared ${unused.length} response(s) that were never ` +
            `requested:\n${unused.map((k) => `  ${k}`).join('\n')}\n\n` +
            `Remove them or fix the flow — dead fixtures drift silently.`
        )
      }
    },
  }
}

function mergeFixtures(
  items: Array<{ path: string; fx: Fixture }>
): Fixture {
  const merged: Fixture = {
    backend_source: [],
    responses: {},
    sse_streams: {},
  }
  for (const { fx } of items) {
    merged.backend_source.push(...fx.backend_source)
    Object.assign(merged.responses, fx.responses)
    if (fx.sse_streams) Object.assign(merged.sse_streams!, fx.sse_streams)
  }
  if (Object.keys(merged.sse_streams ?? {}).length === 0) delete merged.sse_streams
  return merged
}

async function fulfillResponse(route: Route, spec: FixtureResponse) {
  const status = spec.status ?? 200
  const headers = { ...(spec.headers ?? {}) }
  let body: string | Buffer = ''
  if (spec.json !== undefined) {
    headers['content-type'] ??= 'application/json'
    body = JSON.stringify(spec.json)
  } else if (spec.text !== undefined) {
    headers['content-type'] ??= 'text/plain; charset=utf-8'
    body = spec.text
  }
  await route.fulfill({ status, headers, body })
}

async function fulfillSSE(route: Route, events: SseEvent[]) {
  // Yield one macrotask before serving the SSE body. This lets the browser
  // schedule the parallel REST fetches (e.g. /api/agents) ahead of the
  // stream-open — without the yield, fulfillSSE can land first, Vue hydrates
  // with only the SSE-sourced agents, and the REST payload arrives *after*
  // the test's initial `toHaveCount` assertion has already fired. Observed
  // as intermittent 1-panel-instead-of-2 flake on slower machines.
  await new Promise<void>((resolve) => setTimeout(resolve, 30))

  // Build the full SSE body up front. Playwright's route.fulfill does NOT
  // support streaming bodies — clients process events as soon as the body
  // arrives (atomically). If a test ever needs true inter-event timing,
  // split the flow across multiple fixtures rather than adding a streaming
  // helper here.
  //
  // The leading `retry: <ms>` field sets the EventSource reconnect delay
  // for when this mocked response ends (browser sees EOF → onerror). Without
  // it, the client loops on the default ~3s which starves the CPU and races
  // test assertions. 3_600_000 ms (1h) parks the reconnect beyond any test's
  // lifetime — the stream stays effectively one-shot, which matches how
  // fixture-driven tests want it.
  let body = 'retry: 3600000\n\n'
  for (const ev of events) {
    if (ev.id) body += `id: ${ev.id}\n`
    if (ev.event) body += `event: ${ev.event}\n`
    const json =
      typeof ev.data === 'string' ? ev.data : JSON.stringify(ev.data)
    // SSE spec: data lines must not contain raw newlines; split if needed.
    for (const line of json.split('\n')) body += `data: ${line}\n`
    body += '\n'
  }
  body += 'event: mock_end\ndata: [DONE]\n\n'

  await route.fulfill({
    status: 200,
    headers: {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache',
      connection: 'keep-alive',
    },
    body,
  })
}
