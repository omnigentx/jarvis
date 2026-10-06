import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createChapterQueue } from './chapterQueue.js'
const tick=()=>new Promise(resolve=>setImmediate(resolve))
test('only one lookahead survives a rapid chapter/story change',async()=>{
  const pending=[],errors=[]
  const queue=createChapterQueue((s,f,signal)=>new Promise(resolve=>pending.push({s,f,signal,resolve})),e=>errors.push(e))
  queue.prepare('one','01','02');await tick()
  queue.prepare('two','01','02');await tick()
  assert.equal(pending[0].signal.aborted,true)
  pending[0].resolve({audio_url:'/old'});pending[1].resolve({audio_url:'/new'});await tick()
  assert.equal(queue.take('one','01','02'),null)
  assert.deepEqual(queue.take('two','01','02'),{audio_url:'/new'})
  assert.equal(errors.length,0)
})
test('prepare failure leaves explicit fallback available and reports once',async()=>{
  const errors=[]
  const queue=createChapterQueue(async()=>{throw new Error('Synthetic prepare failure')},e=>errors.push(e))
  queue.prepare('one','01','02');await tick()
  assert.equal(queue.take('one','01','02'),null)
  assert.equal(errors.length,1)
})
test('clearing queue discards even an uncancellable late response',async()=>{
  let resolve
  const queue=createChapterQueue(()=>new Promise(r=>resolve=r))
  queue.prepare('one','01','02');await tick();queue.clear()
  resolve({audio_url:'/old'});await tick()
  assert.equal(queue.take('one','01','02'),null)
})
