/**
 * Realtime "memory saved" chip — when Jarvis stores (auto-approved) or proposes
 * (pending approval) a memory, the user SEES it in context via the live
 * `memory_saved` activity-stream SSE, with inline undo / approve / reject —
 * instead of only discovering it later in the Memory tab.
 *
 * Production contract: candidate_service._emit_saved broadcasts `memory_saved`
 * (status saved | pending | rejected); useRealtimeStream → agents.processEvent →
 * chat.addMemorySavedBlock inserts/updates the chip; ChatMessages renders it.
 *
 * Mirrors chat-memory-recall.spec: chat-stream held open so the conversation is
 * active; activity-stream delivers the saved events once the turn is in flight.
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

test('memory_saved paints the chip mid-turn with inline actions', async ({ page }) => {
  const backend = await mockBackend(page, [NOISE, join(FIXTURES, 'chat_memory_recall.yaml')])

  let onChatStarted!: () => void
  const chatStarted = new Promise<void>((r) => { onChatStarted = r })
  let releaseChat: (() => void) | null = null

  // Resolves when the inline "Approve" click POSTs to the candidate route.
  let onApproved!: () => void
  const approved = new Promise<void>((r) => { onApproved = r })

  await page.route('**/api/chat-stream', async (route) => {
    onChatStarted()
    await new Promise<void>((r) => { releaseChat = r }) // keep the conversation active
    await route.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: 'retry: 3600000\n\ndata: {"type":"start","message":"…"}\n\n',
    })
  })

  // Two saved events in one body: one AUTO-saved, one PENDING approval.
  await page.route('**/api/agents/activity-stream**', async (route) => {
    await chatStarted
    const ev = (data: object) => ({ agent_name: 'Jarvis', event_type: 'memory_saved', data })
    const saved = ev({
      candidate_id: 'c1', record_id: 'r1', status: 'saved',
      memory_type: 'semantic', content: 'Phúc là chủ nhân của OmnigentX Jarvis', sensitive: false,
    })
    const pending = ev({
      candidate_id: 'c2', record_id: null, status: 'pending',
      memory_type: 'semantic', content: 'số thẻ ngân hàng của tôi', sensitive: false,
    })
    await route.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: `retry: 3600000\n\ndata: ${JSON.stringify(saved)}\n\ndata: ${JSON.stringify(pending)}\n\n`,
    })
  })

  // Inline approve → real POST to the candidate approve route (assert it fires).
  await page.route('**/api/agents/Jarvis/memory-candidates/c2/approve', async (route) => {
    onApproved()
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"approved"}' })
  })

  await page.goto('/chat')

  const textarea = page.getByPlaceholder(/type a message/i)
  await expect(textarea).toBeVisible()
  await textarea.fill('tôi tên Phúc, đây là số thẻ của tôi')
  await textarea.press('Enter')

  // Chip paints live. A mixed batch (1 saved + 1 pending) takes the warning
  // (pending) tint — `chip-pending` — and keeps the 🧠 memory motif.
  const messages = page.getByTestId('chat-messages')
  const chip = messages.locator('.memory-chip.chip-pending')
  await expect(chip).toBeVisible()
  await expect(chip).toContainText('🧠')

  // Expand → both memories + their per-state actions (design-system .btn pattern:
  // approve = primary, undo/reject = ghost).
  await chip.click()
  await expect(messages.getByText(/Phúc là chủ nhân của OmnigentX/i)).toBeVisible()
  await expect(messages.locator('.s-btn.ghost').first()).toBeVisible()  // auto-saved → undo
  const approveBtn = messages.locator('.s-btn.primary')
  await expect(approveBtn).toBeVisible()                                // pending → approve

  // Inline approve hits the real candidate route (approve-in-context).
  await approveBtn.click()
  await approved

  releaseChat?.()
  expect(backend.unexpected.length).toBe(0)
})
