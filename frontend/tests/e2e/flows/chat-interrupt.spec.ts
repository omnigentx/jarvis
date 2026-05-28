/**
 * Chat interrupt — Stop button while streaming.
 *
 * Production contract (cross-references the unit suite in
 * backend/tests/test_services/test_pause_controller.py):
 *
 *   1. While `isStreaming` is true, the Send button is replaced by a Stop
 *      button in the SAME slot (no layout shift) — `<button v-if="isStreaming"
 *      data-testid="chat-stop">` in ChatInput.vue.
 *   2. Plain click on Stop → useChatStream.cancel('soft') → fetch abort +
 *      POST `/api/agents/Jarvis/interrupt?mode=soft`. A toast surfaces
 *      "tools already called are not rolled back".
 *   3. Shift-click on Stop → useConfirm modal appears with destructive
 *      wording. "Force stop" confirms → POST `?mode=hard` + warning toast
 *      that names the killed subagents. "Keep running" cancels → NO POST.
 *   4. The chat-stream is held open via inline page.route() so we can
 *      reproduce the "user clicked stop mid-stream" scenario deterministically
 *      (the harness's fulfillSSE is atomic — see harness/mock-backend.ts:183).
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')
const INTERRUPT_FIXTURE = join(FIXTURES, 'chat_interrupt.yaml')

const SEND_BUTTON = '[data-testid="chat-send"]'
const STOP_BUTTON = '[data-testid="chat-stop"]'

/**
 * Hold the chat-stream request open. The returned `release()` lets a test
 * resolve the stream cleanly at teardown so Playwright doesn't log a
 * "request was aborted" noise. We never fulfill — the front-end's
 * AbortController in `cancel()` is what closes it.
 */
async function holdChatStreamOpen(page: import('@playwright/test').Page) {
  let release!: () => void
  const released = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/api/chat-stream', async (route) => {
    // Park the route indefinitely. The frontend will abort it when the
    // user clicks Stop; that's what we're testing.
    await released
    try {
      await route.fulfill({
        status: 200,
        headers: { 'content-type': 'text/event-stream' },
        body: 'retry: 3600000\n\nevent: mock_end\ndata: [DONE]\n\n',
      })
    } catch {
      // Route may already be aborted by the time we try to fulfill —
      // expected, ignore.
    }
  })
  return release
}

/** Bring the chat view into the streaming state and return helpers. */
async function startStreamingChat(page: import('@playwright/test').Page) {
  const release = await holdChatStreamOpen(page)
  await page.goto('/chat')

  const textarea = page.getByPlaceholder(/type a message/i)
  await expect(textarea).toBeVisible()
  await textarea.fill('please stop me')

  // Fire the POST without awaiting its response — the route is parked,
  // it never resolves. We need the click to happen, then the Stop button
  // to appear because isStreaming flips synchronously inside send().
  await textarea.press('Enter')

  // Stop button takes the Send slot once isStreaming flips. Use the
  // visibility transition as our "we are streaming" sync point.
  await expect(page.locator(STOP_BUTTON)).toBeVisible()
  await expect(page.locator(SEND_BUTTON)).toHaveCount(0)

  return { release }
}

test('soft stop: button swap → POST mode=soft → reminder toast', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [NOISE, INTERRUPT_FIXTURE])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  const { release } = await startStreamingChat(page)
  try {
    // Plain click — soft mode. No confirm modal: the action is
    // recoverable (no subagents are SIGTERMed in soft mode).
    const interruptPost = page.waitForResponse(
      (r) =>
        r.url().includes('/api/agents/Jarvis/interrupt') &&
        r.request().method() === 'POST',
    )
    await page.locator(STOP_BUTTON).click()
    const res = await interruptPost

    // Production contract: cancel('soft') resolves to mode=soft on the
    // query string. If anyone refactors the call into a body parameter,
    // the backend won't see ?mode=hard for shift-click either — wired
    // to fail loud here.
    const url = new URL(res.url())
    expect(url.searchParams.get('mode')).toBe('soft')

    // Toast contract: chat-store has no system-message API, we surface
    // via useToast.info(). Assert the visible text so a future refactor
    // back to chat-bubble injection breaks this rather than silently
    // disappearing.
    await expect(
      page.getByText(
        /Tools the agent already called.*are not rolled back/i,
      ),
    ).toBeVisible()

    // Button swaps back — isStreaming flipped to false by cancel().
    await expect(page.locator(SEND_BUTTON)).toBeVisible()
    await expect(page.locator(STOP_BUTTON)).toHaveCount(0)

    // Exactly one interrupt POST, exactly once.
    const interruptCalls = recorder.calls.filter(
      (c) =>
        c.method === 'POST' &&
        c.path.startsWith('/api/agents/Jarvis/interrupt'),
    )
    expect(interruptCalls).toHaveLength(1)
  } finally {
    release()
  }

  expect(backend.unexpected.length).toBe(0)
})

