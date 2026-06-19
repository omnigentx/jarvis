<script setup>
import { ref, watch, nextTick, computed, reactive } from 'vue'
import { useBreakpoint } from '../../composables/useBreakpoint'
import { parseYoutubeTags, youtubeEmbedUrl } from '../../utils/youtubeTags'
import { normalizeTs } from '../../utils/timeFormat.js'
import { useVoiceSession } from '../../composables/useVoiceSession.js'
import { useLang } from '../../composables/useLang'
import MarkdownRenderer from '../MarkdownRenderer.vue'

/**
 * ChatMessages — restyled to match design tokens.
 *
 * Bubble system:
 *   - User: var(--bg-3) bg, plain border, right-aligned, avatar 'U'
 *   - Jarvis: var(--primary-bg) bg, var(--primary-bg-strong) border,
 *             left-aligned, avatar 'J' (indigo gradient)
 *   - STT streaming (user-side): cyan blinking caret '|' at message tail
 *   - TTS streaming (jarvis-side): 3 indigo wave dots at message tail
 *   - Interrupted (jarvis-side): warning border + INTERRUPTED chip badge
 *   - Barge-in (user-side, after interrupt): cyan BARGE-IN chip above bubble
 *
 * Streaming flags come from existing chatStore.message.isStreaming + the
 * voice session's wasInterrupted reactive. Voice composable is unmodified —
 * we only consume its state.
 */

const props = defineProps({
  messages: { type: Array, default: () => [] },
  agent: { type: Object, default: null },
  isStreaming: { type: Boolean, default: false },
})

const scrollContainer = ref(null)
const expandedTools = reactive({})
const expandedRows = reactive({})
const { isMobile } = useBreakpoint()
const voice = useVoiceSession()
const { t } = useLang()

function toggleTools(msgId) { expandedTools[msgId] = !expandedTools[msgId] }
function toggleRow(msgId, idx) { expandedRows[`${msgId}-${idx}`] = !expandedRows[`${msgId}-${idx}`] }
function isRowExpanded(msgId, idx) { return !!expandedRows[`${msgId}-${idx}`] }

// ── Memory-used chip (auto-injected recall block) ──
// The retrieval hook injects a message tagged with this marker. Instead of
// rendering it as a raw user bubble, we collapse it into a subtle chip with an
// expandable list of the recalled excerpts. Marker mirrors
// services/memory/retrieval_hook.MEMORY_MARKER.
const MEMORY_MARKER = '⟦memory:recalled⟧'
const expandedMemory = reactive({})
const memLabel = computed(() => t('chat.memoryUsed'))
function isMemoryBlock(msg) {
  return msg.role === 'user' && typeof msg.content === 'string' && msg.content.includes(MEMORY_MARKER)
}
function memoryLines(msg) {
  return String(msg.content || '')
    .split('\n')
    .filter((l) => l.trim().startsWith('- '))
    .map((l) => l.replace(/^\s*-\s*/, ''))
}
function toggleMemory(msgId) { expandedMemory[msgId] = !expandedMemory[msgId] }

// Auto-scroll to bottom on new messages
watch(
  () => props.messages.length,
  async () => {
    await nextTick()
    if (scrollContainer.value) {
      scrollContainer.value.scrollTop = scrollContainer.value.scrollHeight
    }
  }
)

// Track previous interrupt state so we know which user bubble is the
// "barge-in" one (= the most recent user message added right around a
// wasInterrupted flip). Cheap, presentation-only.
const lastBargeUserMsgId = ref(null)
watch(
  () => voice.wasInterrupted.value,
  (now, prev) => {
    if (now && !prev) {
      // Find most recent user message — mark it as the barge-in turn.
      for (let i = props.messages.length - 1; i >= 0; i--) {
        if (props.messages[i].role === 'user') {
          lastBargeUserMsgId.value = props.messages[i].id
          break
        }
      }
    }
  },
)

// Per-message INTERRUPTED flag (set by chatStore.markMessageInterrupted
// when the LLM-in-progress placeholder gets cancelled mid-generation —
// see useVoiceSession.js ``tts_interruption`` handler). Was previously
// derived from ``voice.wasInterrupted`` (a single global ref) + last
// assistant message id, which tagged the wrong bubble whenever a barge-in
// happened *after* the LLM had already finalised (Case B — only TTS was
// cancelled, the message itself was complete).
function isInterrupted(msg) {
  return msg.role === 'assistant' && msg.isInterrupted === true
}

