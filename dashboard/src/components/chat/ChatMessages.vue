<script setup>
import { ref, watch, nextTick, computed, reactive } from 'vue'
import { useBreakpoint } from '../../composables/useBreakpoint'
import { parseYoutubeTags, youtubeEmbedUrl } from '../../utils/youtubeTags'
import MarkdownRenderer from '../MarkdownRenderer.vue'

const props = defineProps({
  messages: { type: Array, default: () => [] },
  agent: { type: Object, default: null },
  isStreaming: { type: Boolean, default: false },
})

const scrollContainer = ref(null)
const expandedTools = reactive({}) // { messageId: boolean }
const expandedRows = reactive({})  // { "msgId-idx": boolean }
const { isMobile } = useBreakpoint()

function toggleTools(msgId) {
  expandedTools[msgId] = !expandedTools[msgId]
}

function toggleRow(msgId, idx) {
  const key = `${msgId}-${idx}`
  expandedRows[key] = !expandedRows[key]
}

function isRowExpanded(msgId, idx) {
  return !!expandedRows[`${msgId}-${idx}`]
}

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

const agentInitials = computed(() => {
  if (!props.agent?.name) return '?'
  return props.agent.name
    .split(/[\s_-]+/)
    .map(w => w[0]?.toUpperCase() || '')
    .join('')
    .slice(0, 2)
})

function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  const now = new Date()
  const diff = now - d
  if (diff < 60000) return 'just now'
  if (diff < 3600000) return `${Math.floor(diff / 60000)} min ago`
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/**
 * Group tool_call + tool_result pairs into consolidated entries.
 * Each pair shares the same `tool` name — we merge them to show one row.
 */
function groupToolCalls(toolCalls) {
  if (!toolCalls?.length) return []
  const grouped = []
  
  for (const tc of toolCalls) {
    if (tc.isResult) {
      // Try to find the matching call and merge duration + result into it
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
    if (g.duration) {
      total += parseFloat(g.duration) || 0
    }
  }
  if (total === 0) return ''
  return `${total.toFixed(1)}s`
}

function toolCount(toolCalls) {
  return groupToolCalls(toolCalls).length
}

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
  // agent__ResearchAgent → ResearchAgent (delegation)
  if (tool.includes('agent__')) return tool.replace('agent__', '')
  // serpapi__search → serpapi search
  return tool.replace(/__/g, ' › ')
}

// Parse [[[PLAY: id]]] tags out of an agent message. Called twice per render
// (text + videoIds), but the regex is trivial; if this ever shows up in a
// profile, switch to a per-message computed via a Map keyed on msg.id.
function parsedAgentContent(content) {
  return parseYoutubeTags(content)
}
</script>

