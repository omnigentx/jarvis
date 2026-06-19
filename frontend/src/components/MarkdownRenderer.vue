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
 *
 * Restyle (Track 5):
 *   - Code block chrome upgraded: 3 traffic-light dots, centered filename
 *     header, copy button (uses navigator.clipboard). Rendered post-render
 *     by walking pre>code blocks and wrapping them in a chrome shell.
 *   - Colors tuned to design tokens (var(--bg-2), var(--border-strong), etc).
 *   - Markdown render pipeline UNCHANGED — marked / DOMPurify / hljs / mermaid
 *     all behave identically. Only post-process chrome added.
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
      // so the agent (or user) can see what went wrong. Build the DOM via
      // textContent so a Mermaid error message that echoes attacker-supplied
      // source (or the source itself) can't break out into live markup.
      const pre = document.createElement('pre')
      pre.className = 'mermaid-error'
      const strong = document.createElement('strong')
      strong.textContent = 'Mermaid syntax error'
      pre.appendChild(strong)
      const errText = (err && err.message) || String(err)
      pre.appendChild(document.createTextNode(`\n${errText}\n\n${source}`))
      el.replaceChildren(pre)
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

// Post-process: wrap each <pre><code> in a chrome shell with traffic-light
// dots, filename label, and copy button. Idempotent — skips already-wrapped
// blocks. Walks the rendered DOM rather than rewriting the marked output so
// the marked / DOMPurify pipeline stays untouched (DOMPurify would strip our
// custom button/header HTML if we tried to inject it pre-sanitize).
function decorateCodeBlocks() {
  if (!contentRef.value) return
  const pres = contentRef.value.querySelectorAll('pre')
  pres.forEach((pre) => {
    if (pre.parentElement?.classList.contains('code-chrome')) return
    if (pre.classList.contains('plain-text')) return
    // Derive language label from `<code class="hljs language-xxx">`.
    const code = pre.querySelector('code')
    let lang = ''
    if (code) {
      const cls = (code.className || '').split(/\s+/)
      for (const c of cls) {
        if (c.startsWith('language-')) { lang = c.slice('language-'.length); break }
      }
    }
    const shell = document.createElement('div')
    shell.className = 'code-chrome'
    const header = document.createElement('div')
    header.className = 'code-chrome__header'
    // Build header via DOM API so `lang` (derived from a className that
    // survives DOMPurify untouched) can't smuggle markup into innerHTML.
    const dots = document.createElement('span')
    dots.className = 'code-chrome__dots'
    for (const color of ['#FF5F57', '#FEBC2E', '#28C840']) {
      const dot = document.createElement('span')
      dot.className = 'code-chrome__dot'
      dot.style.background = color
      dots.appendChild(dot)
    }
    header.appendChild(dots)
    const label = document.createElement('span')
    label.className = 'code-chrome__label'
    label.textContent = lang || 'code'  // textContent — not innerHTML
    header.appendChild(label)
    const btn = document.createElement('button')
    btn.className = 'code-chrome__copy'
    btn.type = 'button'
    btn.setAttribute('aria-label', 'Copy code')
    btn.textContent = 'Copy'
    header.appendChild(btn)
    pre.parentNode.insertBefore(shell, pre)
    shell.appendChild(header)
    shell.appendChild(pre)

    btn.addEventListener('click', async () => {
      const text = code ? code.innerText : pre.innerText
      try {
        await navigator.clipboard.writeText(text)
        btn.textContent = 'Copied'
        setTimeout(() => { btn.textContent = 'Copy' }, 1400)
      } catch {
        btn.textContent = 'Failed'
        setTimeout(() => { btn.textContent = 'Copy' }, 1400)
      }
    })
  })
}

watch(rendered, async () => {
  await nextTick()
  processLinks()
  decorateCodeBlocks()
  renderMermaidBlocks()
})

onMounted(() => {
  processLinks()
  decorateCodeBlocks()
  renderMermaidBlocks()
})
</script>