function isBargeIn(msg) {
  return msg.role === 'user' && msg.id === lastBargeUserMsgId.value
}

const agentInitials = computed(() => {
  if (!props.agent?.name) return '?'
  return props.agent.name
    .split(/[\s_-]+/)
    .map(w => w[0]?.toUpperCase() || '')
    .join('')
    .slice(0, 2)
})

function formatTime(ts) {
  const ms = normalizeTs(ts)
  if (ms === null) return ''
  const d = new Date(ms)
  const now = new Date()
  const diff = now - d
  if (diff < 60000) return t('chat.justNow')
  if (diff < 3600000) return t('chat.minutesAgo', { n: Math.floor(diff / 60000) })
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function groupToolCalls(toolCalls) {
  if (!toolCalls?.length) return []
  const grouped = []
  for (const tc of toolCalls) {
    if (tc.isResult) {
      const match = [...grouped].reverse().find(g => g.tool === tc.tool && !g.duration)
      if (match) {
        match.duration = tc.duration
        match.status = 'done'
        match.resultPreview = tc.resultPreview || null
        continue
      }
    }
    grouped.push({
      tool: tc.tool || 'tool',
      duration: tc.duration || null,
      status: tc.isResult ? 'done' : (tc.status || 'running'),
      args: tc.args || null,
      resultPreview: tc.resultPreview || null,
    })
  }
  return grouped
}

function hasDetail(g) {
  return (g.args && Object.keys(g.args).length > 0) || g.resultPreview
}

function totalDuration(toolCalls) {
  const groups = groupToolCalls(toolCalls)
  if (!groups.length) return ''
  let total = 0
  for (const g of groups) {
    if (g.duration) total += parseFloat(g.duration) || 0
  }
  return total === 0 ? '' : `${total.toFixed(1)}s`
}

function toolCount(toolCalls) { return groupToolCalls(toolCalls).length }

function getToolIcon(tool) {
  if (!tool) return 'terminal'
  const t = tool.toLowerCase()
  if (t.includes('agent')) return 'agent'
  if (t.includes('search') || t.includes('serpapi') || t.includes('brave')) return 'search'
  if (t.includes('read') || t.includes('file') || t.includes('write')) return 'file'
  return 'terminal'
}

function formatToolName(tool) {
  if (!tool) return 'tool'
  if (tool.includes('agent__')) return tool.replace('agent__', '')
  return tool.replace(/__/g, ' › ')
}

function parsedAgentContent(content) {
  return parseYoutubeTags(content)
}
</script>

<template>
  <div
    ref="scrollContainer"
    data-testid="chat-messages"
    class="msgs-scroll"
    :class="{ 'is-mobile': isMobile }"
  >
    <!-- Empty state -->
    <div v-if="!messages.length && !isStreaming" class="empty-state">
      <div class="empty-icon">💬</div>
      <div class="empty-title">{{ t('chat.startConversation') }}</div>
      <div class="empty-sub">
        {{ t('chat.emptySub', { name: agent?.name || t('chat.anAgent') }) }}
      </div>
    </div>

    <div class="msgs-stack">
      <template v-for="msg in messages" :key="msg.id">
        <!-- ── MEMORY-USED CHIP (auto-injected recall block) ───────────── -->
        <div v-if="isMemoryBlock(msg)" class="row row-memory">
          <button class="memory-chip" @click="toggleMemory(msg.id)">
            🧠 {{ memoryLines(msg).length }} {{ memLabel }}
            <span class="mc-chevron" :class="{ expanded: !!expandedMemory[msg.id] }">▾</span>
          </button>
          <div v-if="expandedMemory[msg.id]" class="memory-detail">
            <div v-for="(line, i) in memoryLines(msg)" :key="i" class="memory-line">{{ line }}</div>
          </div>
        </div>

        <!-- ── USER MESSAGE ────────────────────────────────────────────── -->
        <div v-else-if="msg.role === 'user'" class="row row-user">
          <div class="bubble-wrap">
            <span v-if="isBargeIn(msg)" class="chip chip-bargein">
              <span class="chip-dot" /> {{ t('chat.bargeIn') }}
            </span>
            <div class="bubble bubble-user">
              <span class="bubble-text">{{ msg.content }}</span>
              <span v-if="msg.isStreaming" class="stt-caret" aria-hidden="true">|</span>
            </div>
            <div class="msg-time msg-time-right">{{ formatTime(msg.timestamp) }}</div>
          </div>
          <div class="ava ava-user">U</div>
        </div>

        <!-- ── AGENT MESSAGE ──────────────────────────────────────────── -->
        <div v-else class="row row-jarvis">
          <div class="ava ava-jarvis">J</div>

          <div class="bubble-wrap">
            <div v-if="!msg.isStreaming" class="msg-meta">
              <span class="msg-agent">{{ agent?.name || 'Jarvis' }}</span>
              <span class="msg-time">{{ formatTime(msg.timestamp) }}</span>
            </div>

            <!-- Content bubble -->
            <div
              v-if="msg.content || msg.isStreaming"
              class="bubble bubble-jarvis"
              :class="{
                'bubble-error': msg.isError,
                'bubble-interrupted': isInterrupted(msg),
              }"
            >
              <!-- Thinking placeholder (no content yet) -->
              <div v-if="msg.isStreaming && !msg.content" class="typing-row">
                <span class="typing-dot" />
                <span class="typing-dot" />
                <span class="typing-dot" />
              </div>
              <template v-else>
                <MarkdownRenderer
                  :content="parsedAgentContent(msg.content).text"
                  content-type="markdown"
                />
                <!-- TTS streaming indicator (Jarvis is speaking) -->
                <span v-if="msg.isStreaming" class="tts-wave" aria-hidden="true">
                  <span class="tts-dot" />
                  <span class="tts-dot" />
                  <span class="tts-dot" />
                </span>
              </template>

              <!-- Interrupted badge -->
              <div v-if="isInterrupted(msg)" class="interrupted-strip">
                <span class="chip chip-interrupted">◼ {{ t('chat.interrupted') }}</span>
              </div>
            </div>

            <!-- YouTube embeds -->
            <div
              v-for="videoId in parsedAgentContent(msg.content).videoIds"
              :key="videoId"
              class="yt-embed"
              data-testid="chat-youtube-embed"
            >
              <iframe
                :src="youtubeEmbedUrl(videoId)"
                :data-video-id="videoId"
                title="YouTube video player"
                frameborder="0"
                allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                referrerpolicy="strict-origin-when-cross-origin"
                allowfullscreen
              ></iframe>
            </div>

            <!-- Tool calls -->
            <div v-if="msg.toolCalls?.length" class="tc-section">
              <button class="tc-header" @click="toggleTools(msg.id)">
                <span class="tc-header-left">
                  <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
                    <path d="M9.77 4.23a4 4 0 0 1 2 3.27 4 4 0 0 1-1.15 3.12L6.5 14.74a1.5 1.5 0 0 1-2.12 0l-.12-.12a1.5 1.5 0 0 1 0-2.12l4.12-4.12A4 4 0 0 1 9.77 4.23z" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
                  </svg>
                  <span class="tc-summary">
                    {{ t('chat.toolsUsed', { n: toolCount(msg.toolCalls) }) }}
                  </span>
                  <span v-if="totalDuration(msg.toolCalls)" class="tc-duration-label">
                    · {{ totalDuration(msg.toolCalls) }}
                  </span>
                </span>
                <svg
                  :class="['tc-chevron', { expanded: expandedTools[msg.id] }]"
                  width="12" height="12" viewBox="0 0 16 16" fill="none"
                >
                  <path d="M4 6L8 10L12 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
              </button>
              <transition name="tc-expand">
                <div v-if="expandedTools[msg.id]" class="tc-list">
                  <div
                    v-for="(g, idx) in groupToolCalls(msg.toolCalls)"
                    :key="idx"
                    class="tc-entry"
                  >
                    <div
                      :class="['tc-row', { 'tc-row-clickable': hasDetail(g) }]"
                      @click="hasDetail(g) && toggleRow(msg.id, idx)"
                    >
                      <div class="tc-icon">
                        <svg v-if="getToolIcon(g.tool) === 'agent'" width="12" height="12" viewBox="0 0 16 16" fill="none">
                          <path d="M8 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM3 14c0-2.8 2.2-5 5-5s5 2.2 5 5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
                        </svg>
                        <svg v-else-if="getToolIcon(g.tool) === 'search'" width="12" height="12" viewBox="0 0 16 16" fill="none">
                          <circle cx="7" cy="7" r="4" stroke="currentColor" stroke-width="1.2"/>
                          <path d="M10 10l3.5 3.5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
                        </svg>
                        <svg v-else width="12" height="12" viewBox="0 0 16 16" fill="none">
                          <rect x="1.5" y="2.5" width="13" height="11" rx="2" stroke="currentColor" stroke-width="1.2"/>
                          <path d="M5 7l2 2-2 2" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
                          <path d="M9 11h3" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
                        </svg>
                      </div>
                      <span class="tc-name">{{ formatToolName(g.tool) }}</span>
                      <div class="tc-row-right">
                        <span v-if="g.duration" class="tc-dur">{{ g.duration }}</span>
                        <svg v-if="g.status === 'done'" width="14" height="14" viewBox="0 0 16 16" fill="none">
                          <path d="M4.5 8.5L7 11l4.5-5" stroke="var(--success)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                        </svg>
                        <div v-else class="tc-spinner"></div>
                        <svg
                          v-if="hasDetail(g)"
                          :class="['tc-row-chevron', { expanded: isRowExpanded(msg.id, idx) }]"
                          width="10" height="10" viewBox="0 0 16 16" fill="none"
                        >
                          <path d="M4 6L8 10L12 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                        </svg>
                      </div>
                    </div>
                    <transition name="tc-expand">
                      <div v-if="isRowExpanded(msg.id, idx)" class="tc-detail">
                        <div v-if="g.args && Object.keys(g.args).length" class="tc-detail-block">
                          <div class="tc-detail-label">{{ t('chat.arguments') }}</div>
                          <div class="tc-detail-args">
                            <div v-for="(val, key) in g.args" :key="key" class="tc-arg-row">
                              <span class="tc-arg-key">{{ key }}</span>
                              <span class="tc-arg-val">{{ val }}</span>
                            </div>
                          </div>
                        </div>
                        <div v-if="g.resultPreview" class="tc-detail-block">
                          <div class="tc-detail-label">{{ t('chat.result') }}</div>
                          <div class="tc-detail-result">
                            <MarkdownRenderer
                              :content="g.resultPreview"
                              content-type="markdown"
                              :enable-mermaid="false"
                            />
                          </div>
                        </div>
                      </div>
                    </transition>
                  </div>
                </div>
              </transition>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.msgs-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 24px 32px 16px;
  background: var(--bg-0);
}
.msgs-scroll.is-mobile { padding: 16px 12px; }

