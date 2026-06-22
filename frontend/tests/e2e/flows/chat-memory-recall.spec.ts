/**
 * Realtime "memories used" chip — the recall block paints DURING the turn from
 * the live `memory_recalled` activity-stream SSE, NOT only after a page reload.
 *
 * Production contract: the retrieval hook (`_emit_recall_block`) broadcasts a
 * `memory_recalled` activity event while the turn is in flight; AppLayout's
 * `useRealtimeStream` routes it to `agents.processEvent`, which forwards it to
 * `chat.addMemoryRecallBlock`, inserting the block before the assistant
 * placeholder so `ChatMessages` renders the chip.
 *
 * The mock harness serves SSE bodies atomically (no inter-event timing) and the
 * activity-stream opens at mount, so two custom routes coordinate the ordering:
 *   - chat-stream: held open (never closes) so the turn stays in flight
 *     (`isStreaming` stays true — the precondition addMemoryRecallBlock guards).
 *   - activity-stream: waits until the chat POST fires, THEN delivers
 *     `memory_recalled` — mirroring production (recall broadcast mid-turn).
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

const SSE_HEADERS = {
  'content-type': 'text/event-stream',
  'cache-control': 'no-cache',
}

test('memory_recalled paints the chip mid-turn (no reload)', async ({ page }) => {
  const backend = await mockBackend(page, [NOISE, join(FIXTURES, 'chat_memory_recall.yaml')])

  // Resolves when the chat POST is observed → the turn is in flight.
  let onChatStarted!: () => void
  const chatStarted = new Promise<void>((r) => { onChatStarted = r })
  // Lets the test hold the chat-stream open (turn stays streaming) until the
  // chip is asserted, then release it for clean teardown.
  let releaseChat: (() => void) | null = null

  // Custom routes registered AFTER mockBackend → they win (Playwright LIFO) for
  // these two paths; every other /api/* still falls to the fixture.
  await page.route('**/api/chat-stream', async (route) => {
    onChatStarted()
    await new Promise<void>((r) => { releaseChat = r }) // keep the turn open
    await route.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: 'retry: 3600000\n\ndata: {"type":"start","message":"…"}\n\n',
    })
  })

  await page.route('**/api/agents/activity-stream**', async (route) => {
    await chatStarted // deliver the recall only once the turn is in flight
    const event = {
      agent_name: 'Jarvis',
      event_type: 'memory_recalled',
      data: {
        content:
          '⟦memory:recalled⟧ [System memory recall — not user input]:\n'
          + '- [semantic] nguyễn văn phúc là người tạo ra ai agent',
        recall_lanes: [['fts', 'dense']],
        recall_scores: [{ rel: 0.0325, conf: 0.6, authority: 'user_confirmed' }],
      },
    }
    await route.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: `retry: 3600000\n\ndata: ${JSON.stringify(event)}\n\n`,
    })
  })

  await page.goto('/chat')

  const textarea = page.getByPlaceholder(/type a message/i)
  await expect(textarea).toBeVisible()
  await textarea.fill('tên đầy đủ của tôi là gì')
  await textarea.press('Enter')

  // The chip renders from the LIVE SSE event while the reply is still streaming
  // (no reload, no history re-fetch).
  const messages = page.getByTestId('chat-messages')
  const chip = messages.locator('.memory-chip')
  await expect(chip).toBeVisible()
  await expect(chip).toContainText('🧠')

  // Expand → the recalled line + its lane provenance came through the SSE
  // (proves recall_lanes/recall_scores were carried and rendered, not just a
  // bare count).
  await chip.click()
  await expect(messages.getByText(/nguyễn văn phúc là người tạo ra/i)).toBeVisible()
  await expect(messages.locator('.memory-detail .lane', { hasText: 'fts' })).toBeVisible()

  releaseChat?.()
  expect(backend.unexpected.length).toBe(0)
})