<template>
  <div ref="contentRef" class="md-content" v-html="rendered" />
</template>

<style>
.md-content {
  color: var(--text-dim, #B6BAC6);
  font-size: 14px;
  line-height: 1.7;
  word-break: break-word;
}

/* Strip leading margin so the first heading / paragraph sits flush
   against the parent container's padding instead of pushing the
   visible content downward. Without this, a card with padding:16px
   followed by `# Title` ends up with effectively 40px top spacing. */
.md-content > *:first-child { margin-top: 0 !important; }
.md-content > *:last-child { margin-bottom: 0 !important; }

.md-content h1 { font-size: 22px; font-weight: 700; color: var(--text, #F1F2F6); margin: 24px 0 12px; }
.md-content h2 { font-size: 18px; font-weight: 600; color: var(--text, #F1F2F6); margin: 20px 0 10px; }
.md-content h3 { font-size: 16px; font-weight: 600; color: var(--text, #F1F2F6); margin: 16px 0 8px; }
.md-content h4, .md-content h5, .md-content h6 { font-size: 14px; font-weight: 600; color: var(--text, #F1F2F6); margin: 12px 0 6px; }

.md-content p { margin: 8px 0; }
.md-content a { color: var(--primary-hover, #818CF8); text-decoration: none; }
.md-content a:hover { text-decoration: underline; }

.md-content ul, .md-content ol { padding-left: 24px; margin: 8px 0; }
.md-content li { margin: 4px 0; }

.md-content blockquote {
  border-left: 3px solid var(--primary, #6366F1);
  padding: 8px 16px;
  margin: 12px 0;
  color: var(--text-muted, #7B8094);
  background: var(--primary-bg, rgba(99, 102, 241, 0.08));
  border-radius: 0 var(--r-md, 10px) var(--r-md, 10px) 0;
}

.md-content code {
  background: var(--bg-2, #11141B);
  padding: 2px 6px;
  border-radius: var(--r-sm, 6px);
  font-family: var(--font-mono, 'Roboto Mono', monospace);
  font-size: 13px;
  color: var(--text, #F1F2F6);
}

.md-content pre {
  background: #0A0C12;
  /* The code window is always dark in BOTH themes, so its text must be a
     fixed light colour — not var(--text), which is near-black in light
     theme and rendered untokenised code (lang "text") invisible on the
     dark window. hljs token colours below already assume a dark bg. */
  color: #e3e6ef;
  border: 0;
  border-radius: 0;
  padding: 14px 16px;
  overflow-x: auto;
  margin: 0;
  font-family: var(--font-mono, 'Roboto Mono', monospace);
}

.md-content pre code {
  background: transparent;
  padding: 0;
  font-size: 12.5px;
  line-height: 1.65;
  /* The code window is ALWAYS dark (#0A0C12) in both themes. ``.md-content
     code`` above sets ``color: var(--text)`` which is near-black in light
     theme — and because it matches the <code> directly it overrides the light
     colour inherited from <pre>, making untokenised code invisible on the dark
     window. Pin a fixed light colour here. */
  color: #e3e6ef;
}

/* Code window chrome (matches DESIGN_HANDOFF §5). The wrapping div is
   inserted by decorateCodeBlocks() at runtime. */
.md-content .code-chrome {
  margin: 14px 0;
  background: #0A0C12;
  border: 1px solid var(--border-strong, rgba(255,255,255,0.12));
  border-radius: var(--r-md, 10px);
  overflow: hidden;
}
.md-content .code-chrome__header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  /* Fixed light tones — the chrome is always on the dark #0A0C12 window, so
     these must NOT follow theme tokens (which go dark in light theme and
     vanish). */
  border-bottom: 1px solid rgba(255, 255, 255, 0.12);
  background: rgba(255, 255, 255, 0.02);
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  color: #8b91a3;
}
.md-content .code-chrome__dots {
  display: inline-flex;
  gap: 6px;
  flex-shrink: 0;
}
.md-content .code-chrome__dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}
.md-content .code-chrome__label {
  flex: 1;
  text-align: center;
  letter-spacing: 0.06em;
  text-transform: lowercase;
  color: #8b91a3;  /* fixed light — chrome is always dark */
}
.md-content .code-chrome__copy {
  font-family: var(--font-mono, monospace);
  font-size: 10.5px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 3px 8px;
  background: transparent;
  /* Fixed light tones — always on the dark #0A0C12 window. */
  border: 1px solid rgba(255, 255, 255, 0.12);
  color: #b6bac6;
  border-radius: var(--r-sm, 6px);
  cursor: pointer;
  transition: all 0.15s ease;
}
.md-content .code-chrome__copy:hover {
  /* Fixed dark-window tones (theme-independent). */
  background: rgba(255, 255, 255, 0.08);
  border-color: rgba(255, 255, 255, 0.18);
  color: #f1f2f6;
}

/* Wide tables overflow the container on mobile; let them scroll
   horizontally instead of squashing or breaking layout. */
.md-content table {
  display: block;
  width: 100%;
  max-width: 100%;
  overflow-x: auto;
  border-collapse: collapse;
  margin: 12px 0;
  font-size: 13px;
  -webkit-overflow-scrolling: touch;
}

.md-content th {
  background: var(--bg-2, #11141B);
  color: var(--text, #F1F2F6);
  font-weight: 600;
  text-align: left;
  padding: 10px 14px;
  border: 1px solid var(--border-strong, rgba(255,255,255,0.12));
}

.md-content td {
  padding: 8px 14px;
  border: 1px solid var(--border, rgba(255,255,255,0.06));
}

.md-content img {
  max-width: 100%;
  border-radius: var(--r-md, 10px);
  margin: 12px 0;
  cursor: pointer;
}

.md-content hr {
  border: none;
  border-top: 1px solid var(--border, rgba(255,255,255,0.06));
  margin: 16px 0;
}

.md-content .plain-text {
  white-space: pre-wrap;
  font-family: inherit;
  background: transparent;
  border: none;
  padding: 0;
  /* Plain text sits on the normal (light/dark) card, not the dark code window,
     so reset the inherited `.md-content pre` colour (#e3e6ef, a fixed light for
     the dark window) to a themed token — otherwise it renders light-on-light
     and is invisible in light theme. */
  color: var(--text-dim);
}

.md-content .md-mermaid-block {
  margin: 16px 0;
  padding: 12px;
  background: var(--bg-2, #11141B);
  border: 1px solid var(--border, rgba(255,255,255,0.06));
  border-radius: var(--r-md, 10px);
  overflow-x: auto;
  text-align: center;
}

.md-content .md-mermaid-block svg {
  max-width: 100%;
  height: auto;
}

.md-content .mermaid-error {
  color: var(--danger, #EF4444);
  background: var(--danger-bg, rgba(239, 68, 68, 0.10));
  border: 1px solid rgba(239, 68, 68, 0.3);
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

/* ── Mobile responsive — tighter type and spacing for narrow screens. */
@media (max-width: 640px) {
  .md-content { font-size: 13px; line-height: 1.65; }
  .md-content h1 { font-size: 19px; margin: 18px 0 10px; }
  .md-content h2 { font-size: 16px; margin: 16px 0 8px; }
  .md-content h3 { font-size: 15px; margin: 14px 0 6px; }
  .md-content h4, .md-content h5, .md-content h6 { font-size: 13px; }
  .md-content ul, .md-content ol { padding-left: 20px; }
  .md-content blockquote { padding: 6px 12px; }
  .md-content pre { padding: 12px; font-size: 12px; }
  .md-content code { font-size: 12px; padding: 1px 5px; }
  .md-content table { font-size: 12px; }
  .md-content th, .md-content td { padding: 6px 10px; }
}
</style>
