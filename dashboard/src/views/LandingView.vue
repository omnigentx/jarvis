<script setup>
import { capabilities, faqs, hero, landingConfig, useCases } from '../landing/content'

const navItems = [
  ['Capabilities', '#capabilities'],
  ['Use cases', '#use-cases'],
  ['Architecture', '#architecture'],
  ['Self-host', '#self-host'],
]
</script>

<template>
  <div class="landing-page">
    <header class="landing-nav" aria-label="Jarvis landing navigation">
      <a class="brand" href="/landing" aria-label="Jarvis landing home">
        <span class="brand-mark">J</span>
        <span>Jarvis</span>
      </a>
      <nav class="nav-links" aria-label="Landing sections">
        <a v-for="item in navItems" :key="item[0]" :href="item[1]">{{ item[0] }}</a>
        <a :href="landingConfig.githubUrl">GitHub</a>
      </nav>
      <a class="nav-cta" :href="landingConfig.launchUrl">Launch app</a>
    </header>

    <main>
      <section id="hero" class="hero-section" aria-labelledby="landing-title">
        <div class="hero-copy">
          <p class="eyebrow">{{ hero.eyebrow }}</p>
          <h1 id="landing-title">{{ hero.title }}</h1>
          <p class="hero-subtitle">{{ hero.subtitle }}</p>
          <div class="cta-row" aria-label="Primary landing actions">
            <a class="btn btn-primary" :href="landingConfig.launchUrl">Launch Jarvis</a>
            <a class="btn btn-secondary" :href="landingConfig.githubUrl">View GitHub</a>
          </div>
        </div>
        <aside class="product-card" aria-label="Jarvis product preview">
          <div class="card-toolbar">
            <span />
            <span />
            <span />
          </div>
          <div class="agent-row active"><strong>PM Agent</strong><span>Planning initiative</span></div>
          <div class="agent-row"><strong>Dev Agent</strong><span>Preparing implementation</span></div>
          <div class="agent-row"><strong>QE Agent</strong><span>Checking release gates</span></div>
          <div class="tool-strip">MCP • GitHub • Jira • Figma • Voice</div>
        </aside>
      </section>

      <section id="capabilities" class="section" aria-labelledby="capabilities-title">
        <p class="section-kicker">Capabilities</p>
        <h2 id="capabilities-title">An AI team connected to real tools</h2>
        <div class="feature-grid">
          <article v-for="capability in capabilities" :key="capability.title" class="feature-card">
            <h3>{{ capability.title }}</h3>
            <p>{{ capability.text }}</p>
          </article>
        </div>
      </section>

      <section id="how-it-works" class="section split-section" aria-labelledby="workflow-title">
        <div>
          <p class="section-kicker">How it works</p>
          <h2 id="workflow-title">From prompt to coordinated delivery</h2>
        </div>
        <ol class="steps-list">
          <li><strong>Ask</strong><span>Start with a goal or workflow.</span></li>
          <li><strong>Delegate</strong><span>Jarvis routes work to specialist agents.</span></li>
          <li><strong>Act</strong><span>Agents use MCP tools and shared context.</span></li>
          <li><strong>Review</strong><span>Track outputs, approvals, and release readiness.</span></li>
        </ol>
      </section>

      <section id="use-cases" class="section" aria-labelledby="use-cases-title">
        <p class="section-kicker">Use cases</p>
        <h2 id="use-cases-title">Built for end-to-end operational work</h2>
        <div class="use-case-list">
          <article v-for="item in useCases" :key="item" class="use-case-card">{{ item }}</article>
        </div>
      </section>

      <section id="architecture" class="section split-section" aria-labelledby="architecture-title">
        <div>
          <p class="section-kicker">Architecture</p>
          <h2 id="architecture-title">Transparent, self-hostable, and MCP-first</h2>
          <p class="section-copy">Jarvis combines a FastAPI backend, fast-agent runtime, MCP servers, Vue/Vite dashboard, and Docker-based deployment guidance.</p>
        </div>
        <div class="architecture-stack" role="img" aria-label="Jarvis architecture: Vue dashboard, FastAPI backend, fast-agent runtime, and MCP tools">
          <span>Vue/Vite dashboard</span>
          <span>FastAPI backend</span>
          <span>fast-agent runtime</span>
          <span>MCP tool servers</span>
        </div>
      </section>

      <section id="demo" class="section media-section" aria-labelledby="demo-title">
        <p class="section-kicker">Demo and media</p>
        <h2 id="demo-title">Show the dashboard without blocking first paint</h2>
        <p>Use screenshots and short demo video slots after redaction review. Media should be optimized and lazy-loaded for production.</p>
      </section>

      <section id="self-host" class="section split-section" aria-labelledby="self-host-title">
        <div>
          <p class="section-kicker">Self-host</p>
          <h2 id="self-host-title">Run Jarvis under your control</h2>
          <p class="section-copy">Use the open-source repository and Docker-based setup guidance to evaluate Jarvis in a self-hosted environment.</p>
        </div>
        <a class="btn btn-secondary" :href="landingConfig.selfHostUrl">Read self-host guide</a>
      </section>

      <section id="faq" class="section" aria-labelledby="faq-title">
        <p class="section-kicker">FAQ</p>
        <h2 id="faq-title">Questions before you launch</h2>
        <div class="faq-list">
          <article v-for="faq in faqs" :key="faq.question" class="faq-item">
            <h3>{{ faq.question }}</h3>
            <p>{{ faq.answer }}</p>
          </article>
        </div>
      </section>

      <section class="final-cta" aria-labelledby="final-cta-title">
        <h2 id="final-cta-title">Build with an AI team, not just an AI chat box.</h2>
        <p>Try Jarvis, explore the source, or prepare your self-hosted setup.</p>
        <div class="cta-row center">
          <a class="btn btn-primary" :href="landingConfig.launchUrl">Launch Jarvis</a>
          <a class="btn btn-secondary" :href="landingConfig.githubUrl">View GitHub</a>
        </div>
      </section>
    </main>

    <footer class="landing-footer">
      <span>Jarvis by OmnigentX</span>
      <span>Phase 1 canonical: /landing</span>
    </footer>
  </div>
