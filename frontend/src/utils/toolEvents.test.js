/**
 * Unit tests for toolEvents — chat-stream SSE → pushToolCall payloads.
 *
 * Guards the regression that triggered this helper: parallel tool_calls
 * in one LLM turn (Jarvis spawning two agents in a single assistant
 * message) used to render as a single bubble because the consumers
 * read `event.tools?.[0]` and ignored the rest.
 *
 * Run via `npm run test:unit` (Node's built-in test runner).
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { expandToolDone, expandToolRequest } from './toolEvents.js'


// ── expandToolRequest ──────────────────────────────────────────────


test('expandToolRequest yields one entry per tool when tools is an array', () => {
  const event = {
    type: 'tool_request',
    tools: [
      { name: 'agent_spawner__spawn_and_run_isolated', args: { lifecycle: 'oneshot' } },
      { name: 'agent_spawner__spawn_and_run_isolated', args: { lifecycle: 'resumable' } },
    ],
    message: 'Jarvis calling 2 tools',
  }
  const out = expandToolRequest(event)
  assert.equal(out.length, 2)
  assert.equal(out[0].tool, 'agent_spawner__spawn_and_run_isolated')
  assert.deepEqual(out[0].args, { lifecycle: 'oneshot' })
  assert.deepEqual(out[1].args, { lifecycle: 'resumable' })
  // command propagates from the event's batch message to every entry.
  assert.equal(out[0].command, 'Jarvis calling 2 tools')
  assert.equal(out[1].command, 'Jarvis calling 2 tools')
})

test('expandToolRequest falls back to a single legacy entry when tools is missing', () => {
  // Pre-batched-tools SSE shape — kept for compat with older event
  // producers (some custom MCPs / spawn bridges still emit this).
  const event = { type: 'tool_request', tool: 'filesystem__list_directory' }
  const out = expandToolRequest(event)
  assert.equal(out.length, 1)
  assert.equal(out[0].tool, 'filesystem__list_directory')
  assert.equal(out[0].args, null)
})

test('expandToolRequest falls back to server name when tool is also missing', () => {
  const event = { type: 'tool_request', server: 'agent_spawner' }
  const out = expandToolRequest(event)
  assert.equal(out.length, 1)
  assert.equal(out[0].tool, 'agent_spawner')
})

test('expandToolRequest uses "tool" placeholder when no name source is available', () => {
  // Defensive default — never emits an empty tool name into the store
  // (groupToolCalls keys on `tool` and would group disparate calls).
  assert.equal(expandToolRequest({ type: 'tool_request' })[0].tool, 'tool')
})

test('expandToolRequest treats empty tools array like missing', () => {
  const event = { type: 'tool_request', tools: [], tool: 'fallback' }
  const out = expandToolRequest(event)
  assert.equal(out.length, 1)
  assert.equal(out[0].tool, 'fallback')
})


// ── expandToolDone ─────────────────────────────────────────────────


test('expandToolDone yields one result entry per tool in the batch', () => {
  const event = {
    type: 'tool_done',
    tools: [
      { name: 'agent_spawner__spawn_and_run_isolated' },
      { name: 'agent_spawner__spawn_and_run_isolated' },
    ],
    duration_ms: 10493,
    result_preview: 'both succeeded',
  }
  const out = expandToolDone(event)
  assert.equal(out.length, 2)
  for (const entry of out) {
    assert.equal(entry.isResult, true)
    assert.equal(entry.duration, '10.5s')
    assert.equal(entry.resultPreview, 'both succeeded')
  }
})

test('expandToolDone omits duration when duration_ms missing', () => {
  const event = { type: 'tool_done', tools: [{ name: 'x' }] }
  const out = expandToolDone(event)
  assert.equal(out.length, 1)
  assert.equal(out[0].duration, undefined)
})

test('expandToolDone falls back to single legacy entry', () => {
  const event = {
    type: 'tool_done',
    tool: 'filesystem__list_directory',
    duration_ms: 250,
    result_preview: '5 entries',
  }
  const out = expandToolDone(event)
  assert.equal(out.length, 1)
  assert.equal(out[0].tool, 'filesystem__list_directory')
  assert.equal(out[0].duration, '0.3s')
  assert.equal(out[0].resultPreview, '5 entries')
  assert.equal(out[0].isResult, true)
})


// ── Round-trip: parallel calls + results match up ──────────────────


test('paired parallel request + done batches yield matching entry counts', () => {
  // Regression for the user-reported "only 1 tool used" UI bug. Even
  // with the same tool name twice, the helpers must NOT collapse —
  // the call/result grouping happens later in groupToolCalls, and
  // groupToolCalls matches strictly by (tool, !duration), so two
  // pending calls must be present before two results can pair to them.
  const requestEvent = {
    type: 'tool_request',
    tools: [
      { name: 'agent_spawner__spawn_and_run_isolated', args: { l: 'oneshot' } },
      { name: 'agent_spawner__spawn_and_run_isolated', args: { l: 'resumable' } },
    ],
  }
  const doneEvent = {
    type: 'tool_done',
    tools: [
      { name: 'agent_spawner__spawn_and_run_isolated' },
      { name: 'agent_spawner__spawn_and_run_isolated' },
    ],
    duration_ms: 10000,
  }
  const requests = expandToolRequest(requestEvent)
  const dones = expandToolDone(doneEvent)
  assert.equal(requests.length, 2)
  assert.equal(dones.length, 2)
  assert.deepEqual(requests.map((r) => r.tool), dones.map((d) => d.tool))
})
