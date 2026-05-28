/**
 * Chat SSE streaming — user sends a message and the assistant reply is
 * streamed back via POST /api/chat-stream.
 *
 * Coverage:
 *  1. Happy path  — tool_call → tool_result → done. Asserts the final response
 *     text renders and the POST body carries the user's message.
 *  2. Error path  — one progress event then `error`. Asserts the UI surfaces
 *     the error message instead of silently freezing on the typing indicator.
 *  3. Negative control — pressing send with an empty input fires NO POST,
 *     guarding against a refactor that accidentally sends blank messages.
 *
 * Production contract: useChatStream POSTs JSON {message, conversation_id,
 * agent_name} and dispatches SSE `data: {json}` events by `event.type`. Only
 * `done` and `error` are terminal; `done.response` becomes the final bubble.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

// Stable selector via explicit test id added on the send button in
// components/chat/ChatInput.vue. Also has aria-label="Send message" for a11y.
const SEND_BUTTON = '[data-testid="chat-send"]'

test('happy path: message streams back and final response renders', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'chat_streaming_happy.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/chat')

  const textarea = page.getByPlaceholder(/type a message/i)
  await expect(textarea).toBeVisible()

  const USER_MSG = 'hello jarvis, are you streaming?'
  await textarea.fill(USER_MSG)

  // Fire the POST + await its response so we can assert on the streamed
  // body deterministically (no arbitrary timeouts).
  const streamResponse = page.waitForResponse(
    (r) => r.url().endsWith('/api/chat-stream') && r.request().method() === 'POST'
  )
  await textarea.press('Enter')
  await streamResponse

  // Assertion 1: POST body carries the user's message + null conversation_id
  // (fresh conversation) + agent_name resolved to Jarvis via watcher.
  const chatCall = recorder.assertContains('POST', '/api/chat-stream')
  const body = chatCall.body as { message?: string; conversation_id?: string | null; agent_name?: string | null }
  expect(body.message).toBe(USER_MSG)
  expect(body.conversation_id).toBeNull()
  expect(body.agent_name).toBe('Jarvis')

  // Assertion 2: both surfaces render explicitly by name — scoped locators
  // instead of counting global matches. This avoids a brittle dependency
  // on the sidebar's truncation threshold (long message → only bubble
  // matches `exact: true`; short message → bubble + sidebar both match).
  //
  // Contract: final assistant text goes into the chat bubble; user echo
  // goes into BOTH the bubble and the sidebar preview. Each scope gets
  // its own assertion — if either disappears, the failure names which.
  const messagesArea = page.getByTestId('chat-messages')
  const sidebar = page.getByTestId('conversations-panel')

  await expect(
    messagesArea.getByText('Hello from Jarvis — streamed response.', { exact: true })
  ).toBeVisible()

  await expect(messagesArea.getByText(USER_MSG, { exact: true })).toBeVisible()
  await expect(sidebar.getByText(USER_MSG, { exact: true })).toBeVisible()

  expect(backend.unexpected.length).toBe(0)
})

test('error mid-stream: UI surfaces the error, does not freeze', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'chat_streaming_error.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/chat')

  const textarea = page.getByPlaceholder(/type a message/i)
  await expect(textarea).toBeVisible()

  await textarea.fill('trigger an error please')

  const streamResponse = page.waitForResponse(
    (r) => r.url().endsWith('/api/chat-stream') && r.request().method() === 'POST'
  )
  await textarea.press('Enter')
  await streamResponse

  recorder.assertContains('POST', '/api/chat-stream')

  // setMessageError writes event.message into msg.content and flips isError.
  // Assert the error text is visible in the chat bubble (exact match avoids
  // colliding with the activity-feed echo that prefixes "Jarvis: ...").
  await expect(
    page.getByText('Agent crashed: upstream model returned 500', { exact: true })
  ).toBeVisible()

  expect(backend.unexpected.length).toBe(0)
})

test('negative control: clicking send with empty input fires no request', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'chat_streaming_happy.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/chat')

  const textarea = page.getByPlaceholder(/type a message/i)
  await expect(textarea).toBeVisible()

  // Wait for boot calls to settle so the subsequent "no chat-stream" assertion
  // is meaningful. Poll the recorder — the /api/agents GET fires on mount and
  // may have resolved before any waitForResponse predicate could attach.
  await expect
    .poll(() =>
      recorder.calls.some(
        (c) => c.method === 'GET' && c.path === '/api/agents'
      )
    )
    .toBe(true)

  const sendButton = page.locator(SEND_BUTTON)
  await expect(sendButton).toBeVisible()

  // Click send with empty textarea. handleSend short-circuits because
  // `!text && !files.length` — NO POST should fire.
  await sendButton.click()

  // A negative assertion needs a positive signal to know the app is idle.
  // The recorded calls at this point must not contain chat-stream.
  const chatStreamCalls = recorder.calls.filter(
    (c) => c.method === 'POST' && c.path === '/api/chat-stream'
  )
  expect(chatStreamCalls).toHaveLength(0)

  // No unexpected backend hits either (the boot endpoints are all covered).
  expect(backend.unexpected.length).toBe(0)
})
