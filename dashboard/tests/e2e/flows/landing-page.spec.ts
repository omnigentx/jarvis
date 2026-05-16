import { expect, test } from '@playwright/test'

test.describe('Jarvis public landing page', () => {
  test('loads /landing without showing the authentication gate', async ({ page }) => {
    await page.goto('/landing')

    await expect(
      page.getByRole('heading', {
        name: /Jarvis — a self-hostable AI assistant that coordinates an expert agent team/i,
      }),
    ).toBeVisible()
    await expect(page.getByRole('link', { name: /Launch Jarvis/i }).first()).toBeVisible()
    await expect(page.getByRole('link', { name: /View GitHub/i }).first()).toBeVisible()
    await expect(page.getByText(/API key/i)).toHaveCount(0)
  })

  test('exposes required sections and Phase 1 canonical metadata', async ({ page }) => {
    await page.goto('/landing')

    for (const heading of [
      'An AI team connected to real tools',
      'From prompt to coordinated delivery',
      'Built for end-to-end operational work',
      'Transparent, self-hostable, and MCP-first',
      'Run Jarvis under your control',
      'Questions before you launch',
    ]) {
      await expect(page.getByRole('heading', { name: heading })).toBeVisible()
    }

    await expect(page.locator('link[rel="canonical"]')).toHaveAttribute(
      'href',
      'https://jarvis.omnigentx.com/landing',
    )
  })

  test('preserves the existing root redirect behavior for Phase 1', async ({ page }) => {
    await page.goto('/')
    await expect(page).toHaveURL(/\/agents$/)
  })
})
