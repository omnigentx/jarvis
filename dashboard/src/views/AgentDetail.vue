<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAgentsStore } from '../stores/agents'
import { apiFetch } from '../api'
import StatusBadge from '../components/StatusBadge.vue'

const route = useRoute()
const router = useRouter()
const store = useAgentsStore()
const agentDetail = ref(null)
const validTabs = ['overview', 'skills', 'servers', 'instruction', 'context', 'activity']
const initialTab = validTabs.includes(route.query.tab) ? route.query.tab : 'overview'
const activeTab = ref(initialTab)
const isLoading = ref(true)
const expandedServers = reactive({})
const expandedTools = reactive({})
const expandedSkills = reactive({})

// Timeline (persistent history)
const timelineEvents = ref([])
const timelineLoading = ref(false)
const timelineFetched = ref(false)

// Context window snapshots
const contextSnapshots = ref([])
const contextLoading = ref(false)
const contextFetched = ref(false)
const expandedSnapshot = ref(null)
const snapshotMessages = ref([])
const messagesLoading = ref(false)
const expandedMessages = ref(new Set())

const agentName = computed(() => route.params.name)
const liveAgent = computed(() => store.agents.get(agentName.value))

onMounted(async () => {
  try {
    agentDetail.value = await apiFetch(`/api/agents/${agentName.value}`)
  } catch (e) {
    console.error('Failed to load agent detail:', e)
  } finally {
    isLoading.value = false
  }
})

const tabs = [
  { id: 'overview', label: 'Overview' },
  { id: 'skills', label: 'Skills' },
  { id: 'servers', label: 'MCP Servers' },
  { id: 'instruction', label: 'Instruction' },
  { id: 'context', label: 'Context Window' },
  { id: 'activity', label: 'History' },
]

// Merge REST detail with live SSE state
const agent = computed(() => {
  if (!agentDetail.value) return null
  return {
    ...agentDetail.value,
    status: liveAgent.value?.status || agentDetail.value.status || 'idle',
    lastAction: liveAgent.value?.lastAction || null,
    lastError: liveAgent.value?.lastError || null,
  }
})

// Agent initial letter for avatar
const agentInitial = computed(() => (agent.value?.name || '?')[0].toUpperCase())

// Instruction preview (first 12 lines)
const instructionPreview = computed(() => {
  const instr = agent.value?.instruction || ''
  const lines = instr.split('\n')
  if (lines.length <= 12) return instr
  return lines.slice(0, 12).join('\n')
})
const instructionLineCount = computed(() => {
  const instr = agent.value?.instruction || ''
  return instr.split('\n').length
})
const hasMoreInstruction = computed(() => instructionLineCount.value > 12)

// Recent SSE events for this specific agent (live, ephemeral)
const liveEvents = computed(() =>
  store.recentEvents.filter(e => e.agent_name === agentName.value).slice(0, 20)
)

// Merged history: persistent timeline + live SSE (deduped)
const mergedHistory = computed(() => {
  // Normalize timeline events
  const persistent = timelineEvents.value.map(e => ({
    id: `${e.source}_${e.timestamp}_${e.type}`,
    source: e.source,
    event_type: e.type,
    agent: e.agent,
    message: e.content,
    timestamp: e.timestamp,
    metadata: e.metadata || {},
  }))

  // Normalize live SSE events
  const live = liveEvents.value.map(e => ({
    id: `sse_${e.timestamp}_${e.event_type}`,
    source: 'sse',
    event_type: e.event_type,
    agent: e.agent_name,
    message: e.message,
    timestamp: e.timestamp,
    metadata: {},
  }))

  // Merge and dedupe by id
  const seen = new Set()
  const all = [...live, ...persistent]
  return all
    .filter(e => { if (seen.has(e.id)) return false; seen.add(e.id); return true })
    .sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0))
    .slice(0, 50)
})

// Fetch persistent timeline from backend
async function fetchTimeline() {
  if (timelineFetched.value || timelineLoading.value) return
  timelineLoading.value = true
  try {
    const data = await apiFetch(`/api/agent/timeline?agent=${agentName.value}&limit=50`)
    timelineEvents.value = data.events || []
    timelineFetched.value = true
  } catch (e) {
    console.error('Failed to load timeline:', e)
  } finally {
    timelineLoading.value = false
  }
}

// Lazy load timeline when History tab is activated
watch(activeTab, (tab) => {
  if (tab === 'activity') fetchTimeline()
  if (tab === 'context') fetchContextSnapshots()
})

// Fetch context snapshots from backend
async function fetchContextSnapshots() {
  if (contextFetched.value || contextLoading.value) return
  contextLoading.value = true
  try {
    const data = await apiFetch(`/api/agents/${agentName.value}/context?limit=10`)
    contextSnapshots.value = data.snapshots || []
    contextFetched.value = true
  } catch (e) {
    console.error('Failed to load context snapshots:', e)
  } finally {
    contextLoading.value = false
  }
}

