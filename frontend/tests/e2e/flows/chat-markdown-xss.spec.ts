/**
 * MarkdownRenderer XSS regression.
 *
 * The `decorateCodeBlocks` helper used to inject the code-fence language
 * specifier into its header via `header.innerHTML = ...${lang}...`. marked
 * accepts arbitrary chars in the info-string after ``` so a crafted fence
 * could smuggle `<img onerror=...>` past DOMPurify and into live markup.
 *
 * Fix: build the chrome header with createElement + textContent. This spec
 * pins that fix by feeding the malicious markdown through chat and asserting:
 *   1. No <img> with the attacker src reaches the DOM
 *   2. window.xssTriggered remains unset (script didn't execute)
 *   3. The literal lang string appears in .code-chrome__label as text
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('markdown renderer does not execute attacker JS in code-fence lang', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'chat_streaming_xss.yaml'),
  ])

  // Trip-wire: if any earlier-running script ran via innerHTML injection
  // it would have set this flag before this initScript fires. The init
  // ordering is safe because Playwright runs the script BEFORE the
  // navigation happens, so the property is guaranteed false at page open.
  await page.addInitScript(() => {
    ;(window as any).xssTriggered = false
  })

  await page.goto('/chat')
  const textarea = page.getByPlaceholder(/type a message/i)
  await expect(textarea).toBeVisible()

  const streamResponse = page.waitForResponse(
    (r) => r.url().endsWith('/api/chat-stream') && r.request().method() === 'POST',
  )
  await textarea.fill('emit malicious markdown')
  await textarea.press('Enter')
  await streamResponse

  const messagesArea = page.getByTestId('chat-messages')
  // Wait for the code chrome to mount — the renderer's post-process step
  // adds .code-chrome around every <pre><code>. That's our signal the
  // sanitised content is on screen.
  await expect(messagesArea.locator('.code-chrome').first()).toBeVisible()

  // Trip-wire never triggered — the onerror= handler did not run.
  await expect.poll(() => page.evaluate(() => (window as any).xssTriggered)).toBe(false)

  // No <img> element with the attacker src materialised anywhere on the page.
  // (DOMPurify already strips standalone <img onerror=> at the markdown
  // sanitise step; this spec specifically guards the post-sanitise
  // decorateCodeBlocks path.)
  await expect(page.locator('img[src="x"]')).toHaveCount(0)

  // The lang string lands inside .code-chrome__label as text, including the
  // literal characters that *would* have been an HTML img tag. This is the
  // positive signal that the textContent path is in play.
  const label = messagesArea.locator('.code-chrome__label').first()
  await expect(label).toHaveText(/javascript<img/)
})