<template>
  <div
    ref="scrollContainer"
    data-testid="chat-messages"
    class="flex-1 overflow-y-auto"
    :style="{ padding: isMobile ? '16px 12px' : '24px 32px' }"
  >
    <!-- Empty state -->
    <div
      v-if="!messages.length && !isStreaming"
      class="flex flex-col items-center justify-center h-full"
      style="color: var(--text-sub); text-align: center;"
    >
      <div style="font-size: 48px; margin-bottom: 16px; opacity: 0.3;">💬</div>
      <div style="font-size: 16px; font-weight: 500; margin-bottom: 4px;">
        Start a conversation
      </div>
      <div style="font-size: 13px;">
        Send a message to interact with {{ agent?.name || 'an agent' }}
      </div>
    </div>

    <!-- Messages -->
    <div class="flex flex-col" style="gap: 20px;">
      <template v-for="msg in messages" :key="msg.id">
        <!-- User message -->
        <div v-if="msg.role === 'user'" class="flex items-start justify-end" style="gap: 12px;">
          <div
            :style="{
              maxWidth: isMobile ? '85%' : '720px',
              padding: isMobile ? '10px 14px' : '12px 16px',
              borderRadius: '14px',
              background: '#1e3a5f',
              fontSize: '13px',
              fontWeight: '400',
              color: '#e0e8f5',
              lineHeight: '20px',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }"
          >
            {{ msg.content }}
          </div>
          <!-- User avatar -->
          <div
            class="flex items-center justify-center shrink-0"
            :style="{ width: isMobile ? '28px' : '32px', height: isMobile ? '28px' : '32px', borderRadius: '50%', background: '#6366f1', fontSize: isMobile ? '10px' : '12px', fontWeight: '700', color: '#fff' }"
          >
            P
          </div>
        </div>

        <!-- Agent message -->
        <div v-else class="flex items-start" style="gap: 12px;">
          <!-- Agent avatar -->
          <div
            class="flex items-center justify-center shrink-0"
            :style="{ width: isMobile ? '28px' : '32px', height: isMobile ? '28px' : '32px', borderRadius: '50%', background: '#1e3a5f', fontSize: '10px', fontWeight: '700', color: '#3b82f6' }"
          >
            {{ agentInitials }}
          </div>

          <div class="flex flex-col" :style="{ minWidth: 0, maxWidth: isMobile ? '85%' : '720px', flex: 1 }">
            <!-- Agent name + timestamp -->
            <div v-if="!msg.isStreaming" class="flex items-center" style="gap: 8px; margin-bottom: 6px;">
              <span style="font-size: 12px; font-weight: 600; color: #3b82f6;">
                {{ agent?.name || 'Agent' }}
              </span>
              <span style="font-size: 10px; color: var(--text-sub);">
                {{ formatTime(msg.timestamp) }}
              </span>
            </div>

            <!-- Content bubble -->
            <div
              v-if="msg.content || msg.isStreaming"
              class="agent-bubble"
              :style="{
                padding: '12px 16px',
                borderRadius: '14px',
                background: 'var(--bg-input)',
                border: '1px solid var(--border-input)',
                fontSize: '13px',
                fontWeight: '400',
                color: msg.isError ? '#ef4444' : 'var(--text-secondary)',
                lineHeight: '20px',
                wordBreak: 'break-word',
              }"
            >
              <!-- Typing indicator for streaming -->
              <div v-if="msg.isStreaming && !msg.content" class="flex items-center" style="gap: 4px; height: 20px;">
                <div class="typing-dot" style="animation-delay: 0s;"></div>
                <div class="typing-dot" style="animation-delay: 0.2s;"></div>
                <div class="typing-dot" style="animation-delay: 0.4s;"></div>
              </div>
              <MarkdownRenderer
                v-else
                :content="parsedAgentContent(msg.content).text"
                content-type="markdown"
              />
            </div>

            <!-- YouTube embeds — rendered when the agent emits [[[PLAY: id]]] -->
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

            <!-- Tool Calls - Compact expandable section below message -->
            <div v-if="msg.toolCalls?.length" class="tc-section" style="margin-top: 8px;">
              <!-- Collapsed header -->
              <button class="tc-header" @click="toggleTools(msg.id)">
                <div class="flex items-center" style="gap: 6px;">
                  <!-- Wrench icon -->
                  <svg width="13" height="13" viewBox="0 0 16 16" fill="none" style="opacity: 0.6;">
                    <path d="M9.77 4.23a4 4 0 0 1 2 3.27 4 4 0 0 1-1.15 3.12L6.5 14.74a1.5 1.5 0 0 1-2.12 0l-.12-.12a1.5 1.5 0 0 1 0-2.12l4.12-4.12A4 4 0 0 1 9.77 4.23z" stroke="#c4c8d4" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
                    <path d="M12 2l-1.5 1.5M14 4l-1.5 1.5M10.5 3.5L12.5 5.5" stroke="#c4c8d4" stroke-width="1.2" stroke-linecap="round"/>
                  </svg>
                  <span class="tc-summary">
                    {{ toolCount(msg.toolCalls) }} tool{{ toolCount(msg.toolCalls) > 1 ? 's' : '' }} used
                  </span>
                  <span v-if="totalDuration(msg.toolCalls)" class="tc-duration-label">
                    · {{ totalDuration(msg.toolCalls) }}
                  </span>
                </div>
                <svg
                  :class="['tc-chevron', { expanded: expandedTools[msg.id] }]"
                  width="12" height="12" viewBox="0 0 16 16" fill="none"
                >
                  <path d="M4 6L8 10L12 6" stroke="#555872" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
              </button>

              <!-- Expanded tool list -->
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
                      <!-- Icon -->
                      <div class="tc-icon">
                        <!-- Agent delegation -->
                        <svg v-if="getToolIcon(g.tool) === 'agent'" width="12" height="12" viewBox="0 0 16 16" fill="none">
                          <path d="M8 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM3 14c0-2.8 2.2-5 5-5s5 2.2 5 5" stroke="#8b8fa3" stroke-width="1.2" stroke-linecap="round"/>
                        </svg>
                        <!-- Search -->
                        <svg v-else-if="getToolIcon(g.tool) === 'search'" width="12" height="12" viewBox="0 0 16 16" fill="none">
                          <circle cx="7" cy="7" r="4" stroke="#8b8fa3" stroke-width="1.2"/>
                          <path d="M10 10l3.5 3.5" stroke="#8b8fa3" stroke-width="1.2" stroke-linecap="round"/>
                        </svg>
                        <!-- Terminal (default) -->
                        <svg v-else width="12" height="12" viewBox="0 0 16 16" fill="none">
                          <rect x="1.5" y="2.5" width="13" height="11" rx="2" stroke="#8b8fa3" stroke-width="1.2"/>
                          <path d="M5 7l2 2-2 2" stroke="#8b8fa3" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
                          <path d="M9 11h3" stroke="#8b8fa3" stroke-width="1.2" stroke-linecap="round"/>
                        </svg>
                      </div>

                      <!-- Tool name -->
                      <span class="tc-name">{{ formatToolName(g.tool) }}</span>

                      <!-- Duration + Status + Chevron -->
                      <div class="flex items-center" style="gap: 6px; margin-left: auto;">
                        <span v-if="g.duration" class="tc-dur">{{ g.duration }}</span>
                        <!-- Done checkmark -->
                        <svg v-if="g.status === 'done'" width="14" height="14" viewBox="0 0 16 16" fill="none">
                          <path d="M4.5 8.5L7 11l4.5-5" stroke="#10b981" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                        </svg>
                        <!-- Running spinner -->
                        <div v-else class="tc-spinner"></div>
                        <!-- Detail chevron -->
                        <svg
                          v-if="hasDetail(g)"
                          :class="['tc-row-chevron', { expanded: isRowExpanded(msg.id, idx) }]"
                          width="10" height="10" viewBox="0 0 16 16" fill="none"
                        >
                          <path d="M4 6L8 10L12 6" stroke="#555872" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                        </svg>
                      </div>
                    </div>

                    <!-- Expandable detail panel -->
                    <transition name="tc-expand">
                      <div v-if="isRowExpanded(msg.id, idx)" class="tc-detail">
                        <!-- Args -->
                        <div v-if="g.args && Object.keys(g.args).length" class="tc-detail-block">
                          <div class="tc-detail-label">Arguments</div>
                          <div class="tc-detail-args">
                            <div v-for="(val, key) in g.args" :key="key" class="tc-arg-row">
                              <span class="tc-arg-key">{{ key }}</span>
                              <span class="tc-arg-val">{{ val }}</span>
                            </div>
                          </div>
                        </div>
                        <!-- Result preview -->
                        <div v-if="g.resultPreview" class="tc-detail-block">
                          <div class="tc-detail-label">Result</div>
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
@keyframes typingPulse {
  0%, 60%, 100% { opacity: 0.3; transform: scale(0.8); }
  30% { opacity: 1; transform: scale(1); }
}
.typing-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-nav);
  animation: typingPulse 1.4s infinite ease-in-out;
}

