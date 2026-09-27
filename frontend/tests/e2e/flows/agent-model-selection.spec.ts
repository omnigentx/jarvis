import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')

test('agent model UI applies, resets, and preserves state on canary failure', async ({ page }, testInfo) => {
  await page.addInitScript(() => localStorage.setItem('jarvis_lang', 'en'))
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    join(FIXTURES, '_app_boot_noise.yaml'),
    join(FIXTURES, 'agent_model_selection.yaml'),
  ])
  const changes: Array<{ model_id: string, expected_revision: number }> = []
  await page.route('**/api/agents/Jarvis/model', async route => {
    changes.push(route.request().postDataJSON())
    await route.fallback()
  })

  await page.goto('/agents/Jarvis')
  const selection = page.getByRole('region', { name: 'Model selection' })
  await expect(selection).toContainText('openai.coding-agent')
  await expect(selection).toContainText('Default')
  await selection.getByRole('button', { name: 'Change model' }).click()
  const input = selection.getByRole('combobox', { name: 'Model ID' })
  await testInfo.attach('model-selection-desktop', { body: await page.screenshot(), contentType: 'image/png' })

  await input.fill('openai.cx/gpt-6-luna')
  await selection.getByRole('button', { name: 'Apply' }).click()
  await expect(selection).toContainText('openai.cx/gpt-6-luna')
  await expect(selection).toContainText('Custom')
  await selection.getByRole('button', { name: 'Change model' }).click()
  await selection.getByRole('button', { name: 'Use default' }).click()
  await expect(selection).toContainText('openai.coding-agent')
  await expect(selection.getByRole('button', { name: 'Use default' })).toHaveCount(0)

  await selection.getByRole('button', { name: 'Change model' }).click()
  await input.fill('openai.kr/gpt-5.6-luna')
  await selection.getByRole('button', { name: 'Apply' }).click()
  await expect(selection.getByRole('alert')).toContainText('failed a bounded inference probe')
  await expect(selection).toContainText('openai.coding-agent')
  expect(changes).toEqual([
    { run_id: '', model_id: 'openai.cx/gpt-6-luna', expected_revision: 0 },
    { run_id: '', model_id: '', expected_revision: 1 },
    { run_id: '', model_id: 'openai.kr/gpt-5.6-luna', expected_revision: 2 },
  ])
  expect(backend.unexpected).toEqual([])
})

test('agent model editor fits a mobile viewport', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    join(FIXTURES, '_app_boot_noise.yaml'),
    join(FIXTURES, 'agent_model_selection.yaml'),
  ], 'vi')
  await page.route('**/api/agents/Jarvis/model', route => route.fulfill({
    status: 422,
    contentType: 'application/json',
    body: JSON.stringify({ detail: 'The selected model failed a bounded inference probe; check provider routing and try another model.' }),
  }))
  await page.goto('/agents/Jarvis')
  const selection = page.getByRole('region', { name: 'Chọn model' })
  await selection.getByRole('button', { name: 'Đổi model' }).click()
  await expect(selection.getByRole('combobox', { name: 'Mã model' })).toBeVisible()
  await expect(selection.getByRole('button', { name: 'Hủy' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  expect(await selection.getByRole('button', { name: 'Hủy' }).evaluate(element => element.getBoundingClientRect().height)).toBeGreaterThanOrEqual(44)
  await testInfo.attach('model-selection-mobile', { body: await page.screenshot(), contentType: 'image/png' })
  await page.setViewportSize({ width: 320, height: 700 })
  const input = selection.getByRole('combobox', { name: 'Mã model' })
  await input.fill('openai.invalid-model')
  await selection.getByRole('button', { name: 'Áp dụng' }).click()
  await expect(selection.getByRole('alert')).toContainText('bounded inference probe')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  expect(await page.evaluate(() => Math.max(
    document.querySelector('.model-save').getBoundingClientRect().bottom,
    document.querySelector('.model-cancel').getBoundingClientRect().bottom,
  ))).toBeLessThan(560)
  await testInfo.attach('model-selection-mobile-320-error', { body: await page.screenshot(), contentType: 'image/png' })
  expect(backend.unexpected).toEqual([])
})

test('agent model card follows the light theme tokens', async ({ page }, testInfo) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    join(FIXTURES, '_app_boot_noise.yaml'),
    join(FIXTURES, 'agent_model_selection.yaml'),
  ])
  await page.addInitScript(() => localStorage.setItem('jarvis_theme', 'light'))
  await page.goto('/agents/Jarvis')
  const selection = page.getByRole('region', { name: 'Model selection' })
  await expect(selection).toHaveCSS('background-color', 'rgb(255, 255, 255)')
  await selection.getByRole('button', { name: 'Change model' }).click()
  await expect(selection.getByRole('combobox', { name: 'Model ID' })).toBeVisible()
  await testInfo.attach('model-selection-light', { body: await page.screenshot(), contentType: 'image/png' })
  expect(backend.unexpected).toEqual([])
})
