# Dashboard E2E Test Harness

> Playwright + contract-anchored mocked backend. Deterministic, fail-loud.
> Same philosophy as `backend/tests/e2e/`: real code paths, scripted responses.

## Run

```bash
npm run test:e2e           # mocked-backend flow tests (fast, default)
npm run test:e2e -- --ui   # interactive debugger
PW_RECORD=1 npm run test:e2e   # (future) record against real backend
PW_SMOKE=1 npm run test:e2e    # (future) smoke against real backend
```

Trace on failure: `npx playwright show-trace playwright-report/trace.zip`.

## Anchoring mock to real backend — 3 rules

1. **Every fixture has `backend_source`** — a list of `backend/path/file.py::function` citations proving the fixture mirrors real code. Reviewers verify these; an AI adding a fixture MUST read the backend file first.
2. **No invented endpoints**. If the dashboard doesn't call it in production, it doesn't belong in a fixture. If a flow needs a new endpoint, add it to the backend first.
3. **Noise stays in `_app_boot_noise.yaml`** — boot-time side-effect calls (notifications badge, activity SSE, scheduler SSE) are shared. Flow fixtures only declare the endpoints the flow itself exercises.

## Fixture schema

```yaml
backend_source:          # REQUIRED — citations proving fidelity
  - "backend/routes/setup.py::get_setup_status"
  - "dashboard/src/stores/setup.js::fetchStatus"

notes: "free-form reviewer notes (optional)"

responses:
  "GET /api/setup/status":
    status: 200           # optional, defaults to 200
    json: { ... }         # OR
    text: "raw"           # (exactly one of json/text)
    headers: { ... }      # optional

sse_streams:
  "/api/chat/stream":
    - { event: "token_delta", data: { text: "hello" } }
    - { event: "end", data: {} }
```

Schema validated at load time — unknown keys, missing `backend_source`, or bad method patterns throw a clear error before the test runs.

## Adding a new E2E test — 3 steps

### 1. Author the fixture

```yaml
# tests/e2e/fixtures/my_flow.yaml
backend_source:
  - "backend/routes/foo.py::bar_endpoint"

responses:
  "GET /api/foo": { json: { items: [] } }
  "POST /api/foo": { json: { ok: true } }
```

### 2. Write the spec

```ts
// tests/e2e/flows/my-flow.spec.ts
import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('my flow — happy path', async ({ page }) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [NOISE, join(FIXTURES, 'my_flow.yaml')])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/my-route')
  await page.getByRole('button', { name: /save/i }).click()

  recorder.assertContains('POST', '/api/foo')
  expect(backend.unexpected.length).toBe(0)
})
```

### 3. Verify locally

```bash
npx playwright test tests/e2e/flows/my-flow.spec.ts --reporter=list
```

## The five rules

1. **Real production component, real composables.** Do not stub the thing you're protecting. Mock only the HTTP boundary.
2. **Assert the specific contract.** Request body field by name, response shape by key, URL by path. `await expect(foo).toBeTruthy()` is smoke, not regression.
3. **Add a negative control when behavior is conditional.** If the flow branches on backend state, write a second test covering the OTHER branch — catches UI that silently picks one branch regardless.
4. **Fail loud on silent errors.** Empty JSON arrays, missing keys, and 200-status error bodies must make the test fail.
5. **One fixture = one flow.** Do not pile multiple scenarios into a single YAML. Small files diff cleanly.

## Locator discipline

| ✅ Stable | ❌ Fragile |
|---|---|
| `#new-key` (id attribute) | `.wizard-input` (class, likely to be refactored) |
| `getByRole('button', { name: /continue/i })` | `getByText('Continue')` (breaks on i18n) |
| `getByLabel('Master API Key')` | `nth(2)` positional child |
| `data-testid="chat-send"` (add when needed) | raw CSS selectors from devtools |