async function toggleSnapshotMessages(snapshot) {
  if (expandedSnapshot.value === snapshot.id) {
    expandedSnapshot.value = null
    snapshotMessages.value = []
    return
  }
  expandedSnapshot.value = snapshot.id
  messagesLoading.value = true
  try {
    const data = await apiFetch(`/api/agents/${agentName.value}/context/${snapshot.id}/messages`)
    snapshotMessages.value = data.messages || []
  } catch (e) {
    console.error('Failed to load context messages:', e)
    snapshotMessages.value = []
  } finally {
    messagesLoading.value = false
  }
}

function formatTokens(n) {
  if (!n) return '0'
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M'
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K'
  return String(n)
}

function triggerLabel(trigger) {
  const map = { task_complete: '✅ Task Complete', idle: '💤 Idle', error: '❌ Error', manual: '🔧 Manual' }
  return map[trigger] || trigger
}

function roleClass(role) {
  if (role === 'user' || role === 'Role.USER') return 'role-user'
  if (role === 'assistant' || role === 'Role.ASSISTANT') return 'role-assistant'
  return 'role-system'
}

function roleLabel(role) {
  if (role === 'user' || role === 'Role.USER') return 'User'
  if (role === 'assistant' || role === 'Role.ASSISTANT') return 'Assistant'
  return 'System'
}

function truncateContent(content, maxLen = 300) {
  if (!content || content.length <= maxLen) return content
  return content.slice(0, maxLen) + '…'
}

function toggleMessage(idx) {
  if (expandedMessages.value.has(idx)) {
    expandedMessages.value.delete(idx)
  } else {
    expandedMessages.value.add(idx)
  }
}

// Per-server tool data for accordion display
const serverTools = computed(() => {
  const tools = agent.value?.tools || {}
  const servers = agent.value?.servers || []
  return servers.map(srv => {
    const srvTools = tools[srv] || []
    return {
      name: srv,
      connected: true,
      tools: srvTools.map(t => {
        if (typeof t === 'string') return { name: t, description: '' }
        return { name: t.name, description: t.description || '' }
      }),
    }
  })
})

// All tools flattened for display (handles both formats)
const allTools = computed(() => {
  const tools = agent.value?.tools || {}
  const result = []
  for (const [server, toolList] of Object.entries(tools)) {
    for (const tool of toolList) {
      const name = typeof tool === 'string' ? tool : tool.name
      result.push({ server, name })
    }
  }
  return result
})

// Total tool count across all servers
const totalToolCount = computed(() => allTools.value.length)

function toggleServer(srv) {
  expandedServers[srv] = !expandedServers[srv]
}

function toggleTool(serverName, toolName) {
  const key = `${serverName}/${toolName}`
  expandedTools[key] = !expandedTools[key]
}

function isToolExpanded(serverName, toolName) {
  return !!expandedTools[`${serverName}/${toolName}`]
}

function toggleSkill(skillName) {
  expandedSkills[skillName] = !expandedSkills[skillName]
}

function formatTime(ts) {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleTimeString()
}

