import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mockBackend, seedApiKey } from '../harness'

const noise = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures', '_app_boot_noise.yaml')
const candidate = { id: 'sample', name: 'review', repo: 'openai/plugins', commit: 'a'.repeat(40), digest: 'b'.repeat(64), status: 'needs_approval', skills: [{ name: 'review', description: 'Review changes' }], blockers: [], bindings: [], server_names: [] }

for (const mobile of [false, true]) {
  test(`plugin content review and truthful activation (${mobile ? 'mobile' : 'desktop'})`, async ({ page }, info) => {
    if (mobile) await page.setViewportSize({ width: 390, height: 844 })
    await seedApiKey(page)
    await mockBackend(page, [noise])
    await page.route('**/api/plugins**', async route => {
      const path = new URL(route.request().url()).pathname
      let result: unknown = { plugins: [candidate] }
      if (path.endsWith('/targets')) result = { targets: [{agent:'Jarvis',run_id:null,label:'Jarvis'}, {agent:'Developer',run_id:'team-run',label:'Developer · acceptance'}] }
      if (path.endsWith('/files')) result = new URL(route.request().url()).searchParams.has('path')
        ? {digest:candidate.digest,content:'<script>window.packageExecuted = true</script>'}
        : {digest:candidate.digest,files:[{path:'server.py',bytes:48}]}
      if (path.endsWith('/content')) result = { content: '<script>window.pluginExecuted = true</script>', digest: candidate.digest }
      if (path.endsWith('/manual-review')) result = { plugin: candidate, target:'Jarvis', run_id:null, review_token:'test-token', execution_policy:null }
      if (path.endsWith('/manual-activate')) result = { ...candidate, status: 'activation_failed' }
      await route.fulfill({ json: result })
    })
    await page.goto('/settings')
    await page.getByRole('button', { name: 'Plugins', exact: true }).click()
    const card = page.getByTestId('plugin-sample')
    await expect(card).toBeVisible()
    await card.getByRole('button', { name: 'Review skill', exact: true }).click()
    await expect(page.locator('pre')).toContainText('<script>')
    expect(await page.evaluate(() => (window as any).pluginExecuted)).toBeUndefined()
    await card.getByRole('button',{name:'Review package files',exact:true}).click()
    await card.getByRole('button',{name:'server.py 48 B',exact:true}).click()
    await expect(card.locator('pre').last()).toContainText('window.packageExecuted')
    expect(await page.evaluate(() => (window as any).packageExecuted)).toBeUndefined()
    await card.getByLabel('Target agent').selectOption('Jarvis')
    await card.getByRole('button', { name: 'Activate', exact: true }).click()
    await page.getByRole('dialog').getByRole('checkbox').check()
    await page.getByRole('dialog').getByRole('button',{name:'Confirm and activate',exact:true}).click()
    await expect(card).toContainText('Activation failed')
    await expect(card.getByText('Ready', { exact: true })).not.toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
    await page.screenshot({ path: info.outputPath(`plugins-${mobile ? 'mobile' : 'desktop'}.png`), fullPage: true })
  })
}

test('non-blocking plugin review does not claim agent paused or resumed', async ({page}) => {
  await seedApiKey(page)
  await mockBackend(page,[noise])
  const approval = {id:'plugin-review',status:'pending',agent_name:'Jarvis',approval_type:'plugin_source',title:'Review plugin source',content:'Pinned source',content_format:'text',paused_agents:[],comments:[],created_at:new Date().toISOString()}
  await page.route('**/api/approvals**',route => route.fulfill({json:new URL(route.request().url()).pathname === '/api/approvals' ? [approval] : approval}))
  await page.goto('/approvals')
  await page.locator('.approvals-row').click()
  await expect(page.getByRole('button',{name:'✓ Approve',exact:true})).toBeVisible()
  await expect(page.getByText('continues working — review pending',{exact:false})).toBeVisible()
  await expect(page.getByText('agent continues',{exact:false})).not.toBeVisible()
})