.msgs-stack {
  display: flex;
  flex-direction: column;
  gap: 18px;
  max-width: 820px;
  margin: 0 auto;
  width: 100%;
}

/* ── Empty state ────────────────────────────────────────────────────── */
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-muted);
  text-align: center;
}
.empty-icon { font-size: 44px; margin-bottom: 14px; opacity: 0.35; }
.empty-title { font-size: 15px; font-weight: 600; color: var(--text-dim); margin-bottom: 4px; }
.empty-sub { font-size: 12.5px; color: var(--text-muted); }

/* ── Row layout ─────────────────────────────────────────────────────── */
.row { display: flex; gap: 10px; align-items: flex-start; }
.row-user { flex-direction: row-reverse; }
.bubble-wrap { display: flex; flex-direction: column; min-width: 0; max-width: 78%; }

/* ── Avatars ────────────────────────────────────────────────────────── */
.ava {
  flex-shrink: 0;
  width: 32px; height: 32px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
}
.ava-user {
  background: var(--bg-4);
  color: var(--text-dim);
  border: 1px solid var(--border-strong);
}
.ava-jarvis {
  background: linear-gradient(135deg, var(--primary), var(--accent));
  color: white;
}

/* ── Bubbles ────────────────────────────────────────────────────────── */
.bubble {
  padding: 10px 14px;
  border-radius: var(--r-lg);
  font-size: 13.5px;
  line-height: 1.55;
  color: var(--text);
  position: relative;
  word-break: break-word;
}
.bubble-text { white-space: pre-wrap; }

