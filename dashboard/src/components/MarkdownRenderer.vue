<script setup>
/**
 * MarkdownRenderer — markdown → HTML with code highlighting and Mermaid diagrams.
 *
 * Pipeline:
 *   1. `marked` parses MD → HTML; fenced code blocks get hljs classes via
 *      `marked-highlight`. ```mermaid blocks are emitted as a sentinel div
 *      that renderMermaidBlocks() upgrades after mount.
 *   2. The rendered HTML runs through DOMPurify before v-html. This is new —
 *      previously we relied on marked's safe defaults, but with mermaid in
 *      the mix the output may include `<svg>` (added to the allowlist).
 *   3. Mermaid is dynamic-imported only when a `mermaid` block is present,
 *      so the ~600KB chunk doesn't ship to users who never view diagrams.
 */
import { computed, onMounted, ref, watch, nextTick } from 'vue'
import { Marked } from 'marked'
import { markedHighlight } from 'marked-highlight'
import hljs from 'highlight.js/lib/core'
import javascript from 'highlight.js/lib/languages/javascript'
import python from 'highlight.js/lib/languages/python'
import bash from 'highlight.js/lib/languages/bash'
import json from 'highlight.js/lib/languages/json'
import sql from 'highlight.js/lib/languages/sql'
import yaml from 'highlight.js/lib/languages/yaml'
import xml from 'highlight.js/lib/languages/xml'
import DOMPurify from 'dompurify'

hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('js', javascript)
hljs.registerLanguage('python', python)
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('sh', bash)
hljs.registerLanguage('json', json)
hljs.registerLanguage('sql', sql)
hljs.registerLanguage('yaml', yaml)
hljs.registerLanguage('yml', yaml)
hljs.registerLanguage('html', xml)
hljs.registerLanguage('xml', xml)

const props = defineProps({
  content: { type: String, default: '' },
  contentType: { type: String, default: 'markdown' },
  enableMermaid: { type: Boolean, default: true },
})

const contentRef = ref(null)

const MERMAID_PLACEHOLDER_CLASS = 'md-mermaid-block'

const markedInstance = new Marked(
  markedHighlight({
    langPrefix: 'hljs language-',
    highlight(code, lang) {
      if (lang && hljs.getLanguage(lang)) {
        try {
          return hljs.highlight(code, { language: lang }).value
        } catch (_) { /* ignore */ }
      }
      try {
        return hljs.highlightAuto(code).value
      } catch (_) { /* ignore */ }
      return code
    },
  }),
  { breaks: true, gfm: true }
)

function _utf8ToB64(s) {
  // btoa() does not support Unicode directly; encode UTF-8 bytes first.
  return btoa(unescape(encodeURIComponent(s)))
}

// Custom block extension: matches ```mermaid fences before the default
// code-block tokenizer can swallow them, and emits a sentinel div whose
// text content is the base64-encoded source. Stashing the source in
// textContent (rather than a data-* attribute) sidesteps DOMPurify's
// data-attr scrubber, which silently strips multi-line / `<>`-laden values.
markedInstance.use({
  extensions: [
    {
      name: 'mermaid_fence',
      level: 'block',
      start(src) {
        const i = src.indexOf('```mermaid')
        return i < 0 ? undefined : i
      },
      tokenizer(src) {
        const m = /^```mermaid[ \t]*\n([\s\S]*?)\n```[ \t]*(?:\n|$)/.exec(src)
        if (m) {
          return {
            type: 'mermaid_fence',
            raw: m[0],
            code: m[1],
          }
        }
      },
      renderer(token) {
        return `<div class="${MERMAID_PLACEHOLDER_CLASS}">${_utf8ToB64(token.code)}</div>`
      },
    },
  ],
})

