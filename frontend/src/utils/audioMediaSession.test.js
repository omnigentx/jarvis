import { test } from 'node:test'
import assert from 'node:assert/strict'
import { configureAudioMediaSession } from './audioMediaSession.js'

test('unsupported lock-screen action does not abort setup of other controls',()=>{
  const handlers={}
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaSession:{setActionHandler:(key,fn)=>{
    if(key==='seekto') throw new DOMException('unsupported','NotSupportedError')
    handlers[key]=fn
  }}}})
  globalThis.MediaMetadata=class {constructor(data){Object.assign(this,data)}}
  const store={currentChapterFile:'Synthetic',currentStoryTitle:'Synthetic',nextChapter:()=>{}}
  assert.doesNotThrow(()=>configureAudioMediaSession(store,()=>({})))
  assert.equal(typeof handlers.nexttrack,'function')
  assert.equal(typeof handlers.seekforward,'function')
})
