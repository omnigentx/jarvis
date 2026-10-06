/** Decode actual synthetic PCM audio; no private chapter material. */
import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mockBackend, seedApiKey } from '../harness'
const fixtures=join(dirname(fileURLToPath(import.meta.url)),'..','fixtures')
function tone(seconds:number) {
  const samples=16000*seconds, data=Buffer.alloc(44+samples*2)
  data.write('RIFF');data.writeUInt32LE(data.length-8,4);data.write('WAVE',8)
  data.write('fmt ',12);data.writeUInt32LE(16,16);data.writeUInt16LE(1,20)
  data.writeUInt16LE(1,22);data.writeUInt32LE(16000,24);data.writeUInt32LE(32000,28)
  data.writeUInt16LE(2,32);data.writeUInt16LE(16,34);data.write('data',36);data.writeUInt32LE(samples*2,40)
  for(let i=0;i<samples;i++)data.writeInt16LE(Math.round(1000*Math.sin(i*2*Math.PI*220/16000)),44+i*2)
  return data
}
async function boot(page:any) {
  await seedApiKey(page)
  await mockBackend(page,[join(fixtures,'_app_boot_noise.yaml'),join(fixtures,'stories_list.yaml')])
}
test('three decoded chapters advance without waiting for HEAD or boundary POST',async({page})=>{
  await boot(page)
  const downloads:number[]=[], probes:string[]=[], prepared:string[]=[]
  await page.route('**/api/stories/alpha_story/*/prepare',async route=>{
    const file=new URL(route.request().url()).pathname.split('/').at(-2)!
    prepared.push(file)
    const index=file.startsWith('0002')?2:3
    await route.fulfill({json:{audio_url:`/api/tts/synthetic_${index}`,status:'ready',duration:3}})
  })
  await page.route('**/api/stories/alpha_story/*/play',async route=>{
    if(!route.request().url().includes('0001')) {
      // Progress bookkeeping can remain pending; playback must not await it.
      await new Promise(()=>{})
      return
    }
    await route.fulfill({json:{audio_url:'/api/tts/synthetic_1',status:'ready',duration:3}})
  })
  await page.route('**/api/tts/**',async route=>{
    if(route.request().method()==='HEAD') {
      probes.push(route.request().url());await new Promise(()=>{});return
    }
    const index=Number(new URL(route.request().url()).pathname.split('_').at(-1))
    downloads.push(index)
    await route.fulfill({contentType:'audio/wav',body:tone(3)})
  })
  await page.goto('/stories/alpha_story')
  await page.locator('#chapter-0001_prologue\\.txt [data-testid="chapter-play"]').click()
  await expect.poll(()=>prepared.length).toBeGreaterThan(0)
  await expect(page.locator('.mini-player__chapter')).toContainText('Ch.2',{timeout:10000})
  await expect(page.locator('.mini-player__spinner')).toHaveCount(0)
  await expect(page.locator('.mini-player__chapter')).toContainText('Ch.3',{timeout:10000})
  expect(probes).toEqual([])
  expect(downloads).toEqual([1,2,3])
  // Natural end of final chapter clears the player; no wrap or duplicate skips.
  await expect(page.locator('.mini-player')).toHaveCount(0,{timeout:10000})
})

test('queue failure plus fallback failure stops spinner and deliberate retry works',async({page})=>{
  await boot(page)
  let attempts=0
  await page.route('**/api/stories/alpha_story/*/prepare',route=>route.fulfill({status:503,json:{detail:'Synthetic prepare failure'}}))
  await page.route('**/api/stories/alpha_story/*/play',async route=>{
    const second=route.request().url().includes('0002')
    if(second && ++attempts===1) return route.fulfill({status:502,json:{detail:'Synthetic provider failure'}})
    return route.fulfill({json:{audio_url:`/api/tts/synthetic_${second?2:1}`,status:'ready',duration:second?10:2}})
  })
  await page.route('**/api/tts/**',route=>route.fulfill({contentType:'audio/wav',body:tone(route.request().url().includes('_2')?10:2)}))
  await page.goto('/stories/alpha_story')
  await page.locator('#chapter-0001_prologue\\.txt [data-testid="chapter-play"]').click()
  await expect.poll(()=>attempts).toBe(1)
  await expect(page.locator('.mini-player__spinner')).toHaveCount(0)
  await page.locator('#chapter-0002_chapter_two\\.txt [data-testid="chapter-play"]').click()
  await expect.poll(()=>attempts).toBe(2)
  await expect(page.locator('.mini-player__chapter')).toContainText('Ch.2')
  await expect(page.locator('.mini-player__spinner')).toHaveCount(0)
})

