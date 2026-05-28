/**
 * Chat YouTube embed — verifies that a [[[PLAY: <id>]]] tag in an agent's
 * streamed `done` payload triggers an embedded YouTube iframe in the chat
 * bubble, and that the literal tag is hidden from the displayed text.
 *
 * Why this matters: backend's media_server tool (and the music-playback
 * skill) hard-require the LLM to emit the tag verbatim. Dropping it
 * silently on the frontend = user sees raw `[[[PLAY: ...]]]` text and no
 * video — exactly the regression the Flutter parity audit flagged when we
 * decided to retire the Flutter app.
 *
 * Coverage:
 *  1. Happy path — tag at end of message → iframe with correct src renders;
 *     literal tag is not visible; cleaned bubble text is visible.
 *  2. Network safety — embed src points at youtube-nocookie.com (privacy
 *     variant), and is set with the correct video id.
 *
 * The fixture (`chat_streaming_youtube.yaml`) carries a stable test id —
 * `dQw4w9WgXcQ` — that doubles as a recognizable smoke probe in traces.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

const VIDEO_ID = 'dQw4w9WgXcQ'
// ``autoplay=1`` lets the song start without an extra click after the
// agent's response — the user already pressed Enter on the chat
// composer, which browsers count as the gesture that unlocks
// sound-autoplay. ``rel=0`` keeps the post-playback recommendation
// thumbnails within the same channel. Pin both flags here so a future
// refactor that drops either is a visible diff.
const EXPECTED_SRC = `https://www.youtube-nocookie.com/embed/${VIDEO_ID}?autoplay=1&rel=0`

test('PLAY tag → YouTube iframe renders, literal tag is hidden', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'chat_streaming_youtube.yaml'),
  ])

  await page.goto('/chat')

  const textarea = page.getByPlaceholder(/type a message/i)
  await expect(textarea).toBeVisible()

  await textarea.fill('play never gonna give you up')

  const streamResponse = page.waitForResponse(
    (r) => r.url().endsWith('/api/chat-stream') && r.request().method() === 'POST'
  )
  await textarea.press('Enter')
  await streamResponse

  const messagesArea = page.getByTestId('chat-messages')

  // Assertion 1: iframe shows up with the right src.
  // We scope to the messages area so we don't accidentally pick up some
  // unrelated iframe (none exists today, but scoping guards future regressions).
  const embed = messagesArea.getByTestId('chat-youtube-embed')
  await expect(embed).toBeVisible()

  const iframe = embed.locator('iframe')
  await expect(iframe).toHaveAttribute('src', EXPECTED_SRC)
  await expect(iframe).toHaveAttribute('data-video-id', VIDEO_ID)

  // Assertion 2: the cleaned bubble text is visible (Vietnamese phrase
  // before the tag), and the raw tag is NOT visible anywhere.
  await expect(
    messagesArea.getByText('Đang phát Never Gonna Give You Up.', {
      exact: true,
    })
  ).toBeVisible()

  // Negative assertion: nowhere on the page (bubble + sidebar preview)
  // should the literal tag remain. If the parser regex breaks, or a new
  // surface forgets to call it, this fires loudly. The video id itself is
  // only inside iframe attributes — not as visible text — so we don't
  // assert on its visibility.
  await expect(page.getByText('[[[PLAY:', { exact: false })).toHaveCount(0)

  expect(backend.unexpected.length).toBe(0)
})

test('messages without PLAY tag render no iframe (negative control)', async ({
  page,
}) => {
  // Reuse the existing happy-path fixture — its `done.response` is plain
  // text. If a refactor accidentally renders an iframe for every message,
  // this catches it.
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'chat_streaming_happy.yaml'),
  ])

  await page.goto('/chat')

  const textarea = page.getByPlaceholder(/type a message/i)
  await textarea.fill('hello')
  const streamResponse = page.waitForResponse(
    (r) => r.url().endsWith('/api/chat-stream') && r.request().method() === 'POST'
  )
  await textarea.press('Enter')
  await streamResponse

  const messagesArea = page.getByTestId('chat-messages')
  await expect(
    messagesArea.getByText('Hello from Jarvis — streamed response.', { exact: true })
  ).toBeVisible()

  // No embed should appear.
  await expect(messagesArea.getByTestId('chat-youtube-embed')).toHaveCount(0)

  expect(backend.unexpected.length).toBe(0)
})
