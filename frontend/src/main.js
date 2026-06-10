import { createApp } from 'vue'
import { createPinia } from 'pinia'
import router from './router'
import App from './App.vue'
import './style.css'
import './assets/tokens.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
// Wait for the initial route: App.vue's onMounted branches on
// route.meta.layout (bare /setup pages skip the boot auth probe, and the
// template picks bare vs AppLayout chrome). Mounting before the router
// resolves makes meta read as {} for one tick — the probe then runs on
// /setup, flips the auth store to 'unauthenticated' mid-wizard, and the
// AppLayout chrome (with its SSE streams) flashes over the wizard.
// isReady() rejects if a navigation guard throws — degrade to mounting
// anyway (router lands on the resolved-or-error state) instead of a
// blank page with no surfaced error.
router.isReady()
  .catch((err) => console.error('[main] initial route failed:', err))
  .then(() => app.mount('#app'))