/* ─── YouTube Embed ─── */
.yt-embed {
  margin-top: 8px;
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid var(--border-input, #1a1d2e);
  background: #000;
  aspect-ratio: 16 / 9;
  max-width: 560px;
}

.yt-embed iframe {
  width: 100%;
  height: 100%;
  display: block;
  border: 0;
}

/* ─── Tool Calls Section ─── */
.tc-section {
  border-radius: 10px;
}

.tc-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 7px 12px;
  background: var(--bg-input, #111318);
  border: 1px solid var(--border-primary, #1a1d2e);
  border-radius: 8px;
  cursor: pointer;
  transition: border-color 0.15s ease;
}

.tc-header:hover {
  border-color: var(--border-active, #2a3556);
}

.tc-summary {
  font-size: 11px;
  font-weight: 500;
  color: var(--text-secondary, #c4c8d4);
}

.tc-duration-label {
  font-size: 11px;
  color: var(--text-muted, #8b8fa3);
}

.tc-chevron {
  transition: transform 0.2s ease;
  flex-shrink: 0;
}

.tc-chevron.expanded {
  transform: rotate(180deg);
}

.tc-list {
  margin-top: 4px;
  border: 1px solid var(--border-primary, #1a1d2e);
  border-radius: 8px;
  overflow: hidden;
  background: var(--bg-card, #0c0e15);
}

.tc-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  min-height: 32px;
}

.tc-entry:not(:last-child) {
  border-bottom: 1px solid var(--border-primary, #1a1d2e);
}

.tc-row-clickable {
  cursor: pointer;
  transition: background 0.12s ease;
}

.tc-row-clickable:hover {
  background: var(--bg-input, #111318);
}

.tc-row-chevron {
  transition: transform 0.2s ease;
  flex-shrink: 0;
  margin-left: 2px;
}

.tc-row-chevron.expanded {
  transform: rotate(180deg);
}

.tc-icon {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 5px;
  background: var(--bg-input, #111318);
}

.tc-name {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary, #c4c8d4);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tc-dur {
  font-size: 10px;
  font-weight: 500;
  color: var(--text-muted, #8b8fa3);
  padding: 1px 5px;
  background: var(--bg-input, #111318);
  border-radius: 4px;
  white-space: nowrap;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.tc-spinner {
  width: 12px;
  height: 12px;
  border: 1.5px solid var(--border-primary, #1a1d2e);
  border-top-color: var(--text-muted, #8b8fa3);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

/* ─── Detail Panel ─── */
.tc-detail {
  padding: 8px 12px 10px 42px; /* 42px = icon(22) + gap(8) + row padding(12) */
  background: var(--bg-main, #0a0d14);
  border-top: 1px solid var(--border-primary, #1a1d2e);
}

.tc-detail-block + .tc-detail-block {
  margin-top: 10px;
}

.tc-detail-label {
  font-size: 10px;
  font-weight: 600;
  color: var(--text-sub, #555872);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}

.tc-detail-args {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.tc-arg-row {
  display: flex;
  gap: 8px;
  font-size: 11px;
  line-height: 18px;
}

.tc-arg-key {
  color: var(--text-muted, #8b8fa3);
  flex-shrink: 0;
  min-width: 60px;
}

.tc-arg-key::after {
  content: ':';
}

.tc-arg-val {
  color: var(--text-secondary, #c4c8d4);
  word-break: break-word;
}

.tc-detail-result {
  font-size: 11px;
  line-height: 17px;
  color: var(--text-secondary, #c4c8d4);
  background: var(--bg-input, #111318);
  border: 1px solid var(--border-primary, #1a1d2e);
  border-radius: 6px;
  padding: 8px 10px;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
  font-family: 'JetBrains Mono', 'SF Mono', 'Fira Code', monospace;
}

/* Transition */
.tc-expand-enter-active,
.tc-expand-leave-active {
  transition: all 0.2s ease;
  max-height: 400px;
  overflow: hidden;
}

.tc-expand-enter-from,
.tc-expand-leave-to {
  opacity: 0;
  max-height: 0;
  margin-top: 0;
}
</style>
