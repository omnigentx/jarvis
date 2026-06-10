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
router.isReady().then(() => app.mount('#app'))
