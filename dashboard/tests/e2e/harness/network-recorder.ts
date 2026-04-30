/**
 * NetworkRecorder — capture every /api/* request the dashboard makes,
 * assert the ordered sequence matches expected. Parallel to backend's
 * ToolCallRecorder.
 *
 * Strict matching: `assertSequence([...expected])` compares method+path+body
 * tuples in order; any divergence fails with a clear diff.
 */

import type { Page, Request as PwRequest } from '@playwright/test'

export type RecordedRequest = {
  method: string
  path: string
  body: unknown
  query: Record<string, string>
}

type Matcher =
  | [method: string, path: string]
  | [method: string, path: string, bodyPredicate: (b: unknown) => boolean]

export class NetworkRecorder {
  readonly calls: RecordedRequest[] = []

  constructor(private readonly baseApiPrefix = '/api/') {}

  async attach(page: Page): Promise<void> {
    page.on('request', (req: PwRequest) => {
      const url = new URL(req.url())
      if (!url.pathname.startsWith(this.baseApiPrefix)) return
      let body: unknown = null
      const raw = req.postData()
      if (raw) {
        try {
          body = JSON.parse(raw)
        } catch {
          body = raw
        }
      }
      const query: Record<string, string> = {}
      for (const [k, v] of url.searchParams) query[k] = v
      this.calls.push({
        method: req.method().toUpperCase(),
        path: url.pathname,
        body,
        query,
      })
    })
  }

  /**
   * Strict in-order match. Pass pairs `[method, path]` or triples with a
   * body predicate when you want to assert request bodies flexibly.
   */
  assertSequence(expected: Matcher[]): void {
    const actualPairs = this.calls.map((c) => [c.method, c.path] as const)
    const expectedPairs = expected.map((m) => [m[0], m[1]] as const)
    if (
      actualPairs.length !== expectedPairs.length ||
      actualPairs.some(
        ([m, p], i) => m !== expectedPairs[i][0] || p !== expectedPairs[i][1]
      )
    ) {
      throw new Error(
        'NetworkRecorder sequence mismatch.\n' +
          `  expected: ${JSON.stringify(expectedPairs)}\n` +
          `  actual:   ${JSON.stringify(actualPairs)}`
      )
    }
    expected.forEach((m, i) => {
      if (m.length === 3) {
        const ok = m[2](this.calls[i].body)
        if (!ok) {
          throw new Error(
            `NetworkRecorder body predicate failed at index ${i} ` +
              `for ${m[0]} ${m[1]}:\n  actual body: ${JSON.stringify(this.calls[i].body)}`
          )
        }
      }
    })
  }

  assertContains(method: string, path: string): RecordedRequest {
    const hit = this.calls.find(
      (c) => c.method === method.toUpperCase() && c.path === path
    )
    if (!hit) {
      throw new Error(
        `Expected request ${method} ${path} never fired. ` +
          `Captured: ${JSON.stringify(this.calls.map((c) => `${c.method} ${c.path}`))}`
      )
    }
    return hit
  }

  reset(): void {
    this.calls.length = 0
  }
}
