export const landingConfig = {
  launchUrl: '/agents',
  githubUrl: 'https://github.com/omnigentx/jarvis',
  selfHostUrl: 'https://github.com/omnigentx/jarvis#quick-start',
  demoUrl: '#demo',
}

export const hero = {
  eyebrow: 'Self-hostable AI operations workspace',
  title: 'Jarvis — a self-hostable AI assistant that coordinates an expert agent team.',
  subtitle:
    'Plan, research, design, code, test, and deploy with a multi-agent workspace powered by MCP tools, voice interaction, and a production-ready web dashboard.',
}

export const capabilities = [
  {
    title: 'Multi-agent execution',
    text: 'Coordinate PM, BA, SA, Dev, Designer, QE, and DSO agents through structured messages, meetings, and task handoffs.',
  },
  {
    title: 'MCP tool ecosystem',
    text: 'Connect Jarvis to GitHub, Atlassian, Figma, filesystem workflows, web research, Google apps, IoT, TTS, and more.',
  },
  {
    title: 'Hands-free voice',
    text: 'Use speech recognition, optional wake word, streaming TTS, WebSocket audio, and barge-in flows for natural interaction.',
  },
  {
    title: 'Web dashboard',
    text: 'Chat, configure providers, manage voice engines, inspect timelines, review approvals, and manage settings in one UI.',
  },
  {
    title: 'Self-hostable stack',
    text: 'Run Jarvis with Docker Compose and documented deployment guidance while retaining control over data and configuration.',
  },
  {
    title: 'Open architecture',
    text: 'Built on FastAPI, Vue/Vite, fast-agent, MCP servers, and documented security practices for transparent operations.',
  },
]

export const useCases = [
  'Turn a product idea into a PRD, architecture plan, implementation stories, QA checklist, and release plan.',
  'Research a repository and convert findings into implementation-ready engineering tasks.',
  'Coordinate design, development, QA, and release work across an expert AI team.',
  'Use voice to interact with Jarvis while your hands are busy.',
  'Connect Jarvis to external tools to automate real operational workflows.',
]

export const faqs = [
  {
    question: 'Is Jarvis just another chatbot?',
    answer: 'No. Jarvis is designed as a multi-agent workspace that can coordinate specialized roles and real tools through MCP.',
  },
  {
    question: 'Can I self-host it?',
    answer: 'Yes. The upstream project includes Docker Compose and deployment guidance for self-hosted operation.',
  },
  {
    question: 'Why launch at /landing first?',
    answer: 'Phase 1 preserves the current app root while providing a production-ready public landing page. Root promotion is planned as a separate, validated Phase 2.',
  },
]