.bubble-user {
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
}
.bubble-jarvis {
  background: var(--primary-bg);
  border: 1px solid var(--primary-bg-strong);
}
.bubble-error { color: var(--danger); border-color: rgba(239, 68, 68, 0.35); }
.bubble-interrupted { border-color: var(--warning); border-width: 1px; }

/* ── STT caret (user is speaking, message still streaming via STT) ── */
.stt-caret {
  display: inline-block;
  margin-left: 2px;
  font-family: var(--font-mono);
  color: var(--accent);
  font-weight: 600;
  vertical-align: -2px;
  animation: stt-blink 1s steps(2, end) infinite;
}
@keyframes stt-blink {
  0%, 50% { opacity: 1; }
  50.01%, 100% { opacity: 0; }
}

/* ── TTS wave dots (Jarvis is speaking) ───────────────────────────── */
.tts-wave {
  display: inline-flex;
  gap: 4px;
  margin-left: 8px;
  vertical-align: middle;
}
.tts-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--primary);
  display: inline-block;
  animation: tts-wave-bounce 0.8s ease-in-out infinite;
}
.tts-dot:nth-child(2) { animation-delay: 0.2s; }
.tts-dot:nth-child(3) { animation-delay: 0.4s; }
@keyframes tts-wave-bounce {
  0%, 80%, 100% { transform: translateY(0); opacity: 0.5; }
  40%           { transform: translateY(-4px); opacity: 1; }
}

