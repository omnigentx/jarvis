import { test, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { createRenderer } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { useAudioPlayerStore } from '../stores/audioPlayer.js'


const storage = new Map()
globalThis.localStorage = {getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)}
globalThis.window = new EventTarget()
globalThis.document = Object.assign(new EventTarget(), {cookie:'',visibilityState:'visible'})
Object.defineProperty(globalThis,'navigator',{value:{sendBeacon:()=>true},configurable:true})
globalThis.BroadcastChannel = class {postMessage() {} close() {}}
globalThis.MediaError = {MEDIA_ERR_NETWORK:2}
const { useAudioPlayer } = await import('./useAudioPlayer.js')
let engine, app, audio, calls
class NativeAudio extends EventTarget {
  constructor() {super();audio=this;this.paused=true;this.ended=false;this.currentTime=0;this.duration=12}
  play() { this.paused=false;this.ended=false;calls.push(['play',this.src]);queueMicrotask(()=>this.dispatchEvent(new Event('playing')));return Promise.resolve() }
  pause() {this.paused=true;this.dispatchEvent(new Event('pause'))}
  load() {}
}
globalThis.Audio = NativeAudio
const renderer = createRenderer({insert(){},remove(){},createComment:()=>({}),createElement:()=>({}),createText:()=>({}),parentNode:()=>null,nextSibling:()=>null,setText(){},setElementText(){},patchProp(){}})
function start() {
  storage.clear();calls=[];setActivePinia(createPinia())
  app=renderer.createApp({setup(){engine=useAudioPlayer();return()=>null}})
  app.mount({})
  globalThis.fetch = async (url, opts={}) => {
    calls.push([opts.method||'GET',url])
    if(opts.method==='HEAD') return new Promise(()=>{})
    return new Response(JSON.stringify({audio_url:url.includes('02.txt')?'/api/tts/second':'/api/tts/first',status:'ready'}),{headers:{'content-type':'application/json'}})
  }
  return useAudioPlayerStore()
}
afterEach(()=>{engine.destroy();app.unmount();useAudioPlayerStore().stopAndReset()})

test('completed chapter calls native play for queued chapter in the ended task, without HEAD',async()=>{
  const store=start()
  await store.playChapter('synthetic','Synthetic','01.txt',['01.txt','02.txt'])
  await new Promise(resolve=>setImmediate(resolve))
  audio.currentTime=12;audio.ended=true;audio.paused=true
  document.visibilityState='hidden'
  audio.dispatchEvent(new Event('ended'))
  // Before any network/microtask/Vue tick can complete, restart native audio.
  assert.match(audio.src,/second/)
  assert.equal(audio.paused,false)
  assert.equal(calls.filter(c=>c[0]==='play').length,2)
  assert.equal(calls.filter(c=>c[0]==='HEAD').length,0)
  audio.dispatchEvent(new Event('ended'))
  assert.equal(store.currentChapterFile,'02.txt')
  assert.equal(store.playbackType,'story')
  await new Promise(resolve=>setImmediate(resolve))
})

test('visibility/pageshow recovers a missed native ended event once',async()=>{
  const store=start()
  await store.playChapter('synthetic','Synthetic','01.txt',['01.txt','02.txt'])
  await new Promise(resolve=>setImmediate(resolve))
  audio.ended=true;audio.paused=true
  document.visibilityState='visible'
  document.dispatchEvent(new Event('visibilitychange'))
  window.dispatchEvent(new Event('pageshow'))
  assert.match(audio.src,/second/)
  assert.equal(calls.filter(c=>c[0]==='play').length,2)
  await new Promise(resolve=>setImmediate(resolve))
})

test('an unknown live-source HEAD stall times out and leaves playback paused',async t=>{
  const store=start()
  t.mock.timers.enable({apis:['setTimeout']})
  globalThis.EventSource=class extends EventTarget {close() {}}
  let headSignal
  globalThis.fetch=async(url,options={})=>{
    if(options.method==='HEAD') return new Promise((resolve,reject)=>{
      headSignal=options.signal
      headSignal.addEventListener('abort',()=>reject(new DOMException('Timeout','AbortError')),{once:true})
    })
    return new Response(JSON.stringify({audio_url:'/api/tts/live',status:'none'}),{headers:{'content-type':'application/json'}})
  }
  await store.playChapter('synthetic','Synthetic','01.txt',['01.txt'])
  audio.ended=true;audio.paused=true
  audio.dispatchEvent(new Event('ended'))
  assert.equal(headSignal.aborted,false)
  t.mock.timers.tick(15_000)
  await new Promise(resolve=>setImmediate(resolve))
  assert.equal(headSignal.aborted,true)
  assert.equal(store.isPlaying,false)
  assert.equal(store.isPaused,true)
})

test('closing player ignores native error from clearing the audio source', async () => {
  const { useToastState } = await import('./useToast.js')
  const store = start()
  const toastState = useToastState()
  for (const toast of toastState.toasts.value) toastState.dismissToast(toast.id)
  await store.playChapter('synthetic', 'Synthetic', '01.txt', ['01.txt'])
  await new Promise(resolve => setImmediate(resolve))
  store.stopAndReset()
  audio.error = { code: 4, message: 'Source cleared intentionally' }
  audio.dispatchEvent(new Event('error'))
  assert.equal(toastState.toasts.value.length, 0)
  assert.equal(store.playbackType, 'none')
})
