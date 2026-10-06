import { test, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { createRenderer } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { useAudioPlayerStore } from '../stores/audioPlayer.js'
import { useAudioPlayer } from './useAudioPlayer.js'

const storage = new Map()
globalThis.localStorage = {getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)}
globalThis.window = new EventTarget()
globalThis.document = Object.assign(new EventTarget(), {cookie:'',visibilityState:'visible'})
Object.defineProperty(globalThis,'navigator',{value:{sendBeacon:()=>true},configurable:true})
globalThis.BroadcastChannel = class {postMessage() {} close() {}}
globalThis.MediaError = {MEDIA_ERR_NETWORK:2}
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
