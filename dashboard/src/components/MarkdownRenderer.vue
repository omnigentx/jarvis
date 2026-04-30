<script setup>
/**
 * MarkdownRenderer — renders markdown content with syntax highlighting.
 * Uses `marked` for markdown→HTML and `highlight.js` for code blocks.
 * Styled with dark theme matching Jarvis design system.
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

// Register common languages
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
})

const contentRef = ref(null)

// Configure marked with highlight.js via marked-highlight extension
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

const rendered = computed(() => {
  if (!props.content) return ''
  if (props.contentType === 'text') {
    // Wrap plain text in <pre> for readability
    return `<pre class="plain-text">${props.content.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</pre>`
  }
  return markedInstance.parse(props.content)
})

// Make external links open in new tab
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
})

onMounted(() => {
  processLinks()
})
</script>

<template>
  <div ref="contentRef" class="md-content" v-html="rendered" />
</template>

<style>
/* ─── Markdown Renderer Dark Theme ─── */
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

/* highlight.js overrides for dark theme */
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
