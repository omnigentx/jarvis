import { test } from 'node:test'
import assert from 'node:assert/strict'
import { effectScope } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { useAgentsStore } from '../stores/agents.js'
import { useActivityStream } from './useActivityStream.js'
import { agentIdentity } from './agentIdentity.js'

test('useActivityStream selection retains same-name agents by session', () => {
  setActivePinia(createPinia())
  const store = useAgentsStore()
  store.upsertAgent('Worker', { session_id: 'session-a', name: 'Worker', status: 'running' })
  store.upsertAgent('Worker', { session_id: 'session-b', name: 'Worker', status: 'running' })
  const scope = effectScope()
  let stream
  scope.run(() => { stream = useActivityStream() })
  stream.toggleAgent(store.agentsList[0])
  assert.deepEqual(stream.filteredAgents.value.map(agentIdentity), [agentIdentity(store.agentsList[1])])
  scope.stop()
})