const PURIFY_CONFIG = {
  ADD_TAGS: ['svg', 'g', 'path', 'rect', 'circle', 'line', 'polyline', 'polygon', 'text', 'tspan', 'foreignObject', 'marker', 'defs', 'use', 'ellipse', 'pattern'],
  ADD_ATTR: ['data-mermaid-b64', 'viewBox', 'transform', 'fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'cx', 'cy', 'r', 'd', 'x', 'y', 'x1', 'x2', 'y1', 'y2', 'points', 'dy', 'text-anchor', 'font-size', 'font-family', 'marker-end', 'marker-start', 'orient', 'refX', 'refY', 'markerWidth', 'markerHeight'],
}

const rendered = computed(() => {
  if (!props.content) return ''
  if (props.contentType === 'text') {
    return `<pre class="plain-text">${props.content.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</pre>`
  }
  const html = markedInstance.parse(props.content)
  return DOMPurify.sanitize(html, PURIFY_CONFIG)
})

const hasMermaid = computed(() =>
  props.enableMermaid && /```mermaid\b/.test(props.content || '')
)

let _mermaidPromise = null
function loadMermaid() {
  // Single-flight dynamic import. Subsequent calls reuse the cached module.
  if (!_mermaidPromise) {
    _mermaidPromise = import('mermaid').then((mod) => {
      const mermaid = mod.default || mod
      mermaid.initialize({
        startOnLoad: false,
        theme: 'dark',
        securityLevel: 'strict',
        fontFamily: 'inherit',
      })
      return mermaid
    })
  }
  return _mermaidPromise
}

function _b64ToUtf8(b64) {
  try {
    return decodeURIComponent(escape(atob(b64)))
  } catch (_) {
    return ''
  }
}

let _mermaidIdCounter = 0
async function renderMermaidBlocks() {
  if (!contentRef.value || !hasMermaid.value) return
  const blocks = contentRef.value.querySelectorAll(`.${MERMAID_PLACEHOLDER_CLASS}`)
  if (!blocks.length) return
  const mermaid = await loadMermaid()
  for (const el of blocks) {
    // Skip blocks already rendered (text content was replaced by SVG).
    if (el.querySelector('svg')) continue
    const source = _b64ToUtf8((el.textContent || '').trim())
    if (!source) continue
    const id = `md-mermaid-${++_mermaidIdCounter}`
    try {
      const { svg } = await mermaid.render(id, source)
      el.innerHTML = svg
    } catch (err) {
      // Surface errors loud-but-contained: keep the original source visible
      // so the agent (or user) can see what went wrong.
      el.innerHTML = `<pre class="mermaid-error"><strong>Mermaid syntax error</strong>\n${(err && err.message) || err}\n\n${source.replace(/</g, '&lt;')}</pre>`
    }
  }
}

function processLinks() {
  if (!contentRef.value) return
  const links = contentRef.value.querySelectorAll('a')
  links.forEach((a) => {
    if (a.href && !a.href.startsWith(window.location.origin)) {
      a.setAttribute('target', '_blank')
      a.setAttribute('rel', 'noopener noreferrer')
    }
  })
}

watch(rendered, async () => {
  await nextTick()
  processLinks()
  renderMermaidBlocks()
})

onMounted(() => {
  processLinks()
  renderMermaidBlocks()
})
</script>

<template>
  <div ref="contentRef" class="md-content" v-html="rendered" />
</template>

<style>
.md-content {
  color: var(--text-secondary, #c4c8d4);
  font-size: 14px;
  line-height: 1.7;
  word-break: break-word;
}

.md-content h1 { font-size: 22px; font-weight: 700; color: var(--text-heading, #f0f2f5); margin: 24px 0 12px; }
.md-content h2 { font-size: 18px; font-weight: 600; color: var(--text-heading, #f0f2f5); margin: 20px 0 10px; }
.md-content h3 { font-size: 16px; font-weight: 600; color: var(--text-heading, #f0f2f5); margin: 16px 0 8px; }
.md-content h4, .md-content h5, .md-content h6 { font-size: 14px; font-weight: 600; color: var(--text-heading, #f0f2f5); margin: 12px 0 6px; }

.md-content p { margin: 8px 0; }
.md-content a { color: #3b82f6; text-decoration: none; }
.md-content a:hover { text-decoration: underline; }

.md-content ul, .md-content ol { padding-left: 24px; margin: 8px 0; }
.md-content li { margin: 4px 0; }

.md-content blockquote {
  border-left: 3px solid #3b82f6;
  padding: 8px 16px;
  margin: 12px 0;
  color: var(--text-muted, #8b8fa3);
  background: rgba(59, 130, 246, 0.05);
  border-radius: 0 8px 8px 0;
}

.md-content code {
  background: #111318;
  padding: 2px 6px;
  border-radius: 4px;
  font-family: 'Roboto Mono', 'Fira Code', monospace;
  font-size: 13px;
  color: #e2e8f0;
}

.md-content pre {
  background: #111318;
  border: 1px solid #1a1d2e;
  border-radius: 8px;
  padding: 16px;
  overflow-x: auto;
  margin: 12px 0;
}

.md-content pre code {
  background: transparent;
  padding: 0;
  font-size: 13px;
  line-height: 1.5;
}

.md-content table {
  width: 100%;
  border-collapse: collapse;
  margin: 12px 0;
  font-size: 13px;
}

.md-content th {
  background: #111318;
  color: var(--text-heading, #f0f2f5);
  font-weight: 600;
  text-align: left;
  padding: 10px 14px;
  border: 1px solid #1a1d2e;
}

.md-content td {
  padding: 8px 14px;
  border: 1px solid #1a1d2e;
}

.md-content img {
  max-width: 100%;
  border-radius: 8px;
  margin: 12px 0;
  cursor: pointer;
}

.md-content hr {
  border: none;
  border-top: 1px solid #1a1d2e;
  margin: 16px 0;
}

.md-content .plain-text {
  white-space: pre-wrap;
  font-family: inherit;
  background: transparent;
  border: none;
  padding: 0;
}

.md-content .md-mermaid-block {
  margin: 16px 0;
  padding: 12px;
  background: #0c0e15;
  border: 1px solid #1a1d2e;
  border-radius: 8px;
  overflow-x: auto;
  text-align: center;
}

.md-content .md-mermaid-block svg {
  max-width: 100%;
  height: auto;
}

.md-content .mermaid-error {
  color: #f87171;
  background: rgba(248, 113, 113, 0.08);
  border: 1px solid rgba(248, 113, 113, 0.3);
  white-space: pre-wrap;
}

.md-content .hljs-keyword { color: #c792ea; }
.md-content .hljs-string { color: #c3e88d; }
.md-content .hljs-number { color: #f78c6c; }
.md-content .hljs-comment { color: #546e7a; font-style: italic; }
.md-content .hljs-function { color: #82aaff; }
.md-content .hljs-built_in { color: #ffcb6b; }
.md-content .hljs-variable { color: #f07178; }
.md-content .hljs-attr { color: #ffcb6b; }
.md-content .hljs-title { color: #82aaff; }
.md-content .hljs-params { color: #e2e8f0; }
</style>