</template>

<style scoped>
.landing-page {
  min-height: 100vh;
  overflow-x: hidden;
  background:
    radial-gradient(circle at top left, rgba(124, 58, 237, 0.22), transparent 32rem),
    radial-gradient(circle at 80% 10%, rgba(6, 182, 212, 0.14), transparent 28rem),
    var(--bg-base);
  color: var(--text-heading);
}

.landing-nav,
.section,
.hero-section,
.final-cta,
.landing-footer {
  width: min(1248px, calc(100% - 48px));
  margin: 0 auto;
}

.landing-nav {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 24px 0;
}

.brand,
.nav-links,
.cta-row,
.card-toolbar,
.steps-list li,
.landing-footer {
  display: flex;
  align-items: center;
}

.brand { gap: 10px; font-weight: 800; }
.brand-mark {
  display: inline-grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border-radius: 12px;
  background: linear-gradient(135deg, #7c3aed, #06b6d4);
}

.nav-links { gap: 22px; color: var(--text-body); font-size: 14px; }
.nav-links a:hover, .nav-cta:hover { color: var(--text-heading); }
.nav-cta { color: #dbeafe; font-weight: 700; }

.hero-section {
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(320px, 0.95fr);
  gap: 56px;
  align-items: center;
  padding: 84px 0 72px;
}

.eyebrow,
.section-kicker {
  color: #67e8f9;
  font-size: 13px;
  font-weight: 800;
  letter-spacing: 0.12em;
  margin-bottom: 16px;
  text-transform: uppercase;
}

h1 { font-size: clamp(44px, 6vw, 78px); line-height: 0.98; letter-spacing: -0.06em; margin-bottom: 24px; }
h2 { font-size: clamp(32px, 4vw, 52px); line-height: 1.05; letter-spacing: -0.04em; margin-bottom: 18px; }
h3 { font-size: 19px; margin-bottom: 10px; }

.hero-subtitle,
.section-copy,
.media-section p,
.faq-item p,
.feature-card p {
  color: var(--text-body);
  font-size: 17px;
}

.hero-subtitle { max-width: 700px; font-size: 20px; }
.cta-row { gap: 14px; margin-top: 34px; flex-wrap: wrap; }
.cta-row.center { justify-content: center; }

.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 52px;
  padding: 0 22px;
  border: 1px solid transparent;
  border-radius: 14px;
  font-weight: 800;
}
.btn:focus-visible { outline: 3px solid #67e8f9; outline-offset: 3px; }
.btn-primary { background: linear-gradient(135deg, #7c3aed, #06b6d4); color: white; }
.btn-secondary { background: rgba(17, 19, 24, 0.86); border-color: var(--border); color: var(--text-heading); }

.product-card,
.feature-card,
.use-case-card,
.architecture-stack,
.media-section,
.faq-item,
.final-cta {
  border: 1px solid rgba(148, 163, 184, 0.16);
  background: rgba(17, 19, 24, 0.74);
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.28);
  backdrop-filter: blur(16px);
}
.product-card { border-radius: 28px; padding: 22px; }
.card-toolbar { gap: 8px; margin-bottom: 22px; }
.card-toolbar span { width: 10px; height: 10px; border-radius: 99px; background: #475569; }
.agent-row { display: flex; justify-content: space-between; gap: 16px; padding: 18px; border-radius: 18px; background: rgba(15, 23, 42, 0.62); margin-bottom: 12px; }
.agent-row span { color: var(--text-body); }
.agent-row.active { border: 1px solid rgba(103, 232, 249, 0.32); }
.tool-strip { color: #a7f3d0; margin-top: 22px; font-weight: 700; }

.section { padding: 82px 0; }
.feature-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; margin-top: 34px; }
.feature-card, .use-case-card, .faq-item { border-radius: 22px; padding: 24px; }

.split-section { display: grid; grid-template-columns: 0.8fr 1fr; gap: 36px; align-items: start; }
.steps-list { list-style: none; flex-direction: column; gap: 14px; }
.steps-list li { width: 100%; gap: 16px; padding: 18px; border-radius: 18px; background: rgba(30, 34, 51, 0.56); }
.steps-list span { color: var(--text-body); }

.use-case-list { display: grid; grid-template-columns: repeat(5, minmax(180px, 1fr)); gap: 14px; margin-top: 34px; }
.architecture-stack { display: grid; gap: 12px; padding: 24px; border-radius: 24px; }
.architecture-stack span { padding: 14px 16px; border-radius: 14px; background: rgba(59, 130, 246, 0.12); color: #dbeafe; }
.media-section { border-radius: 26px; padding: 36px; }
.faq-list { display: grid; gap: 14px; margin-top: 28px; }
.final-cta { text-align: center; border-radius: 32px; padding: 56px 28px; margin-top: 72px; margin-bottom: 56px; }
.final-cta p { color: var(--text-body); }
.landing-footer { justify-content: space-between; gap: 16px; padding: 28px 0 44px; color: var(--text-body); font-size: 14px; }

@media (max-width: 900px) {
  .landing-nav { align-items: flex-start; flex-direction: column; }
  .nav-links { flex-wrap: wrap; }
  .hero-section, .split-section { grid-template-columns: 1fr; padding-top: 48px; }
  .feature-grid { grid-template-columns: 1fr 1fr; }
  .use-case-list { grid-template-columns: 1fr; }
}

@media (max-width: 640px) {
  .landing-nav,
  .section,
  .hero-section,
  .final-cta,
  .landing-footer { width: min(100% - 28px, 1248px); }
  h1 { font-size: 40px; }
  .hero-subtitle { font-size: 17px; }
  .feature-grid { grid-template-columns: 1fr; }
  .landing-footer { align-items: flex-start; flex-direction: column; }
}
</style>