test('MCP policy uses the host profile and clears entered credentials after save',async ({page}) => {
  await page.setViewportSize({width:390,height:844})
  await seedApiKey(page)
  await mockBackend(page,[noise])
  let saved: any
  const digest = 'sha256:'+'c'.repeat(64)
  await page.route('**/api/plugins**',async route => {
    const path = new URL(route.request().url()).pathname
    let result: any = {plugins:[{...candidate,server_names:['echo'],credential_slots:['TEST_SLOT']}]}
    if(path.endsWith('/targets')) result={targets:[]}
    if(path.endsWith('/runtime-profile')) result={runtime_available:true,image:digest}
    if(path.endsWith('/policy')) {
      if(route.request().method()==='PUT') { saved=route.request().postDataJSON(); result={configured:true,credential_slots:['TEST_SLOT']} }
      else result={image:null,credential_slots:[]}
    }
    await route.fulfill({json:result})
  })
  await page.goto('/settings')
  await page.getByRole('button',{name:'Plugins',exact:true}).click()
  await page.locator('.execution-policy summary').click()
  const policy = page.locator('.execution-policy')
  await expect(policy.getByLabel('Reviewed sandbox image digest')).toHaveValue(digest)
  await policy.getByLabel('TEST_SLOT',{exact:true}).fill('nonsecret-test-marker')
  await policy.getByRole('button',{name:'Save policy',exact:true}).click()
  await expect(policy.getByLabel('TEST_SLOT',{exact:true})).toHaveValue('')
  expect(saved).toEqual({image:digest,credentials:{TEST_SLOT:'nonsecret-test-marker'}})
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy()
})

test('reviewed Ready MCP has no false blocker and shows explicit target placeholder',async ({page}) => {
  await seedApiKey(page)
  await mockBackend(page,[noise])
  await page.route('**/api/plugins**',route=>route.fulfill({json:new URL(route.request().url()).pathname.endsWith('/targets')
    ? {targets:[{agent:'Jarvis',run_id:null,label:'Jarvis'}]}
    : {plugins:[{...candidate,status:'ready',policy_configured:true,skills:[],blockers:['executable_content','mcp_requires_policy_review']}]}}))
  await page.goto('/settings')
  await page.getByRole('button',{name:'Plugins',exact:true}).click()
  const card=page.getByTestId('plugin-sample')
  await expect(card.getByText('Ready',{exact:true})).toBeVisible()
  await expect(card.locator('.blocked')).not.toBeVisible()
  expect(await card.getByLabel('Target agent').evaluate((select:any)=>select.selectedIndex)).toBe(0)
})

for (const mobile of [false, true]) {
  test(`source reputation and explicit external consent (${mobile ? 'mobile' : 'desktop'})`, async ({ page }, info) => {
    if (mobile) await page.setViewportSize({ width: 390, height: 844 })
    await seedApiKey(page)
    await mockBackend(page, [noise])
    let installs = 0
    const entries = [
      {...candidate, source_origin:{kind:'official',publisher:'OpenAI'}, subdirectory:'plugins/review'},
      {...candidate,name:'community-review',repo:'community/review',source_origin:{kind:'community',marketplace:'OpenAI'}, subdirectory:'plugin'},
      {...candidate,name:'external-review',repo:'unknown/review',source_origin:{kind:'external'}, subdirectory:'plugin'},
    ]
    await page.route('**/api/plugins**', async route => {
      const path = new URL(route.request().url()).pathname
      let json: any = { plugins: [] }
      if (path.endsWith('/targets')) json = { targets: [] }
      if (path.endsWith('/catalog')) json = { plugins: entries }
      if (path.endsWith('/install')) { installs++; expect(route.request().postDataJSON().source_confirmed).toBe(true); json = { status:'needs_approval' } }
      await route.fulfill({ json })
    })
    await page.goto('/settings')
    await page.getByRole('button',{name:'Plugins',exact:true}).click()
    await page.getByRole('button',{name:'Browse plugins',exact:true}).click()
    await expect(page.locator('.catalog')).toContainText('Official repository')
    await expect(page.locator('.catalog')).toContainText('Community · listed in marketplace')
    await expect(page.locator('.catalog')).toContainText('External · unverified')
    await page.locator('.catalog li').last().getByRole('button',{name:'Add plugin',exact:true}).click()
    const dialog=page.getByRole('dialog')
    await expect(dialog).toContainText('unknown/review')
    await expect(dialog).toContainText('not a security endorsement')
    const proceed=dialog.getByRole('button',{name:'Download and inspect',exact:true})
    await expect(proceed).toBeDisabled()
    expect(installs).toBe(0)
    await page.screenshot({ path: info.outputPath(`source-review-${mobile ? 'mobile' : 'desktop'}.png`), fullPage:true })
    await dialog.getByRole('checkbox').check()
    await proceed.click()
    await expect(dialog).not.toBeVisible()
    await expect(page.getByRole('status')).toContainText('Awaiting activation review')
    expect(installs).toBe(1)
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy()
    await page.locator('.catalog li').first().getByRole('button',{name:'Add plugin',exact:true}).click()
    await expect(page.getByRole('dialog').getByRole('checkbox')).toHaveCount(0)
    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog')).not.toBeVisible()
    expect(installs).toBe(1)
  })
}
