/**
 * Unit tests for agentTerminalUtils — render-side derivations
 * for AgentTerminal.vue. Run via ``npm run test:unit``.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  buildRenderRows,
  textContent,
  isTextTruncated,
  toolCallList,
  toolResultList,
  summarizeArgs,
} from './agentTerminalUtils.js'


// ── buildRenderRows ─────────────────────────────────────────────────


test('buildRenderRows inserts a run separator at the first turn', () => {
  const turns = [
    { turn_idx: 0, role: 'user', run_id: 'r1' },
    { turn_idx: 1, role: 'assistant', run_id: 'r1' },
  ]
  const rows = buildRenderRows(turns)
  assert.equal(rows.length, 3)
  assert.equal(rows[0].kind, 'run')
  assert.equal(rows[0].run_id, 'r1')
  assert.equal(rows[1].kind, 'turn')
  assert.equal(rows[2].kind, 'turn')
})

test('buildRenderRows adds a separator only when run_id changes', () => {
  const turns = [
    { turn_idx: 0, role: 'user', run_id: 'r1' },
    { turn_idx: 1, role: 'assistant', run_id: 'r1' },
    { turn_idx: 2, role: 'user', run_id: 'r2' },
  ]
  const rows = buildRenderRows(turns)
  const seps = rows.filter(r => r.kind === 'run')
  assert.equal(seps.length, 2)
  assert.deepEqual(seps.map(s => s.run_id), ['r1', 'r2'])
})

test('buildRenderRows skips separator for turns missing run_id', () => {
  const turns = [
    { turn_idx: 0, role: 'user' },           // no run_id
    { turn_idx: 1, role: 'assistant' },      // no run_id
  ]
  const rows = buildRenderRows(turns)
  assert.equal(rows.length, 2)
  assert.ok(rows.every(r => r.kind === 'turn'))
})

test('buildRenderRows handles empty input', () => {
  assert.deepEqual(buildRenderRows([]), [])
  assert.deepEqual(buildRenderRows(null), [])
})


// ── textContent ─────────────────────────────────────────────────────


test('textContent joins multi-block text with newlines', () => {
  const turn = {
    message: {
      content: [
        { type: 'text', text: 'first' },
        { type: 'text', text: 'second' },
      ],
    },
  }
  assert.equal(textContent(turn), 'first\nsecond')
})

test('textContent ignores non-text blocks', () => {
  const turn = {
    message: {
      content: [
        { type: 'text', text: 'hello' },
        { type: 'image', data: '...' },
      ],
    },
  }
  assert.equal(textContent(turn), 'hello')
})

test('textContent returns empty for missing content', () => {
  assert.equal(textContent({}), '')
  assert.equal(textContent({ message: {} }), '')
  assert.equal(textContent({ message: { content: [] } }), '')
})


// ── isTextTruncated ────────────────────────────────────────────────


test('isTextTruncated true when any text block has _truncated', () => {
  const turn = {
    message: {
      content: [
        { type: 'text', text: 'small' },
        { type: 'text', text: 'big', _truncated: true, _full_size: 99999 },
      ],
    },
  }
  assert.equal(isTextTruncated(turn), true)
})

test('isTextTruncated false when no block is truncated', () => {
  const turn = { message: { content: [{ type: 'text', text: 'small' }] } }
  assert.equal(isTextTruncated(turn), false)
})


// ── toolCallList ───────────────────────────────────────────────────


test('toolCallList extracts name and args from tool_calls dict', () => {
  const turn = {
    message: {
      tool_calls: {
        'tu_1': {
          params: { name: 'search', arguments: { q: 'jarvis' } },
        },
        'tu_2': {
          params: { name: 'fetch', arguments: { url: 'https://x.com' } },
        },
      },
    },
  }
  const list = toolCallList(turn)
  assert.equal(list.length, 2)
  assert.deepEqual(list.map(t => t.name).sort(), ['fetch', 'search'])
  const search = list.find(t => t.name === 'search')
  assert.deepEqual(search.args, { q: 'jarvis' })
})

test('toolCallList returns empty for non-assistant turns', () => {
  assert.deepEqual(toolCallList({ message: {} }), [])
  assert.deepEqual(toolCallList({}), [])
})


// ── toolResultList ─────────────────────────────────────────────────


test('toolResultList carries _truncated/_full_size flags through', () => {
  const turn = {
    message: {
      tool_results: {
        'tu_1': {
          content: [
            { type: 'text', text: 'first chunk', _truncated: true, _full_size: 200_000 },
          ],
        },
      },
    },
  }
  const list = toolResultList(turn)
  assert.equal(list.length, 1)
  assert.equal(list[0].truncated, true)
  assert.equal(list[0].fullSize, 200_000)
  assert.equal(list[0].text, 'first chunk')
})

test('toolResultList flags isError', () => {
  const turn = {
    message: {
      tool_results: {
        'tu_1': {
          isError: true,
          content: [{ type: 'text', text: 'EACCES' }],
        },
      },
    },
  }
  const [r] = toolResultList(turn)
  assert.equal(r.isError, true)
})

test('toolResultList concatenates multiple text blocks', () => {
  const turn = {
    message: {
      tool_results: {
        'tu_1': {
          content: [
            { type: 'text', text: 'line1' },
            { type: 'text', text: 'line2' },
          ],
        },
      },
    },
  }
  assert.equal(toolResultList(turn)[0].text, 'line1\nline2')
})


// ── summarizeArgs ──────────────────────────────────────────────────


test('summarizeArgs handles short string args', () => {
  assert.equal(summarizeArgs({ q: 'jarvis' }), 'q=jarvis')
})

test('summarizeArgs truncates long string values to 60 chars + ellipsis', () => {
  const big = 'X'.repeat(100)
  const out = summarizeArgs({ q: big })
  // 60 chars of X + ellipsis '…'
  assert.match(out, /^q=X{60}…$/)
})

test('summarizeArgs JSON-stringifies non-string values', () => {
  const out = summarizeArgs({ data: { key: 1 } })
  assert.equal(out, 'data={"key":1}')
})

test('summarizeArgs lists at most 3 keys with "+N more" tail', () => {
  const out = summarizeArgs({ a: 1, b: 2, c: 3, d: 4, e: 5 })
  assert.match(out, /\+2 more/)
})

test('summarizeArgs handles empty / non-object', () => {
  assert.equal(summarizeArgs({}), '')
  assert.equal(summarizeArgs(null), '')
  assert.equal(summarizeArgs(undefined), '')
})