// This verifies browser JS suspension/recovery, not an iOS/Android lock screen.
test('Chromium resumes a real audio playlist after JavaScript suspension',async({page,context,browserName})=>{
  test.skip(browserName!=='chromium','Debugger CDP command is Chromium-specific')
  await boot(page)
  await page.addInitScript(()=>{
    const state:{ticks:number,plays:number,audio?:HTMLMediaElement}={ticks:0,plays:0}
    ;(window as any).__audioLifecycle=state
    setInterval(()=>state.ticks++,100)
    const play=HTMLMediaElement.prototype.play
    HTMLMediaElement.prototype.play=function(){state.plays++;state.audio=this;return play.call(this)}
  })
  let prepared=false
  await page.route('**/api/stories/alpha_story/*/prepare',async route=>{
    prepared=true
    await route.fulfill({json:{audio_url:'/api/tts/synthetic_2',status:'ready',duration:12}})
  })
  await page.route('**/api/stories/alpha_story/*/play',route=>route.fulfill({json:{audio_url:route.request().url().includes('0001')?'/api/tts/synthetic_1':'/api/tts/synthetic_2',status:'ready',duration:12}}))
  await page.route('**/api/tts/**',route=>route.fulfill({contentType:'audio/wav',body:tone(route.request().url().includes('_1')?2:12)}))
  await page.goto('/stories/alpha_story')
  await page.locator('#chapter-0001_prologue\\.txt [data-testid="chapter-play"]').click()
  await expect.poll(()=>prepared).toBe(true)
  await expect(page.locator('.mini-player__spinner')).toHaveCount(0)
  // canplay/spinner state alone does not prove decoding/playback has started.
  await expect.poll(()=>page.evaluate(()=>(window as any).__audioLifecycle.audio?.currentTime||0)).toBeGreaterThan(0.2)
  const session=await context.newCDPSession(page)
  await session.send('Debugger.enable')
  const before=await page.evaluate(()=>(window as any).__audioLifecycle.ticks)
  try {
    const paused=new Promise<void>(resolve=>session.once('Debugger.paused',()=>resolve()))
    await session.send('Debugger.pause')
    await paused
    // Host-side wait spans the first track's end while renderer timers freeze.
    await new Promise(resolve=>setTimeout(resolve,4000))
    await session.send('Debugger.resume')
    const after=await page.evaluate(()=>(window as any).__audioLifecycle.ticks)
    expect(after-before).toBeLessThan(5)
    await page.bringToFront()
    await expect(page.locator('.mini-player__chapter')).toContainText('Ch.2',{timeout:10000})
    expect(await page.evaluate(()=>(window as any).__audioLifecycle.plays)).toBe(2)
    await expect(page.locator('.mini-player__spinner')).toHaveCount(0)
  } finally {
    await session.send('Debugger.disable')
    await session.detach()
  }
})


test('audio HTTP failure shows a useful error and deliberate retry decodes', async ({page}) => {
  await boot(page)
  await page.route('**/api/stories/alpha_story/*/play', route => route.fulfill({json:{audio_url:'/api/tts/synthetic_retry',status:'generating'}}))
  let failed = true
  await page.route('**/api/tts/synthetic_retry*', route => failed
    ? route.fulfill({status:503,headers:{'Retry-After':'60'},json:{detail:'Synthetic cooldown'}})
    : route.fulfill({contentType:'audio/wav',body:tone(12)}))
  await page.goto('/stories/alpha_story')
  await page.getByTestId('chapter-play').first().click()
  await expect(page.getByText('Unable to play audio', {exact:true})).toBeVisible()
  await expect(page.locator('.mini-player__spinner')).toHaveCount(0)
  failed = false
  await page.getByTestId('chapter-play').first().click()
  await expect(page.locator('.mini-player__spinner')).toHaveCount(0)
  await expect(page.locator('.mini-player__time')).not.toContainText('0:00 /')
})