function formatDate(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const now = new Date()
  const isToday = d.toDateString() === now.toDateString()
  if (isToday) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function historyBadgeClass(type) {
  if (!type) return 'badge-default'
  if (type.startsWith('meeting_')) return 'badge-meeting'
  if (type.startsWith('inbox_')) return 'badge-inbox'
  if (type.startsWith('spawn_')) return 'badge-spawn'
  if (type === 'tool_call') return 'badge-tool'
  if (type === 'tool_result' || type === 'result') return 'badge-success'
  if (type === 'thinking') return 'badge-thinking'
  if (type === 'error') return 'badge-error'
  return 'badge-default'
}

function historyBadgeLabel(type) {
  if (!type) return 'event'
  if (type.startsWith('meeting_')) return type.replace('meeting_', '📋 ')
  if (type.startsWith('inbox_')) return type.replace('inbox_', '📨 ')
  if (type.startsWith('spawn_')) return type.replace('spawn_', '🚀 ')
  return type
}
</script>

<template>
  <div class="agent-detail">


    <!-- Loading -->
    <div v-if="isLoading" class="loading-state">
      <div class="loading-text">Loading agent details...</div>
    </div>

    <template v-else-if="agent">
      <!-- Agent Header Card -->
      <div class="header-card">
        <div class="header-left">
          <div class="agent-avatar" :class="{ 'avatar-running': agent.status === 'running' }">
            {{ agentInitial }}
          </div>
          <div class="header-info">
            <div class="header-name-row">
              <h1 class="agent-name">{{ agent.name }}</h1>
              <StatusBadge :status="agent.status || 'idle'" />
              <span v-if="agent.is_default" class="badge badge-master">Master</span>
            </div>
            <p class="header-meta">
              {{ agent.description || 'AI Agent' }}
              · {{ agent.type }}
              · {{ agent.model || 'openai.gpt-4o-mini' }}
            </p>
          </div>
        </div>
        <div class="header-actions">
          <button class="btn btn-outline" disabled>Restart</button>
          <router-link
            v-if="agent.is_default"
            :to="{ path: '/agents/' + agent.name + '/inject' }"
            class="btn btn-primary"
          >
            Inject Prompt
          </router-link>
        </div>
      </div>

      <!-- Tabs -->
      <div class="tabs-bar">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          @click="activeTab = tab.id"
          class="tab-item"
          :class="{ active: activeTab === tab.id }"
        >
          {{ tab.label }}
        </button>
      </div>

      <!-- ===== OVERVIEW TAB ===== -->
      <div v-if="activeTab === 'overview'" class="animate-fade-in">
        <!-- Stats Row (full width) -->
        <div class="stats-row">
          <div class="stat-card">
            <span class="stat-label">Model</span>
            <span class="stat-value stat-green">{{ agent.model?.includes('.') ? agent.model.slice(agent.model.indexOf('.') + 1) : (agent.model || '—') }}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">Type</span>
            <span class="stat-value stat-blue">{{ agent.type }}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">Skills</span>
            <span class="stat-value stat-purple">{{ agent.skills?.length || 0 }}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">Servers</span>
            <span class="stat-value stat-orange">{{ agent.servers?.length || 0 }}</span>
          </div>
        </div>

        <!-- 2-column panels -->
        <div class="overview-columns">
          <!-- Left Column -->
          <div class="overview-col">
            <!-- Skills Panel -->
            <div class="panel" v-if="agent.skills?.length">
              <div class="panel-header">
                <h3>Skills ({{ agent.skills.length }})</h3>
                <button class="view-all-link" @click="activeTab = 'skills'">View All →</button>
              </div>
              <div class="skill-list">
                <div v-for="skill in agent.skills" :key="skill.name" class="skill-item">
                  <span class="skill-icon">⚡</span>
                  <div class="skill-info">
                    <span class="skill-name">{{ skill.name }}</span>
                    <span v-if="skill.description" class="skill-desc">{{ skill.description }}</span>
                  </div>
                </div>
              </div>
            </div>

            <!-- Child Agents -->
            <div class="panel" v-if="agent.child_agents?.length">
              <div class="panel-header">
                <h3>Sub-agents ({{ agent.child_agents.length }})</h3>
              </div>
              <div class="server-list">
                <router-link
                  v-for="child in agent.child_agents"
                  :key="child"
                  :to="'/agents/' + child"
                  class="server-item"
                >
                  <div class="server-icon">🤖</div>
                  <span class="server-name">{{ child }}</span>
                  <StatusBadge 
                    :status="store.agents.get(child)?.status || 'idle'" 
                    class="ml-auto"
                  />
                </router-link>
              </div>
            </div>
          </div>
          
          <!-- Right Column -->
          <div class="overview-col">
            <!-- MCP Servers Panel -->
            <div class="panel" v-if="agent.servers?.length">
              <div class="panel-header">
                <h3>MCP Servers ({{ agent.servers.length }})</h3>
                <button class="view-all-link" @click="activeTab = 'servers'">View All →</button>
              </div>
              <div class="server-list">
                <div v-for="srv in agent.servers" :key="srv" class="server-item">
                  <div class="server-icon">🔌</div>
                  <span class="server-name">{{ srv }}</span>
                  <span class="badge badge-connected">● Connected</span>
                </div>
              </div>
            </div>

            <!-- Instruction Preview -->
            <div class="panel" v-if="agent.instruction">
              <div class="panel-header">
                <h3>Instruction (preview)</h3>
                <button class="view-all-link" @click="activeTab = 'instruction'">Expand →</button>
              </div>
              <pre class="instruction-preview">{{ instructionPreview }}</pre>
              <div v-if="hasMoreInstruction" class="instruction-more">
                … {{ instructionLineCount - 12 }} more lines
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- ===== SKILLS TAB ===== -->
      <div v-else-if="activeTab === 'skills'" class="animate-fade-in">
        <div v-if="agent.skills?.length" class="accordion-list">
          <div
            v-for="skill in agent.skills"
            :key="skill.name"
            class="accordion-card skill-accordion-card"
            :class="{ 'accordion-expanded': expandedSkills[skill.name] }"
          >
            <!-- Skill Header -->
            <button
              class="accordion-header skill-accordion-header"
              @click="toggleSkill(skill.name)"
              :aria-expanded="expandedSkills[skill.name]"
            >
              <div class="accordion-header-left">
                <span class="skill-accordion-icon">⚡</span>
                <div class="skill-header-info">
                  <span class="accordion-title">{{ skill.name }}</span>
                  <span v-if="skill.description" class="skill-header-preview">
                    {{ skill.description }}
                  </span>
                </div>
              </div>
              <div class="accordion-header-right">
                <span class="skill-tag" v-if="skill.type">{{ skill.type }}</span>
                <span
                  class="accordion-chevron"
                  :class="{ 'chevron-open': expandedSkills[skill.name] }"
                >›</span>
              </div>
            </button>

            <!-- Skill Expanded Body: content only (description stays in header) -->
            <div v-if="expandedSkills[skill.name]" class="accordion-body skill-accordion-body">
              <div v-if="skill.content" class="skill-content-block">
                <pre class="skill-content-pre">{{ skill.content }}</pre>
              </div>
            </div>
          </div>
        </div>
        <div v-else class="empty-state">No skills configured</div>
      </div>

      <!-- ===== MCP SERVERS TAB ===== -->
      <div v-else-if="activeTab === 'servers'" class="animate-fade-in">
        <div v-if="serverTools.length" class="accordion-list">
          <div
            v-for="srv in serverTools"
            :key="srv.name"
            class="accordion-card"
            :class="{ 'accordion-expanded': expandedServers[srv.name] }"
          >
            <!-- Accordion Header -->
            <button class="accordion-header" @click="toggleServer(srv.name)">
              <div class="accordion-header-left">
                <span class="accordion-icon">🔌</span>
                <span class="accordion-title">{{ srv.name }}</span>
                <span class="badge badge-connected" v-if="srv.connected">● Connected</span>
                <span class="badge badge-disconnected" v-else>● Disconnected</span>
              </div>
              <div class="accordion-header-right">
                <span class="accordion-tool-count" v-if="srv.tools.length">
                  {{ srv.tools.length }} tool{{ srv.tools.length !== 1 ? 's' : '' }}
                </span>
                <span class="accordion-tool-count muted" v-else>Tools not available</span>
                <span class="accordion-chevron" :class="{ 'chevron-open': expandedServers[srv.name] }">›</span>
              </div>
            </button>

            <!-- Accordion Body -->
            <div v-if="expandedServers[srv.name] && srv.tools.length" class="accordion-body">
              <div
                v-for="tool in srv.tools"
                :key="tool.name"
                class="accordion-tool-item"
                :class="{ 'tool-clickable': !!tool.description }"
                @click="tool.description && toggleTool(srv.name, tool.name)"
              >
                <span class="accordion-tool-icon">🔧</span>
                <div class="accordion-tool-info">
                  <span class="accordion-tool-name">{{ tool.name }}</span>
                  <span
                    v-if="tool.description"
                    class="accordion-tool-desc"
                    :class="{ 'desc-expanded': isToolExpanded(srv.name, tool.name) }"
                  >{{ tool.description }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div v-else class="empty-state">No MCP servers connected</div>
      </div>

      <!-- ===== INSTRUCTION TAB ===== -->
      <div v-else-if="activeTab === 'instruction'" class="animate-fade-in">
        <div class="panel">
          <pre class="instruction-full">{{ agent.instruction || 'No instruction configured' }}</pre>
        </div>
      </div>

      <!-- ===== CONTEXT WINDOW TAB ===== -->
      <div v-else-if="activeTab === 'context'" class="animate-fade-in">
        <!-- Loading -->
        <div v-if="contextLoading && !contextSnapshots.length" class="loading-state">
          <div class="loading-text">Loading context snapshots...</div>
        </div>

        <!-- Empty -->
        <div v-else-if="!contextSnapshots.length" class="empty-state">
          No context window snapshots recorded yet
        </div>

        <!-- Snapshot list -->
        <div v-else class="context-list">
          <div
            v-for="snap in contextSnapshots"
            :key="snap.id"
            class="context-card"
            :class="{ 'context-card-expanded': expandedSnapshot === snap.id }"
          >
            <!-- Snapshot header (clickable) -->
            <div class="context-header" @click="toggleSnapshotMessages(snap)">
              <div class="context-header-left">
                <span class="context-trigger">{{ triggerLabel(snap.trigger) }}</span>
                <span class="context-time">{{ formatDate(snap.created_at) }}</span>
              </div>
              <div class="context-stats">
                <span class="context-stat">
                  <span class="stat-icon">💬</span>
                  {{ snap.message_count }} msgs
                </span>
                <span class="context-stat">
                  <span class="stat-icon">📥</span>
                  {{ formatTokens(snap.total_input_tokens) }}
                </span>
                <span class="context-stat">
                  <span class="stat-icon">📤</span>
                  {{ formatTokens(snap.total_output_tokens) }}
                </span>
                <span class="context-run-id" v-if="snap.run_id">
                  {{ snap.run_id.slice(0, 8) }}
                </span>
                <span class="expand-icon">{{ expandedSnapshot === snap.id ? '▼' : '▶' }}</span>
              </div>
            </div>

            <!-- Expanded messages -->
            <div v-if="expandedSnapshot === snap.id" class="context-messages">
              <div v-if="messagesLoading" class="loading-text" style="padding: 16px;">
                Loading messages...
              </div>
              <div v-else-if="!snapshotMessages.length" class="empty-state" style="padding: 16px;">
                No messages in this snapshot
              </div>
              <div v-else class="messages-scroll">
                <div
                  v-for="(msg, idx) in snapshotMessages"
                  :key="idx"
                  class="context-msg"
                  :class="[roleClass(msg.role), { 'msg-expanded': expandedMessages.has(idx) }]"
                  @click="toggleMessage(idx)"
                >
                  <div class="msg-header">
                    <span class="msg-role-badge" :class="roleClass(msg.role)">
                      {{ roleLabel(msg.role) }}
                    </span>
                    <span v-if="msg.has_tool_calls" class="msg-tool-badge">
                      🔧 {{ msg.tool_count }} tool{{ msg.tool_count > 1 ? 's' : '' }}
                    </span>
                    <span v-if="msg.has_tool_results" class="msg-tool-badge result">
                      📊 result
                    </span>
                    <span class="msg-index">#{{ idx + 1 }}</span>
                  </div>
                  <pre class="msg-content" :class="{ 'msg-content-full': expandedMessages.has(idx) }">{{ expandedMessages.has(idx) ? msg.content : truncateContent(msg.content) }}</pre>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- ===== HISTORY TAB ===== -->
      <div v-else-if="activeTab === 'activity'" class="animate-fade-in">
        <!-- Loading state -->
        <div v-if="timelineLoading && !mergedHistory.length" class="loading-state">
          <div class="loading-text">Loading history...</div>
        </div>

        <!-- Empty state -->
        <div v-else-if="!mergedHistory.length" class="empty-state">
          No activity recorded for this agent
        </div>

        <!-- History list -->
        <div v-else class="activity-list">
          <div
            v-for="event in mergedHistory"
            :key="event.id"
            class="activity-item"
          >
            <span class="activity-time">{{ formatDate(event.timestamp) }}</span>
            <div class="activity-body">
              <span
                class="activity-badge"
                :class="historyBadgeClass(event.event_type)"
              >
                {{ historyBadgeLabel(event.event_type) }}
              </span>
              <span class="activity-message">{{ event.message }}</span>
              <span v-if="event.metadata?.agenda" class="activity-meta">
                📌 {{ event.metadata.agenda }}
              </span>
              <span v-if="event.metadata?.to" class="activity-meta">
                → {{ event.metadata.to }}
              </span>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- Not found -->
    <div v-else class="empty-state" style="padding: 80px 0;">
      <div style="font-size: 2.5rem; margin-bottom: 0.5rem;">❓</div>
      <h3 style="color: var(--color-text-primary)">Agent not found</h3>
      <p style="color: var(--color-text-muted)">{{ agentName }}</p>
    </div>
  </div>
</template>

<style scoped>
.agent-detail {
  max-width: 1200px;
  margin: 0 auto;
}

/* ── Header Card ── */
.header-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--color-bg-card, #0c0e15);
  border: 1px solid var(--color-border, #1a1d2e);
  border-radius: 12px;
  padding: 20px 24px;
  margin-bottom: 20px;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}
.agent-avatar {
  width: 52px;
  height: 52px;
  border-radius: 50%;
  background: linear-gradient(135deg, #1e3a5f, #2a3556);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 22px;
  font-weight: 700;
  color: #c4c8d4;
  flex-shrink: 0;
  border: 2px solid transparent;
  transition: border-color 0.3s;
}
.avatar-running {
  border-color: #10b981;
  box-shadow: 0 0 12px rgba(16, 185, 129, 0.25);
}
.header-name-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.agent-name {
  font-size: 20px;
  font-weight: 700;
  color: var(--color-text-primary, #f0f2f5);
  margin: 0;
}
.header-meta {
  font-size: 12px;
  color: var(--color-text-muted, #8b8fa3);
  margin-top: 4px;
}
.header-actions {
  display: flex;
  gap: 10px;
}

/* ── Badges ── */
.badge {
  font-size: 11px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 20px;
  white-space: nowrap;
}
.badge-master {
  background: rgba(99, 102, 241, 0.15);
  color: #818cf8;
}
.badge-connected {
  font-size: 11px;
  color: #10b981;
  margin-left: auto;
}
.badge-disconnected {
  font-size: 11px;
  color: #ef4444;
  margin-left: auto;
}

/* ── Buttons ── */
.btn {
  font-size: 13px;
  font-weight: 500;
  padding: 8px 20px;
  border-radius: 8px;
  border: none;
  cursor: pointer;
  text-decoration: none;
  transition: all 0.15s;
}
.btn-outline {
  background: transparent;
  border: 1px solid var(--color-border, #1a1d2e);
  color: var(--color-text-secondary, #c4c8d4);
}
.btn-outline:hover:not(:disabled) { border-color: var(--color-text-muted); }
.btn-outline:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-primary {
  background: #ef4444;
  color: white;
}
.btn-primary:hover { background: #dc2626; }

/* ── Tabs ── */
.tabs-bar {
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--color-border, #1a1d2e);
  margin-bottom: 20px;
}
.tab-item {
  padding: 10px 20px;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-muted, #8b8fa3);
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  cursor: pointer;
  transition: all 0.15s;
}
.tab-item:hover { color: var(--color-text-secondary, #c4c8d4); }
.tab-item.active {
  color: var(--color-accent, #00d4aa);
  border-bottom-color: var(--color-accent, #00d4aa);
}

/* ── Overview 2-column Layout ── */
.overview-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.overview-col {
  min-width: 0; /* prevent grid blowout from long text */
}
@media (max-width: 900px) {
  .overview-columns { grid-template-columns: 1fr; }
}

/* ── Stats Row ── */
.stats-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-bottom: 16px;
}
.stat-card {
  background: var(--color-bg-card, #0c0e15);
  border: 1px solid var(--color-border, #1a1d2e);
  border-radius: 10px;
  padding: 14px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.stat-label {
  font-size: 11px;
  color: var(--color-text-muted, #8b8fa3);
  text-transform: uppercase;
  letter-spacing: 0.3px;
}
.stat-value {
  font-size: 16px;
  font-weight: 700;
}
.stat-green { color: #10b981; }
.stat-blue { color: #3b82f6; }
.stat-purple { color: #818cf8; }
.stat-orange { color: #f59e0b; }

/* ── Panel ── */
.panel {
  background: var(--color-bg-card, #0c0e15);
  border: 1px solid var(--color-border, #1a1d2e);
  border-radius: 12px;
  padding: 16px;
  margin-bottom: 16px;
}
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.panel-header h3 {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text-primary, #f0f2f5);
  margin: 0;
}
.view-all-link {
  font-size: 12px;
  font-weight: 500;
  color: var(--color-accent, #00d4aa);
  background: none;
  border: none;
  cursor: pointer;
}
.view-all-link:hover { text-decoration: underline; }

/* ── Skills (Overview panel – compact list) ── */
.skill-list { display: flex; flex-direction: column; gap: 4px; }
.skill-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(255,255,255,0.02);
}
.skill-icon {
  font-size: 16px;
  flex-shrink: 0;
  margin-top: 1px;
}
.skill-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.skill-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-primary, #f0f2f5);
}
.skill-desc {
  font-size: 11px;
  color: var(--color-text-muted, #8b8fa3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── Skills Tab Accordion ── */
.skill-accordion-card {
  /* inherits accordion-card styles */
}
.skill-accordion-header {
  /* inherits accordion-header styles */
}
.skill-accordion-icon {
  font-size: 20px;
  flex-shrink: 0;
  filter: drop-shadow(0 0 4px rgba(0, 212, 170, 0.3));
}
.skill-header-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  text-align: left;
}
.skill-header-preview {
  font-size: 11px;
  color: var(--color-text-muted, #8b8fa3);
  white-space: normal;
  word-break: break-word;
}
.skill-tag {
  font-size: 10px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 20px;
  background: rgba(0, 212, 170, 0.1);
  color: #00d4aa;
  white-space: nowrap;
}
.skill-accordion-body {
  border-top: 1px solid var(--color-border, #1a1d2e);
  padding: 16px;
}
.skill-full-desc {
  font-size: 13px;
  line-height: 1.7;
  color: var(--color-text-secondary, #c4c8d4);
  margin-bottom: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}
.skill-content-block {
  background: rgba(0, 0, 0, 0.25);
  border: 1px solid var(--color-border, #1a1d2e);
  border-radius: 8px;
  overflow: hidden;
}
.skill-content-pre {
  font-family: 'SF Mono', 'JetBrains Mono', 'Cascadia Code', monospace;
  font-size: 12px;
  line-height: 1.7;
  color: var(--color-text-secondary, #c4c8d4);
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
  padding: 14px;
  max-height: 400px;
  overflow-y: auto;
}
.skill-no-content {
  font-size: 12px;
  color: var(--color-text-subtle, #555872);
  font-style: italic;
  text-align: center;
  padding: 8px 0;
}

/* ── Servers ── */
.server-list { display: flex; flex-direction: column; gap: 4px; }
.server-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(255,255,255,0.02);
  text-decoration: none;
  transition: background 0.15s;
}
.server-icon {
  font-size: 18px;
  flex-shrink: 0;
}
.server-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-primary, #f0f2f5);
}

/* ── Instruction ── */
.instruction-preview, .instruction-full {
  font-family: 'SF Mono', 'JetBrains Mono', 'Cascadia Code', monospace;
  font-size: 12px;
  line-height: 1.7;
  color: var(--color-text-secondary, #c4c8d4);
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
  background: rgba(0,0,0,0.2);
  padding: 12px;
  border-radius: 8px;
  max-height: 280px;
  overflow: hidden;
}
.instruction-full {
  max-height: none;
  overflow: auto;
}
.instruction-more {
  font-size: 11px;
  color: var(--color-text-subtle, #555872);
  padding-top: 8px;
  text-align: center;
}

/* ── Accordion (MCP Servers Tab) ── */
.accordion-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.accordion-card {
  background: var(--color-bg-card, #0c0e15);
  border: 1px solid var(--color-border, #1a1d2e);
  border-radius: 12px;
  overflow: hidden;
  transition: border-color 0.2s;
}
.accordion-card:hover {
  border-color: #2a3556;
}
.accordion-expanded {
  border-color: #2a3556;
}
.accordion-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 14px 16px;
  background: none;
  border: none;
  cursor: pointer;
  color: inherit;
  text-align: left;
  transition: background 0.15s;
}
.accordion-header:hover {
  background: rgba(255, 255, 255, 0.02);
}
.accordion-header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}
.accordion-header-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.accordion-icon {
  font-size: 18px;
  flex-shrink: 0;
}
.accordion-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-primary, #f0f2f5);
}
.accordion-tool-count {
  font-size: 11px;
  color: var(--color-text-muted, #8b8fa3);
}
.accordion-tool-count.muted {
  color: var(--color-text-subtle, #555872);
}
.accordion-chevron {
  font-size: 18px;
  font-weight: 300;
  color: var(--color-text-subtle, #555872);
  transition: transform 0.2s ease;
  transform: rotate(0deg);
}
.chevron-open {
  transform: rotate(90deg);
}
.accordion-body {
  padding: 0 16px 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  animation: fadeIn 0.15s ease-out;
}
.accordion-tool-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 8px 12px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.015);
}
.accordion-tool-icon {
  font-size: 14px;
  flex-shrink: 0;
  margin-top: 2px;
}
.accordion-tool-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.accordion-tool-name {
  font-size: 12px;
  font-weight: 500;
  color: var(--color-text-primary, #f0f2f5);
}
.accordion-tool-desc {
  font-size: 11px;
  color: var(--color-text-muted, #8b8fa3);
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  transition: all 0.2s ease;
  cursor: pointer;
}
.accordion-tool-desc.desc-expanded {
  -webkit-line-clamp: unset;
  line-clamp: unset;
  display: block;
}
.tool-clickable {
  cursor: pointer;
  transition: background 0.15s ease;
}
.tool-clickable:hover {
  background: rgba(255, 255, 255, 0.03);
}

/* ── History source badges ── */
.badge-meeting { background: rgba(139, 92, 246, 0.12); color: #a78bfa; }
.badge-inbox   { background: rgba(59, 130, 246, 0.12); color: #60a5fa; }
.badge-spawn   { background: rgba(16, 185, 129, 0.12); color: #34d399; }

.activity-meta {
  display: block;
  font-size: 11px;
  color: var(--color-text-subtle, #555872);
  margin-top: 2px;
}

/* ── Activity ── */
.activity-list { display: flex; flex-direction: column; gap: 4px; }
.activity-item {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 10px 14px;
  border-radius: 8px;
  background: var(--color-bg-card, #0c0e15);
}
.activity-time {
  font-size: 11px;
  color: var(--color-text-subtle, #555872);
  white-space: nowrap;
  margin-top: 2px;
}
.activity-body {
  flex: 1;
  min-width: 0;
}
.activity-badge {
  font-size: 10px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
  margin-right: 8px;
}
.badge-tool { background: rgba(59,130,246,0.12); color: #60a5fa; }
.badge-success { background: rgba(16,185,129,0.12); color: #34d399; }
.badge-thinking { background: rgba(245,158,11,0.12); color: #fbbf24; }
.badge-error { background: rgba(239,68,68,0.12); color: #f87171; }
.badge-default { background: rgba(100,116,139,0.12); color: #94a3b8; }
.activity-message {
  font-size: 13px;
  color: var(--color-text-secondary, #c4c8d4);
}

/* ── Empty State ── */
.empty-state {
  text-align: center;
  padding: 48px 0;
  font-size: 13px;
  color: var(--color-text-muted, #8b8fa3);
}

/* ── Loading ── */
.loading-state {
  display: flex;
  justify-content: center;
  padding: 80px 0;
}
.loading-text {
  color: var(--color-text-muted, #8b8fa3);
  animation: pulse 1.5s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

/* ── Animations ── */
.animate-fade-in {
  animation: fadeIn 0.15s ease-out;
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

/* ════════════════════════════════════════════════
   MOBILE RESPONSIVE — max-width: 767px
   ════════════════════════════════════════════════ */
@media (max-width: 767px) {
  .agent-detail {
    max-width: 100%;
    /* no padding — AppLayout handles edge padding */
  }


  .desktop-only { display: none !important; }

  /* ── Header card ── */
  .header-card {
    flex-direction: column;
    align-items: flex-start;
    gap: 14px;
    border-radius: 0;
    border-left: none;
    border-right: none;
    border-top: none;
    padding: 14px;
    margin-bottom: 0;
  }

  .header-left { gap: 12px; }

  .agent-avatar {
    width: 44px;
    height: 44px;
    font-size: 18px;
  }

  .agent-name { font-size: 16px; }

  .header-meta { font-size: 11px; }

  /* Hide restart/inject buttons on mobile — use Inject tab instead */
  .header-actions { display: none; }

  /* ── Tabs bar: horizontal scroll ── */
  .tabs-bar {
    overflow-x: auto;
    scrollbar-width: none;
    margin-bottom: 0;
    border-bottom: 1px solid var(--color-border, #1a1d2e);
    -webkit-overflow-scrolling: touch;
  }

  .tabs-bar::-webkit-scrollbar { display: none; }

  .tab-item {
    padding: 10px 16px;
    font-size: 13px;
    white-space: nowrap;
    flex-shrink: 0;
  }

  /* ── Overview: 1-column ── */
  .stats-row {
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    padding: 12px 14px;
    background: transparent;
    margin-bottom: 0;
  }

  .stat-card {
    padding: 10px 12px;
    border-radius: 8px;
  }

  .stat-value { font-size: 14px; }

  .overview-columns {
    grid-template-columns: 1fr;
    padding: 0 14px;
    gap: 12px;
  }

  .panel {
    margin-bottom: 12px;
    border-radius: 8px;
    padding: 12px 14px;
  }

  /* ── Activity / History ── */
  .activity-item {
    flex-direction: column;
    gap: 4px;
    padding: 10px 14px;
  }

  .activity-time {
    font-size: 10px;
  }

  .activity-message {
    font-size: 12px;
    word-break: break-word;
  }

  /* ── Accordion (MCP servers) ── */
  .accordion-header {
    padding: 12px 14px;
  }

  /* ── Instruction ── */
  .instruction-preview {
    font-size: 11px;
    max-height: 200px;
  }

  /* ── Context Window ── */
  .context-header {
    flex-direction: column;
    align-items: flex-start;
    padding: 12px 14px;
    gap: 8px;
  }
  .context-header-left {
    width: 100%;
  }
  .context-stats {
    width: 100%;
    flex-wrap: wrap;
    gap: 8px 12px;
  }
  .context-run-id {
    margin-left: 0;
  }
  .expand-icon {
    position: absolute;
    right: 14px;
    top: 14px;
  }
  .context-msg {
    padding: 8px 10px;
  }
  .msg-content {
    font-size: 11px;
    max-height: 150px;
  }
  .messages-scroll {
    padding: 6px;
  }
}

/* ── Context Window Tab ── */
.context-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.context-card {
  background: var(--color-bg-card, #0c0e15);
  border: 1px solid var(--color-border, #1a1d2e);
  border-radius: 10px;
  overflow: hidden;
  transition: border-color 0.2s;
}
.context-card:hover {
  border-color: var(--color-border-active, #2a3556);
}
.context-card-expanded {
  border-color: var(--color-accent-blue, #3b82f6);
}
.context-header {
  padding: 14px 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  cursor: pointer;
  gap: 12px;
  position: relative;
}
.context-header:hover {
  background: rgba(255,255,255,0.02);
}
.context-header-left {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.context-trigger {
  font-weight: 600;
  font-size: 13px;
  color: var(--color-text-primary, #f0f2f5);
  white-space: nowrap;
}
.context-time {
  font-size: 12px;
  color: var(--color-text-muted, #8b8fa3);
  white-space: nowrap;
}
.context-stats {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-shrink: 0;
}
.context-stat {
  font-size: 12px;
  color: var(--color-text-secondary, #c4c8d4);
  display: flex;
  align-items: center;
  gap: 3px;
}
.stat-icon {
  font-size: 11px;
}
.context-run-id {
  font-family: monospace;
  font-size: 11px;
  color: var(--color-text-subtle, #555872);
  background: rgba(255,255,255,0.04);
  padding: 2px 6px;
  border-radius: 4px;
}
.expand-icon {
  font-size: 10px;
  color: var(--color-text-muted, #8b8fa3);
  margin-left: 4px;
}

/* Messages area */
.context-messages {
  border-top: 1px solid var(--color-border, #1a1d2e);
}
.messages-scroll {
  max-height: 500px;
  overflow-y: auto;
  padding: 8px;
}
.context-msg {
  padding: 10px 14px;
  border-radius: 8px;
  margin-bottom: 6px;
  border-left: 3px solid transparent;
}
.context-msg.role-user {
  background: rgba(30, 58, 95, 0.3);
  border-left-color: #3b82f6;
}
.context-msg.role-assistant {
  background: rgba(13, 26, 18, 0.3);
  border-left-color: #10b981;
}
.context-msg.role-system {
  background: rgba(99, 102, 241, 0.1);
  border-left-color: #6366f1;
}
.msg-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.msg-role-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
}
.msg-role-badge.role-user {
  background: rgba(59, 130, 246, 0.2);
  color: #60a5fa;
}
.msg-role-badge.role-assistant {
  background: rgba(16, 185, 129, 0.2);
  color: #34d399;
}
.msg-role-badge.role-system {
  background: rgba(99, 102, 241, 0.2);
  color: #818cf8;
}
.msg-tool-badge {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(245, 158, 11, 0.15);
  color: #fbbf24;
}
.msg-tool-badge.result {
  background: rgba(16, 185, 129, 0.15);
  color: #34d399;
}
.msg-index {
  font-size: 10px;
  color: var(--color-text-subtle, #555872);
  margin-left: auto;
}
.msg-content {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  color: var(--color-text-secondary, #c4c8d4);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.5;
  margin: 0;
  max-height: 200px;
  overflow-y: auto;
  cursor: pointer;
  transition: max-height 0.3s ease;
}
.msg-content-full {
  max-height: none;
}
.context-msg {
  cursor: pointer;
}
.context-msg:hover {
  background: rgba(255, 255, 255, 0.02);
}
.msg-expanded {
  border-left: 2px solid var(--color-accent, #3b82f6);
}

/* ── Mobile Responsive ── */

</style>
