import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    redirect: '/agents',
  },
  {
    path: '/landing',
    name: 'JarvisLanding',
    component: () => import('./views/LandingView.vue'),
    meta: {
      title: 'Jarvis AI Assistant',
      layout: 'public',
      public: true,
      canonical: 'https://jarvis.omnigentx.com/landing',
      description: 'Jarvis is a self-hostable AI assistant that coordinates an expert agent team with MCP tools, voice interaction, and a production-ready dashboard.',
    },
  },
  {
    path: '/agents',
    name: 'AgentsList',
    component: () => import('./views/AgentsList.vue'),
    meta: { title: 'Agents' },
  },
  {
    path: '/agents/:name',
    name: 'AgentDetail',
    component: () => import('./views/AgentDetail.vue'),
    meta: { title: 'Agent Detail', back: '/agents' },
  },
  {
    path: '/skills',
    name: 'SkillsLibrary',
    component: () => import('./views/SkillsLibraryView.vue'),
    meta: { title: 'Skills' },
  },
  {
    path: '/mcp-servers',
    name: 'McpServers',
    component: () => import('./views/McpServersView.vue'),
    meta: { title: 'MCP Servers' },
  },
  {
    path: '/monitor',
    name: 'TeamMonitor',
    component: () => import('./views/TeamMonitor.vue'),
    meta: { title: 'Team Monitor' },
  },
  {
    path: '/chat',
    name: 'Chat',
    component: () => import('./views/ChatView.vue'),
    meta: { title: 'Chat' },
  },
  {
    path: '/runs',
    name: 'Runs',
    component: () => import('./views/ComingSoon.vue'),
    meta: { title: 'Runs', comingSoon: true },
  },
  {
    path: '/scheduler',
    name: 'Scheduler',
    component: () => import('./views/SchedulerDashboard.vue'),
    meta: { title: 'Scheduler' },
  },
  {
    path: '/token-usage',
    name: 'TokenUsage',
    component: () => import('./views/TokenUsage.vue'),
    meta: { title: 'Token Usage' },
  },
  {
    path: '/notifications',
    name: 'Notifications',
    component: () => import('./views/NotificationList.vue'),
    meta: { title: 'Notifications' },
  },
  {
    path: '/notifications/:id',
    name: 'NotificationDetail',
    component: () => import('./views/NotificationDetail.vue'),
    meta: { title: 'Notification Detail', back: '/notifications' },
  },
  {
    path: '/approvals',
    name: 'Approvals',
    component: () => import('./views/ApprovalsView.vue'),
    meta: { title: 'Approvals' },
  },
  {
    path: '/stories',
    name: 'Stories',
    component: () => import('./views/StoriesView.vue'),
    meta: { title: 'Stories' },
  },
  {
    path: '/stories/:storyId',
    name: 'StoryDetail',
    component: () => import('./views/StoriesView.vue'),
    meta: { title: 'Story Detail', back: '/stories' },
    props: true,
  },
  {
    path: '/stories/:storyId/read/:filename',
    name: 'StoryReader',
    component: () => import('./views/StoryReaderView.vue'),
    meta: { title: 'Reader', back: true },
    props: true,
  },
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('./views/SettingsView.vue'),
    meta: { title: 'Settings' },
  },
  {
    path: '/oauth/callback',
    name: 'OAuthCallback',
    component: () => import('./views/OAuthCallback.vue'),
    meta: { title: 'OAuth Callback', layout: 'bare' },
  },
  {
    path: '/setup',
    component: () => import('./views/setup/SetupWizard.vue'),
    meta: { title: 'Setup', layout: 'bare' },
    children: [
      { path: '', name: 'SetupRoot', redirect: { name: 'SetupAuth' } },
      {
        path: 'auth',
        name: 'SetupAuth',
        component: () => import('./views/setup/StepAuth.vue'),
        meta: { title: 'Setup — API Key', layout: 'bare' },
      },
      {
        path: 'llm',
        name: 'SetupLLM',
        component: () => import('./views/setup/StepLLM.vue'),
        meta: { title: 'Setup — LLM', layout: 'bare' },
      },
      {
        path: 'services',
        name: 'SetupServices',
        component: () => import('./views/setup/StepServices.vue'),
        meta: { title: 'Setup — Services', layout: 'bare' },
      },
      {
        path: 'yaml',
        name: 'SetupYaml',
        component: () => import('./views/setup/StepYaml.vue'),
        meta: { title: 'Setup — YAML', layout: 'bare' },
      },
      {
        path: 'verify',
        name: 'SetupVerify',
        component: () => import('./views/setup/StepVerify.vue'),
        meta: { title: 'Setup — Verify', layout: 'bare' },
      },
    ],
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

function upsertMeta(name, content) {
  if (!content) return
  let tag = document.querySelector(`meta[name="${name}"]`)
  if (!tag) {
    tag = document.createElement('meta')
    tag.setAttribute('name', name)
    document.head.appendChild(tag)
  }
  tag.setAttribute('content', content)
}

function upsertCanonical(href) {
  let tag = document.querySelector('link[rel="canonical"]')
  if (!href) {
    tag?.remove()
    return
  }
  if (!tag) {
    tag = document.createElement('link')
    tag.setAttribute('rel', 'canonical')
    document.head.appendChild(tag)
  }
  tag.setAttribute('href', href)
}

// Update document metadata for both dashboard and public landing routes.
router.afterEach((to) => {
  document.title = `${to.meta.title || 'Dashboard'} — My Jarvis`
  upsertMeta('description', to.meta.description)
  upsertCanonical(to.meta.canonical)
})

export default router
