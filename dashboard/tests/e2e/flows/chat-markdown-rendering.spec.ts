/**
 * Chat markdown + mermaid rendering — guards the upgrade in
 * `feat/md-mermaid-tts-scoping`.
 *
 * Why these specific assertions:
 *  - The chat bubble used to render plain text. After this PR it routes the
 *    agent reply through `MarkdownRenderer.vue`. We verify that headings and
 *    bold are produced as real DOM elements (not literal `#` / `**`), which
 *    is the only signal that the renderer actually ran.
 *  - Mermaid is loaded lazily (only when a ```mermaid block is present), so
 *    we wait for the first `<svg>` to appear rather than asserting a chunk
 *    name. If the dynamic import path breaks, this assertion fails loudly
 *    instead of silently leaving a code block on screen.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('agent reply renders as Markdown — heading, bold, list, code', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'chat_streaming_markdown.yaml'),
  ])

  await page.goto('/chat')
  const textarea = page.getByPlaceholder(/type a message/i)
  await expect(textarea).toBeVisible()

  const streamResponse = page.waitForResponse(
    (r) => r.url().endsWith('/api/chat-stream') && r.request().method() === 'POST'
  )
  await textarea.fill('plan a sprint')
  await textarea.press('Enter')
  await streamResponse

  const messagesArea = page.getByTestId('chat-messages')

  // Heading must be a real <h1>, not the literal text "# Sprint plan".
  // Scoping to the messages area avoids picking up any nav/section headers.
  const heading = messagesArea.locator('h1', { hasText: 'Sprint plan' })
  await expect(heading).toBeVisible()

  // Bold must render as a <strong>, again scoped so nothing else with the
  // same word can satisfy the assertion.
  await expect(messagesArea.locator('strong', { hasText: 'two phases' })).toBeVisible()

  // The literal markdown syntax must NOT leak through.
  await expect(messagesArea.getByText(/^# Sprint plan$/m)).toHaveCount(0)
  await expect(messagesArea.getByText('**two phases**', { exact: false })).toHaveCount(0)

  // Inline code → <code>npm test</code>
  await expect(messagesArea.locator('code', { hasText: 'npm test' })).toBeVisible()

  expect(backend.unexpected.length).toBe(0)
})

test('mermaid fenced block becomes an inline SVG diagram', async ({ page }) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'chat_streaming_markdown.yaml'),
  ])

  await page.goto('/chat')
  const textarea = page.getByPlaceholder(/type a message/i)
  await expect(textarea).toBeVisible()

  const streamResponse = page.waitForResponse(
    (r) => r.url().endsWith('/api/chat-stream') && r.request().method() === 'POST'
  )
  await textarea.fill('plan a sprint')
  await textarea.press('Enter')
  await streamResponse

  const messagesArea = page.getByTestId('chat-messages')

  // The MarkdownRenderer emits a <div class="md-mermaid-block"> placeholder
  // whose textContent is the base64-encoded source. After the lazy-loaded
  // mermaid module renders, the inner HTML is replaced with an <svg> — its
  // presence is the signal that the dynamic import path actually fired.
  const svg = messagesArea.locator('.md-mermaid-block svg')
  await expect(svg).toBeVisible({ timeout: 10_000 })

  // Source labels must show up inside the SVG so we know it parsed our
  // flowchart and didn't fall back to an error block.
  await expect(messagesArea.locator('.md-mermaid-block')).toContainText('Backend ready')
  await expect(messagesArea.locator('.md-mermaid-block')).toContainText('Ship')

  // The triple-backtick fence must NOT remain in the rendered DOM.
  await expect(messagesArea.getByText('```mermaid', { exact: false })).toHaveCount(0)

  expect(backend.unexpected.length).toBe(0)
})