/* ── Typing dots (thinking placeholder, no content yet) ──────────── */
.typing-row { display: inline-flex; gap: 5px; align-items: center; height: 18px; }
.typing-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--text-muted);
  animation: typing-bounce 1.4s ease-in-out infinite;
}
.typing-dot:nth-child(2) { animation-delay: 0.2s; }
.typing-dot:nth-child(3) { animation-delay: 0.4s; }
@keyframes typing-bounce {
  0%, 60%, 100% { opacity: 0.3; transform: scale(0.85); }
  30%           { opacity: 1;   transform: scale(1);    }
}

/* ── Interrupted strip ───────────────────────────────────────────── */
.interrupted-strip {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed rgba(245, 158, 11, 0.35);
  display: flex;
  align-items: center;
  gap: 6px;
}

/* ── Chips ────────────────────────────────────────────────────────── */
.chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 18px;
  padding: 0 7px;
  border-radius: var(--r-full);
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  font-weight: 500;
  border: 1px solid var(--border-strong);
  background: var(--bg-2);
  color: var(--text-muted);
}
.chip-dot {
  width: 5px; height: 5px;
  border-radius: 50%;
  background: currentColor;
  display: inline-block;
}
.chip-bargein {
  background: var(--accent-bg);
  color: var(--accent);
  border-color: rgba(34, 211, 238, 0.30);
  margin-bottom: 6px;
  align-self: flex-end;
}
.chip-bargein .chip-dot { animation: typing-bounce 1.2s ease-in-out infinite; }
.chip-interrupted {
  background: var(--warning-bg);
  color: var(--warning);
  border-color: rgba(245, 158, 11, 0.30);
}

/* ── Message meta ─────────────────────────────────────────────────── */
.msg-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 5px;
}
.msg-agent {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--primary-hover);
}
.msg-time {
  font-family: var(--font-mono);
  font-size: 9.5px;
  color: var(--text-subtle);
  letter-spacing: 0.10em;
}
.msg-time-right { margin-top: 4px; text-align: right; }

/* ── YouTube ──────────────────────────────────────────────────────── */
.yt-embed {
  margin-top: 8px;
  border-radius: var(--r-md);
  overflow: hidden;
  border: 1px solid var(--border-strong);
  background: #000;
  aspect-ratio: 16 / 9;
  max-width: 560px;
}
.yt-embed iframe { width: 100%; height: 100%; display: block; border: 0; }

/* ── Tool calls ───────────────────────────────────────────────────── */
.tc-section { margin-top: 8px; }
.tc-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 7px 12px;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  cursor: pointer;
  color: var(--text-dim);
  transition: border-color 0.15s ease;
}
.tc-header:hover { border-color: var(--border-strong); }
.tc-header-left { display: flex; align-items: center; gap: 6px; }
.tc-summary { font-size: 11.5px; font-weight: 500; color: var(--text-dim); }
.tc-duration-label { font-size: 11px; color: var(--text-muted); }
.tc-chevron { transition: transform 0.2s; flex-shrink: 0; color: var(--text-muted); }
.tc-chevron.expanded { transform: rotate(180deg); }

