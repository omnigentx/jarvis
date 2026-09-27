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

  await input.fill('gpt-6-luna')
  await expect(selection.getByRole('listbox', { name: 'Matching models' }).getByRole('option')).toHaveCount(1)
  await input.press('ArrowDown')
  await input.press('Enter')
  await expect(input).toHaveValue('openai.cx/gpt-6-luna')
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
  await page.route('**/api/agents/model-catalog', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ models: Array.from({ length: 570 }, (_, index) => `openai.cx/model-${index}`) }),
  }))
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
  await expect(selection).toContainText('570 mã model')
  const mobileInput = selection.getByRole('combobox', { name: 'Mã model' })
  await mobileInput.fill('model-')
  await expect(selection.getByRole('listbox').getByRole('option')).toHaveCount(8)
  await testInfo.attach('model-search-570-mobile', { body: await selection.screenshot(), contentType: 'image/png' })
  await selection.getByRole('option', { name: 'openai.cx/model-0', exact: true }).click()
  await expect(mobileInput).toHaveValue('openai.cx/model-0')
  await mobileInput.fill('openai.coding-agent')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  expect(await selection.getByRole('button', { name: 'Hủy' }).evaluate(element => element.getBoundingClientRect().height)).toBeGreaterThanOrEqual(44)
  await testInfo.attach('model-selection-mobile', { body: await page.screenshot(), contentType: 'image/png' })
  await page.setViewportSize({ width: 320, height: 700 })
  await mobileInput.fill('openai.invalid-model')
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

test('spawned dynamic agent exposes model selection in Overview', async ({ page }) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    join(FIXTURES, '_app_boot_noise.yaml'),
    join(FIXTURES, 'agent_model_selection.yaml'),
  ])
  await page.route('**/api/agents/ModelProbe', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      name: 'ModelProbe', status: 'idle', type: 'dynamic', run_id: 'probe-run',
      model: 'openai.coding-agent', configured_model: 'openai.coding-agent',
      base_model: 'openai.coding-agent', model_revision: 0, overridden: false,
      active_model: null, instruction: 'Probe model changes.', servers: [], tools: {}, skills: [],
    }),
  }))
  await page.goto('/agents/ModelProbe')
  const selection = page.getByRole('region', { name: 'Model selection' })
  await expect(selection).toContainText('openai.coding-agent')
  await selection.getByRole('button', { name: 'Change model' }).click()
  await expect(selection.getByRole('combobox', { name: 'Model ID' })).toBeVisible()
  expect(backend.unexpected).toEqual([])
})

test('catalog failure keeps manual entry available and retry restores suggestions', async ({ page }) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    join(FIXTURES, '_app_boot_noise.yaml'),
    join(FIXTURES, 'agent_model_selection.yaml'),
  ])
  let catalogRequests = 0
  await page.route('**/api/agents/model-catalog', route => {
    catalogRequests += 1
    return route.fulfill(catalogRequests === 1
      ? { status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'catalog offline' }) }
      : { status: 200, contentType: 'application/json', body: JSON.stringify({ models: ['openai.coding-agent', 'openai.cx/gpt-6-luna'] }) })
  })
  await page.goto('/agents/Jarvis')
  const selection = page.getByRole('region', { name: 'Model selection' })
  await selection.getByRole('button', { name: 'Change model' }).click()
  const input = selection.getByRole('combobox', { name: 'Model ID' })
  await expect(input).toBeEnabled()
  await expect(selection).toContainText('Suggestions are unavailable')
  await selection.getByRole('button', { name: 'Retry' }).click()
  await expect(selection).toContainText('2 available model IDs')
  await input.fill('gpt-6')
  await expect(selection.getByRole('option', { name: 'openai.cx/gpt-6-luna' })).toBeVisible()
  expect(catalogRequests).toBe(2)
  expect(backend.unexpected).toEqual([])
})