test('hard stop: shift-click → confirm modal → POST mode=hard → warning toast names killed subagents', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [NOISE, INTERRUPT_FIXTURE])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  const { release } = await startStreamingChat(page)
  try {
    // Shift-click opens useConfirm modal. Native Playwright modifier
    // syntax — same key Vue's @click.shift listener checks.
    await page.locator(STOP_BUTTON).click({ modifiers: ['Shift'] })

    // The modal must surface the destructive warning BEFORE any POST
    // fires — that's the whole point of the pre-action confirm. If the
    // POST raced ahead of the modal, this test catches it because we
    // assert the modal is visible and `interrupt` has zero hits so far.
    const modal = page.getByText(/Force stop — terminate subagents/i)
    await expect(modal).toBeVisible()
    expect(
      recorder.calls.filter(
        (c) =>
          c.method === 'POST' &&
          c.path.startsWith('/api/agents/Jarvis/interrupt'),
      ),
    ).toHaveLength(0)

    // Click Force stop. Mode is encoded in the query string — see soft
    // test for the rationale.
    const interruptPost = page.waitForResponse(
      (r) =>
        r.url().includes('/api/agents/Jarvis/interrupt?mode=hard') &&
        r.request().method() === 'POST',
    )
    await page.getByRole('button', { name: /Force stop/i }).click()
    const res = await interruptPost
    expect(new URL(res.url()).searchParams.get('mode')).toBe('hard')

    // Warning toast contract: names the subagents from the fixture
    // response so the user knows which ones were killed.
    await expect(
      page.getByText(/Force-stopped.*2 subagent\(s\) terminated/i),
    ).toBeVisible()
    await expect(page.getByText(/Minh - Dev/)).toBeVisible()
    await expect(page.getByText(/Linh - QE/)).toBeVisible()

    await expect(page.locator(SEND_BUTTON)).toBeVisible()
  } finally {
    release()
  }

  expect(backend.unexpected.length).toBe(0)
})

test('hard stop cancelled: clicking "Keep running" fires NO interrupt', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [NOISE, INTERRUPT_FIXTURE])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  const { release } = await startStreamingChat(page)
  try {
    await page.locator(STOP_BUTTON).click({ modifiers: ['Shift'] })

    const modal = page.getByText(/Force stop — terminate subagents/i)
    await expect(modal).toBeVisible()

    await page.getByRole('button', { name: /Keep running/i }).click()
    await expect(modal).toBeHidden()

    // Negative assertion — give the page a beat to ensure no late POST.
    // 250ms is enough: ChatView.handleStop runs synchronously after the
    // confirm Promise resolves. If a hypothetical future bug added a
    // setTimeout-delayed cancel, this catches the race.
    await page.waitForTimeout(250)

    const interruptCalls = recorder.calls.filter(
      (c) =>
        c.method === 'POST' &&
        c.path.startsWith('/api/agents/Jarvis/interrupt'),
    )
    expect(interruptCalls).toHaveLength(0)

    // Stream is still alive (we never released the held route). Stop
    // button must still be present — `cancel` was never called.
    await expect(page.locator(STOP_BUTTON)).toBeVisible()
  } finally {
    release()
  }

  expect(backend.unexpected.length).toBe(0)
})