.tc-list {
  margin-top: 4px;
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  overflow: hidden;
  background: var(--bg-1);
}
.tc-row { display: flex; align-items: center; gap: 8px; padding: 6px 12px; min-height: 32px; }
.tc-entry:not(:last-child) { border-bottom: 1px solid var(--border); }
.tc-row-clickable { cursor: pointer; transition: background 0.12s; }
.tc-row-clickable:hover { background: var(--bg-2); }
.tc-row-right { display: flex; align-items: center; gap: 6px; margin-left: auto; color: var(--text-muted); }
.tc-row-chevron { transition: transform 0.2s; flex-shrink: 0; }
.tc-row-chevron.expanded { transform: rotate(180deg); }
.tc-icon {
  flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  width: 22px; height: 22px;
  border-radius: var(--r-sm);
  background: var(--bg-2);
  color: var(--text-muted);
}
.tc-name { font-size: 12px; font-weight: 500; color: var(--text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tc-dur {
  font-size: 10.5px; font-weight: 500;
  color: var(--text-muted);
  padding: 1px 5px;
  background: var(--bg-2);
  border-radius: var(--r-sm);
}
.tc-spinner {
  width: 12px; height: 12px;
  border: 1.5px solid var(--border);
  border-top-color: var(--text-muted);
  border-radius: 50%;
  animation: tc-spin 0.8s linear infinite;
}
@keyframes tc-spin { to { transform: rotate(360deg); } }

.tc-detail {
  padding: 8px 12px 10px 42px;
  background: var(--bg-0);
  border-top: 1px solid var(--border);
}
.tc-detail-block + .tc-detail-block { margin-top: 10px; }
.tc-detail-label {
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 600;
  color: var(--text-subtle);
  text-transform: uppercase;
  letter-spacing: 0.10em;
  margin-bottom: 4px;
}
.tc-detail-args { display: flex; flex-direction: column; gap: 2px; }
.tc-arg-row { display: flex; gap: 8px; font-size: 11.5px; line-height: 18px; }
.tc-arg-key {
  color: var(--text-muted);
  flex-shrink: 0;
  min-width: 60px;
  font-family: var(--font-mono);
}
.tc-arg-key::after { content: ':'; }
.tc-arg-val { color: var(--text-dim); word-break: break-word; }
.tc-detail-result {
  font-size: 11.5px;
  line-height: 17px;
  color: var(--text-dim);
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  padding: 8px 10px;
  margin: 0;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
}

.tc-expand-enter-active, .tc-expand-leave-active {
  transition: all 0.2s ease;
  max-height: 400px;
  overflow: hidden;
}
.tc-expand-enter-from, .tc-expand-leave-to { opacity: 0; max-height: 0; margin-top: 0; }

/* ── Mobile tuning ────────────────────────────────────────────────
   On <420px viewports, 78% bubble width minus a 32px avatar + 10px
   gap leaves only ~237px usable text width, which fragments URLs,
   code blocks, and long words. 88% buys back ~25px which is the
   difference between "code overflows" and "code wraps". msg-time was
   9.5px which is below the readability floor on high-DPI mobile. */
@media (max-width: 480px) {
  .bubble-wrap { max-width: 88%; }
  .msg-time { font-size: 11px; letter-spacing: 0.08em; }
  /* YouTube embed inside a bubble-wrap was capped at 78% × 560 →
     awkward letterboxing on phones. Let it expand to full bubble. */
  .yt-embed { max-width: 100%; }
}

/* ── Memory-used chip (auto-injected recall block) ── */
.row-memory { display: flex; flex-direction: column; align-items: center; gap: 6px; margin: 2px 0; }
.memory-chip { display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
  background: var(--primary-bg); color: var(--primary); border: 1px solid var(--primary-bg-strong);
  border-radius: var(--r-full); padding: 3px 12px; font-size: 12px; }
.memory-chip:hover { background: var(--primary-bg-strong); }
.mc-chevron { transition: transform .15s; display: inline-block; }
.mc-chevron.expanded { transform: rotate(180deg); }
.memory-detail { max-width: 78%; background: var(--bg-2); border: 1px solid var(--border);
  border-radius: var(--r-md); padding: 8px 12px; }
.memory-line { font-size: 12px; color: var(--text-dim); line-height: 1.5;
  padding: 2px 0; border-bottom: 1px solid var(--border); }
.memory-line:last-child { border-bottom: none; }
</style>