No `waitForTimeout` ever. Use `expect.poll(...)`, `expect(locator).toBeVisible()`, or `page.waitForResponse(...)` instead.

## Anti-patterns

- **Asserting on fixture values** — `expect(text).toBe(fixture.turns[0].content)` is tautological.
- **Tolerating unexpected requests** — every `backend.unexpected.length === 0` assertion is load-bearing; don't skip it.
- **Testing against real network by accident** — if a test intermittently fails with timeouts, you've forgotten `mockBackend`.
- **Catching `waitForTimeout` in review** — flaky tests mask real bugs. If you can't find the right `waitFor*`, the assertion is wrong, not the timing.
- **Mixing flows in one fixture** — if you need setup state X for one test and state Y for another, write two fixtures.

## Checklist for AI agents creating a test

- [ ] Read the backend route/service code first; cite it in `backend_source`.
- [ ] Read the dashboard view/composable; note which endpoints it fires on mount vs. on-interaction.
- [ ] Fixture file in `fixtures/<flow_name>.yaml`; flow spec in `flows/<flow-name>.spec.ts`.
- [ ] Include `_app_boot_noise.yaml` in every `mockBackend([NOISE, FLOW])`.
- [ ] Locators use id / role / testid — no `.classname`, no `nth(i)`.
- [ ] Every `mockBackend` call is followed by an assertion that `unexpected.length === 0`.
- [ ] Add at least one negative control when the flow has conditional logic.
- [ ] NO `waitForTimeout`. Use `expect.poll` or `page.waitForResponse` instead.
- [ ] Run `npx playwright test tests/e2e/flows/<file>.spec.ts` and confirm pass.
- [ ] Trace artifact inspected once — confirm assertions fire on the DOM state you expect.
- [ ] No test imports from `harness/*.ts` directly — use `../harness` barrel.
- [ ] If you added endpoints to the fixture, verify each was consumed (`matchedKeys` returns them).

## Adding a new endpoint/SSE stream across the app

When a feature adds a new `/api/*` endpoint:

1. Add the response to the most relevant existing flow fixture (or create a new one).
2. If it's a boot-time side effect present on every page, put it in `_app_boot_noise.yaml`.
3. The next test run will fail loud if any existing flow now hits it unexpectedly — that's the signal you need to update fixtures.

## Gaps to fill (tracked follow-up)

- **Record mode** (`PW_RECORD=1`) — proxy to real backend (docker-compose with playback LLM) + auto-write fixture. Currently fixtures are hand-crafted from reading backend code.
- **Smoke mode** (`PW_SMOKE=1`) — 2-3 tests against real backend for full-stack verification.
- **Contract-verify CI nightly** — replay all fixtures against real backend, diff, fail on drift. Catches intentional backend changes we forgot to reflect.
- **Visual regression** — screenshot diffing for critical views (setup, chat, agent detail).
- **a11y snapshot** — axe-core run on every route in at least one test.

## Harness reference

Everything exported from `tests/e2e/harness`:

| Export | Purpose |
|---|---|
| `mockBackend(page, fixture\|fixtures[])` | Intercept /api/* with fixture(s). Later fixtures win on key conflicts. Returns `{ fulfilled, unexpected, assertAllFulfilled(), matchedKeys() }`. |
| `seedApiKey(page, key?)` | Inject X-API-Key into localStorage before navigation. Default `test-api-key-e2e`. |
| `clearApiKey(page)` | Inject removal — for unauthenticated-state tests. |
| `NetworkRecorder` | Attach with `await rec.attach(page)`. Use `assertSequence([...])` for strict ordering, `assertContains(method, path)` for loose. |

SSE streams are served via the fixture's `sse_streams` field (atomic body delivery with a leading `retry: 3600000` + 30 ms macrotask yield to let REST win the hydration race). Playwright's Route API can't stream in real time; if a test ever needs true inter-event delays, split the flow across fixtures instead.
