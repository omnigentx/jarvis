<script setup>
/**
 * Settings → Services.
 *
 * Stacks service-specific cards (not generic toggles): Google (handled by
 * the shared GoogleOAuthCard which encapsulates client-credentials,
 * desktop/web flows, and the API enable checklist), Project Repo (single
 * URL), Roborock (IoT creds), and GitHub (dev agent git access).
 *
 * Each card has its own status badge that drives the visual state
 * (connected / configured / not-connected) per the design.
 */
import { ref, computed, onMounted } from 'vue'
import { apiFetch } from '../../api'
import { useConfirm } from '../../composables/useConfirm'
import { useLang } from '../../composables/useLang'
import GoogleOAuthCard from '../../components/GoogleOAuthCard.vue'

const { confirm } = useConfirm()
const { t } = useLang()

// ─── Project Repository (jarvis_repo) ───────────────────────────────────
const JARVIS_REPO_CATEGORY = 'service.jarvis_repo'
const JARVIS_REPO_URL_KEY = 'JARVIS_REPO_URL'

const jarvisRepoStatus = ref({
  url_set: false,
  current_url: null,
})
const jarvisRepoLoading = ref(true)
const jarvisRepoStatusError = ref('')
const jarvisRepoEditing = ref(false)
const jarvisRepoUrl = ref('')
const jarvisRepoSaving = ref(false)
const jarvisRepoSaved = ref(false)
const jarvisRepoError = ref('')

const jarvisRepoConfigured = computed(() => jarvisRepoStatus.value.url_set)

async function fetchJarvisRepoStatus() {
  jarvisRepoLoading.value = true
  jarvisRepoStatusError.value = ''
  try {
    const resp = await apiFetch(`/api/settings/${JARVIS_REPO_CATEGORY}`)
    const items = resp?.items || []
    const row = items.find((i) => i.key === JARVIS_REPO_URL_KEY)
    const plain =
      row && !row.is_secret && typeof row.value === 'string' && row.value.length > 0
        ? row.value
        : null
    jarvisRepoStatus.value = {
      url_set: !!(row && row.has_value),
      current_url: plain,
    }
  } catch (err) {
    jarvisRepoStatusError.value = err?.body?.detail || err?.message || String(err)
  } finally {
    jarvisRepoLoading.value = false
  }
}

async function saveJarvisRepo() {
  const url = jarvisRepoUrl.value.trim()
  if (!url) {
    jarvisRepoError.value = t('settings.services.repo.errEmpty')
    return
  }
  jarvisRepoSaving.value = true
  jarvisRepoError.value = ''
  jarvisRepoSaved.value = false
  try {
    await apiFetch('/api/settings/bulk', {
      method: 'POST',
      body: JSON.stringify({
        items: [
          {
            category: JARVIS_REPO_CATEGORY,
            key: JARVIS_REPO_URL_KEY,
            value: url,
            is_secret: false,
          },
        ],
      }),
    })
    jarvisRepoSaved.value = true
    jarvisRepoUrl.value = ''
    jarvisRepoEditing.value = false
    await fetchJarvisRepoStatus()
  } catch (err) {
    jarvisRepoError.value = err?.body?.detail || err?.message || String(err)
  } finally {
    jarvisRepoSaving.value = false
  }
}

async function removeJarvisRepo() {
  if (
    !(await confirm({
      title: t('settings.services.repo.clearTitle'),
      message: t('settings.services.repo.clearMsg'),
      confirmText: t('settings.services.repo.clearConfirm'),
      variant: 'danger',
    }))
  ) {
    return
  }
  try {
    await apiFetch('/api/settings/bulk', {
      method: 'POST',
      body: JSON.stringify({
        items: [
          {
            category: JARVIS_REPO_CATEGORY,
            key: JARVIS_REPO_URL_KEY,
            value: null,
            is_secret: false,
          },
        ],
      }),
    })
    jarvisRepoSaved.value = false
    jarvisRepoEditing.value = false
    await fetchJarvisRepoStatus()
  } catch (err) {
    jarvisRepoError.value = err?.body?.detail || err?.message || String(err)
  }
}

function cancelJarvisRepoEdit() {
  jarvisRepoEditing.value = false
  jarvisRepoUrl.value = ''
  jarvisRepoError.value = ''
}

// ─── Roborock (Vacuum) ───────────────────────────────────────────────────
const ROBOROCK_CATEGORY = 'service.roborock'
const ROBOROCK_USERNAME_KEY = 'ROBOROCK_USERNAME'
const ROBOROCK_PASSWORD_KEY = 'ROBOROCK_PASSWORD'

const roborockStatus = ref({
  username_set: false,
  password_set: false,
})
const roborockLoading = ref(true)
const roborockStatusError = ref('')
const roborockEditing = ref(false)
const roborockUsername = ref('')
const roborockPassword = ref('')
const roborockSaving = ref(false)
const roborockSaved = ref(false)
const roborockError = ref('')

const roborockConnected = computed(
  () => roborockStatus.value.username_set && roborockStatus.value.password_set,
)

async function fetchRoborockStatus() {
  roborockLoading.value = true
  roborockStatusError.value = ''
  try {
    const resp = await apiFetch(`/api/settings/${ROBOROCK_CATEGORY}`)
    const items = resp?.items || []
    const hasKey = (k) => items.some((i) => i.key === k && i.has_value)
    roborockStatus.value = {
      username_set: hasKey(ROBOROCK_USERNAME_KEY),
      password_set: hasKey(ROBOROCK_PASSWORD_KEY),
    }
  } catch (err) {
    roborockStatusError.value = err?.body?.detail || err?.message || String(err)
  } finally {
    roborockLoading.value = false
  }
}

async function saveRoborock() {
  const username = roborockUsername.value.trim()
  const password = roborockPassword.value.trim()
  if (!username || !password) {
    roborockError.value = t('settings.services.roborock.errRequired')
    return
  }
  roborockSaving.value = true
  roborockError.value = ''
  roborockSaved.value = false
  try {
    await apiFetch('/api/settings/bulk', {
      method: 'POST',
      body: JSON.stringify({
        items: [
          { category: ROBOROCK_CATEGORY, key: ROBOROCK_USERNAME_KEY, value: username, is_secret: true },
          { category: ROBOROCK_CATEGORY, key: ROBOROCK_PASSWORD_KEY, value: password, is_secret: true },
        ],
      }),
    })
    roborockSaved.value = true
    roborockUsername.value = ''
    roborockPassword.value = ''
    roborockEditing.value = false
    await fetchRoborockStatus()
  } catch (err) {
    roborockError.value = err?.body?.detail || err?.message || String(err)
  } finally {
    roborockSaving.value = false
  }
}

async function disconnectRoborock() {
  if (
    !(await confirm({
      title: t('settings.services.roborock.disconnectTitle'),
      message: t('settings.services.roborock.disconnectMsg'),
      confirmText: t('settings.services.disconnect'),
      variant: 'danger',
    }))
  ) {
    return
  }
  try {
    await apiFetch('/api/settings/bulk', {
      method: 'POST',
      body: JSON.stringify({
        items: [
          { category: ROBOROCK_CATEGORY, key: ROBOROCK_USERNAME_KEY, value: null, is_secret: true },
          { category: ROBOROCK_CATEGORY, key: ROBOROCK_PASSWORD_KEY, value: null, is_secret: true },
        ],
      }),
    })
    roborockSaved.value = false
    roborockEditing.value = false
    await fetchRoborockStatus()
  } catch (err) {
    roborockError.value = err?.body?.detail || err?.message || String(err)
  }
}

function cancelRoborockEdit() {
  roborockEditing.value = false
  roborockUsername.value = ''
  roborockPassword.value = ''
  roborockError.value = ''
}

// ─── GitHub (dev agent git access) ──────────────────────────────────────
const GITHUB_CATEGORY = 'service.github'
const GITHUB_TOKEN_KEY = 'personal_access_token'
const GITHUB_USER_NAME_KEY = 'user_name'
const GITHUB_USER_EMAIL_KEY = 'user_email'

const githubStatus = ref({
  token_set: false,
  user_name_set: false,
  user_email_set: false,
})
const githubLoading = ref(true)
const githubStatusError = ref('')
const githubEditing = ref(false)
const githubToken = ref('')
const githubUserName = ref('')
const githubUserEmail = ref('')
const githubSaving = ref(false)
const githubSaved = ref(false)
const githubError = ref('')

const githubConnected = computed(
  () =>
    githubStatus.value.token_set &&
    githubStatus.value.user_name_set &&
    githubStatus.value.user_email_set,
)

async function fetchGithubStatus() {
  githubLoading.value = true
  githubStatusError.value = ''
  try {
    const resp = await apiFetch(`/api/settings/${GITHUB_CATEGORY}`)
    const items = resp?.items || []
    const hasKey = (k) => items.some((i) => i.key === k && i.has_value)
    githubStatus.value = {
      token_set: hasKey(GITHUB_TOKEN_KEY),
      user_name_set: hasKey(GITHUB_USER_NAME_KEY),
      user_email_set: hasKey(GITHUB_USER_EMAIL_KEY),
    }
  } catch (err) {
    githubStatusError.value = err?.body?.detail || err?.message || String(err)
  } finally {
    githubLoading.value = false
  }
}

async function saveGithub() {
  const token = githubToken.value.trim()
  const name = githubUserName.value.trim()
  const email = githubUserEmail.value.trim()
  if (!token || !name || !email) {
    githubError.value = t('settings.services.github.errRequired')
    return
  }
  githubSaving.value = true
  githubError.value = ''
  githubSaved.value = false
  try {
    await apiFetch('/api/settings/bulk', {
      method: 'POST',
      body: JSON.stringify({
        items: [
          { category: GITHUB_CATEGORY, key: GITHUB_TOKEN_KEY, value: token, is_secret: true },
          { category: GITHUB_CATEGORY, key: GITHUB_USER_NAME_KEY, value: name, is_secret: false },
          { category: GITHUB_CATEGORY, key: GITHUB_USER_EMAIL_KEY, value: email, is_secret: false },
        ],
      }),
    })
    githubSaved.value = true
    githubToken.value = ''
    githubUserName.value = ''
    githubUserEmail.value = ''
    githubEditing.value = false
    await fetchGithubStatus()
  } catch (err) {
    githubError.value = err?.body?.detail || err?.message || String(err)
  } finally {
    githubSaving.value = false
  }
}

async function disconnectGithub() {
  if (
    !(await confirm({
      title: t('settings.services.github.disconnectTitle'),
      message: t('settings.services.github.disconnectMsg'),
      confirmText: t('settings.services.disconnect'),
      variant: 'danger',
    }))
  ) {
    return
  }
  try {
    await apiFetch('/api/settings/bulk', {
      method: 'POST',
      body: JSON.stringify({
        items: [
          { category: GITHUB_CATEGORY, key: GITHUB_TOKEN_KEY, value: null, is_secret: true },
          { category: GITHUB_CATEGORY, key: GITHUB_USER_NAME_KEY, value: null, is_secret: false },
          { category: GITHUB_CATEGORY, key: GITHUB_USER_EMAIL_KEY, value: null, is_secret: false },
        ],
      }),
    })
    githubSaved.value = false
    githubEditing.value = false
    await fetchGithubStatus()
  } catch (err) {
    githubError.value = err?.body?.detail || err?.message || String(err)
  }
}

function cancelGithubEdit() {
  githubEditing.value = false
  githubToken.value = ''
  githubUserName.value = ''
  githubUserEmail.value = ''
  githubError.value = ''
}

onMounted(() => {
  fetchJarvisRepoStatus()
  fetchRoborockStatus()
  fetchGithubStatus()
})
</script>

<template>
  <div class="svc-sections">
    <!-- Google OAuth — wraps the shared card in a service-card shell -->
    <section class="service-card panel-card">
      <div class="hud-corner" />
      <header class="card-header">
        <div class="icon-circle google">
          <svg width="18" height="18" viewBox="0 0 48 48">
            <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3c-1.6 4.6-6 8-11.3 8c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3l5.7-5.7C34.5 6.1 29.5 4 24 4C13 4 4 13 4 24s9 20 20 20s20-9 20-20c0-1.3-.1-2.3-.4-3.5"/>
            <path fill="#FF3D00" d="m6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3l5.7-5.7C34.5 6.1 29.5 4 24 4C16.3 4 9.6 8.4 6.3 14.7"/>
            <path fill="#4CAF50" d="M24 44c5.4 0 10.3-2.1 14-5.5l-6.5-5.3c-2 1.5-4.6 2.5-7.5 2.5c-5.3 0-9.7-3.4-11.3-8l-6.6 5.1C9.5 39.6 16.2 44 24 44"/>
            <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.3-2.3 4.3-4.3 5.7l6.5 5.3C41.4 35.5 44 30.1 44 24c0-1.3-.1-2.3-.4-3.5"/>
          </svg>
        </div>
        <div class="card-title-block">
          <h2>{{ t('settings.services.google.title') }}</h2>
          <p>{{ t('settings.services.google.desc') }}</p>
        </div>
      </header>
      <GoogleOAuthCard />
    </section>

    <!-- Project Repository -->
    <section class="service-card panel-card">
      <div class="hud-corner" />
      <header class="card-header">
        <div class="icon-circle">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M3 7v10a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-7l-2-2H5a2 2 0 0 0-2 2z" />
            <circle cx="12" cy="14" r="2" />
            <path d="M12 12V9" />
          </svg>
        </div>
        <div class="card-title-block">
          <h2>{{ t('settings.services.repo.title') }}</h2>
          <p>{{ t('settings.services.repo.desc') }}</p>
        </div>
        <span
          class="status-chip"
          :class="{ on: jarvisRepoConfigured }"
        >
          {{ jarvisRepoConfigured ? t('settings.services.statusConfigured') : t('settings.services.statusNotConfigured') }}
        </span>
      </header>

      <div v-if="jarvisRepoLoading" class="muted">{{ t('common.loading') }}</div>
      <div v-else-if="jarvisRepoStatusError" class="error-msg">{{ jarvisRepoStatusError }}</div>

      <!-- Form mode -->
      <div
        v-if="!jarvisRepoLoading && (!jarvisRepoConfigured || jarvisRepoEditing)"
        class="card-body"
      >
        <p class="muted">{{ t('settings.services.repo.plainTextNote') }}</p>
        <label class="field-label">JARVIS_REPO_URL</label>
        <input
          class="text-input mono"
          placeholder="https://github.com/&lt;user&gt;/jarvis.git"
          autocomplete="off"
          v-model="jarvisRepoUrl"
          @keydown.enter="saveJarvisRepo"
        />
        <div class="action-row">
          <button
            v-if="jarvisRepoEditing"
            type="button"
            class="btn"
            :disabled="jarvisRepoSaving"
            @click="cancelJarvisRepoEdit"
          >{{ t('common.cancel') }}</button>
          <button
            type="button"
            class="btn primary"
            :disabled="jarvisRepoSaving"
            @click="saveJarvisRepo"
          >{{ jarvisRepoSaving ? t('settings.common.saving') : t('settings.services.repo.saveUrl') }}</button>
        </div>
        <div v-if="jarvisRepoError" class="error-msg">{{ jarvisRepoError }}</div>
        <div v-if="jarvisRepoSaved" class="success-msg">{{ t('settings.services.repo.savedMsg') }}</div>
      </div>

      <!-- Configured display -->
      <div
        v-if="!jarvisRepoLoading && jarvisRepoConfigured && !jarvisRepoEditing"
        class="card-body row-flow"
      >
        <div class="repo-url">
          <div v-if="jarvisRepoStatus.current_url" class="repo-url-line">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" style="color: var(--text-muted)">
              <path d="M12 .3a12 12 0 0 0-3.8 23.4c.6.1.8-.3.8-.6v-2c-3.3.7-4-1.6-4-1.6-.5-1.4-1.3-1.8-1.3-1.8-1.1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1.1 1.8 2.8 1.3 3.5 1 .1-.8.4-1.3.8-1.6-2.7-.3-5.5-1.3-5.5-6a4.7 4.7 0 0 1 1.3-3.2c-.2-.4-.6-1.6.1-3.2 0 0 1-.3 3.3 1.2a11.5 11.5 0 0 1 6 0C17.3 4.7 18.3 5 18.3 5c.7 1.6.2 2.8.1 3.2.8.9 1.3 2 1.3 3.2 0 4.6-2.8 5.7-5.5 6 .4.4.8 1.1.8 2.3v3.4c0 .3.2.7.8.6A12 12 0 0 0 12 .3"/>
            </svg>
            <code>{{ jarvisRepoStatus.current_url }}</code>
          </div>
          <div v-else class="muted small">{{ t('settings.services.repo.encryptedNote') }}</div>
        </div>
        <div class="action-row inline">
          <button type="button" class="btn" @click="jarvisRepoEditing = true">{{ t('common.edit') }}</button>
          <button type="button" class="btn danger" @click="removeJarvisRepo">{{ t('settings.services.repo.clearConfirm') }}</button>
        </div>
      </div>
    </section>

    <!-- Roborock -->
    <section class="service-card panel-card">
      <div class="hud-corner" />
      <header class="card-header">
        <div class="icon-circle">
          <span style="font-size: 18px;">🤖</span>
        </div>
        <div class="card-title-block">
          <h2>{{ t('settings.services.roborock.title') }}</h2>
          <p>{{ t('settings.services.roborock.desc') }}</p>
        </div>
        <span
          class="status-chip"
          :class="{ on: roborockConnected }"
        >
          {{ roborockConnected ? t('settings.services.statusConnected') : t('settings.services.statusNotConfigured') }}
        </span>
      </header>

      <div v-if="roborockLoading" class="muted">{{ t('common.loading') }}</div>
      <div v-else-if="roborockStatusError" class="error-msg">{{ roborockStatusError }}</div>

      <div
        v-if="!roborockLoading && (!roborockConnected || roborockEditing)"
        class="card-body"
      >
        <p class="muted">{{ t('settings.services.roborock.note') }}</p>
        <label class="field-label">{{ t('settings.services.roborock.emailLabel') }}</label>
        <input
          class="text-input"
          placeholder="you@example.com"
          autocomplete="off"
          v-model="roborockUsername"
        />
        <label class="field-label">{{ t('settings.services.roborock.passwordLabel') }}</label>
        <input
          class="text-input"
          type="password"
          placeholder="••••••••"
          autocomplete="new-password"
          v-model="roborockPassword"
        />
        <div class="action-row">
          <button
            v-if="roborockEditing"
            type="button"
            class="btn"
            :disabled="roborockSaving"
            @click="cancelRoborockEdit"
          >{{ t('common.cancel') }}</button>
          <button
            type="button"
            class="btn primary"
            :disabled="roborockSaving"
            @click="saveRoborock"
          >{{ roborockSaving ? t('settings.common.saving') : t('settings.services.saveCredentials') }}</button>
        </div>
        <div v-if="roborockError" class="error-msg">{{ roborockError }}</div>
        <div v-if="roborockSaved" class="success-msg">{{ t('settings.services.roborock.savedMsg') }}</div>
      </div>

      <div
        v-if="!roborockLoading && roborockConnected && !roborockEditing"
        class="card-body row-flow"
      >
        <div class="meta-info">
          <div>{{ t('settings.services.roborock.onFile') }}</div>
          <div class="muted small">{{ t('settings.services.encryptedEcho') }}</div>
        </div>
        <div class="action-row inline">
          <button type="button" class="btn" @click="roborockEditing = true">{{ t('settings.services.update') }}</button>
          <button type="button" class="btn danger" @click="disconnectRoborock">{{ t('settings.services.disconnect') }}</button>
        </div>
      </div>
    </section>

    <!-- GitHub -->
    <section class="service-card panel-card">
      <div class="hud-corner" />
      <header class="card-header">
        <div class="icon-circle">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M12 .3a12 12 0 0 0-3.8 23.4c.6.1.8-.3.8-.6v-2c-3.3.7-4-1.6-4-1.6-.5-1.4-1.3-1.8-1.3-1.8-1.1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1.1 1.8 2.8 1.3 3.5 1 .1-.8.4-1.3.8-1.6-2.7-.3-5.5-1.3-5.5-6a4.7 4.7 0 0 1 1.3-3.2c-.2-.4-.6-1.6.1-3.2 0 0 1-.3 3.3 1.2a11.5 11.5 0 0 1 6 0C17.3 4.7 18.3 5 18.3 5c.7 1.6.2 2.8.1 3.2.8.9 1.3 2 1.3 3.2 0 4.6-2.8 5.7-5.5 6 .4.4.8 1.1.8 2.3v3.4c0 .3.2.7.8.6A12 12 0 0 0 12 .3"/>
          </svg>
        </div>
        <div class="card-title-block">
          <h2>{{ t('settings.services.github.title') }}</h2>
          <p>{{ t('settings.services.github.desc') }}</p>
        </div>
        <span
          class="status-chip"
          :class="{ on: githubConnected }"
        >
          {{ githubConnected ? t('settings.services.statusConfigured') : t('settings.services.statusNotConfigured') }}
        </span>
      </header>

      <div v-if="githubLoading" class="muted">{{ t('common.loading') }}</div>
      <div v-else-if="githubStatusError" class="error-msg">{{ githubStatusError }}</div>

      <div
        v-if="!githubLoading && (!githubConnected || githubEditing)"
        class="card-body"
      >
        <p class="muted">
          {{ t('settings.services.github.tokenHintPre') }}
          <a href="https://github.com/settings/tokens" target="_blank" rel="noopener">github.com/settings/tokens</a>
          {{ t('settings.services.github.tokenHintPost') }} <code>repo</code> {{ t('settings.services.github.tokenHintScope') }}
        </p>
        <label class="field-label">{{ t('settings.services.github.tokenLabel') }}</label>
        <input
          class="text-input mono"
          placeholder="ghp_…"
          type="password"
          autocomplete="new-password"
          v-model="githubToken"
        />
        <label class="field-label">{{ t('settings.services.github.userNameLabel') }}</label>
        <input
          class="text-input"
          placeholder="Jane Developer"
          autocomplete="off"
          v-model="githubUserName"
        />
        <label class="field-label">{{ t('settings.services.github.userEmailLabel') }}</label>
        <input
          class="text-input"
          placeholder="jane@example.com"
          autocomplete="off"
          v-model="githubUserEmail"
        />
        <div class="action-row">
          <button
            v-if="githubEditing"
            type="button"
            class="btn"
            :disabled="githubSaving"
            @click="cancelGithubEdit"
          >{{ t('common.cancel') }}</button>
          <button
            type="button"
            class="btn primary"
            :disabled="githubSaving"
            @click="saveGithub"
          >{{ githubSaving ? t('settings.common.saving') : t('settings.services.saveCredentials') }}</button>
        </div>
        <div v-if="githubError" class="error-msg">{{ githubError }}</div>
        <div v-if="githubSaved" class="success-msg">{{ t('settings.services.github.savedMsg') }}</div>
      </div>

      <div
        v-if="!githubLoading && githubConnected && !githubEditing"
        class="card-body row-flow"
      >
        <div class="meta-info">
          <div>{{ t('settings.services.github.onFile') }}</div>
          <div class="muted small">{{ t('settings.services.github.bindNote') }}</div>
        </div>
        <div class="action-row inline">
          <button type="button" class="btn" @click="githubEditing = true">{{ t('settings.services.update') }}</button>
          <button type="button" class="btn danger" @click="disconnectGithub">{{ t('settings.services.disconnect') }}</button>
        </div>
      </div>
    </section>

    <!-- Placeholder -->
    <section class="service-card muted-card">
      <p>
        <strong>{{ t('settings.services.more.title') }}</strong> {{ t('settings.services.more.desc') }}
      </p>
    </section>
  </div>
</template>

<style scoped>
.svc-sections { display: flex; flex-direction: column; gap: 14px; }

/* ── Service card (HUD style) ─────────────────────────────────────── */
.service-card {
  position: relative;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 18px 22px;
}
.hud-corner {
  position: absolute; right: 0; bottom: 0;
  width: 16px; height: 16px;
  border-right: 1.5px solid var(--accent);
  border-bottom: 1.5px solid var(--accent);
  opacity: 0.4;
  border-bottom-right-radius: var(--r-md);
  pointer-events: none;
}
.card-header {
  display: flex; align-items: flex-start; gap: 12px;
  margin-bottom: 14px;
}
.card-title-block { flex: 1; min-width: 0; }
.card-title-block h2 {
  font-size: 15px; font-weight: 600; color: var(--text);
  margin: 0 0 2px;
}
.card-title-block p {
  margin: 0; font-size: 12.5px; color: var(--text-dim); line-height: 1.5;
}
.icon-circle {
  flex-shrink: 0;
  width: 36px; height: 36px;
  border-radius: var(--r-sm);
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  color: var(--accent);
  display: grid; place-items: center;
}
.icon-circle.google { background: #fff; border-color: transparent; padding: 4px; }

.status-chip {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.08em;
  color: var(--text-muted);
  margin-left: 8px;
  white-space: nowrap;
  align-self: flex-start;
}
.status-chip.on { color: var(--success); }

/* ── Card body ────────────────────────────────────────────────────── */
.card-body {
  display: flex; flex-direction: column; gap: 10px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}
.card-body.row-flow {
  flex-direction: row; align-items: center; justify-content: space-between;
}
.repo-url { flex: 1; min-width: 0; }
.repo-url-line {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 8px 12px;
  background: var(--bg-4);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  max-width: 100%;
}
.repo-url-line code {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.meta-info { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--text); }

.field-label {
  display: block;
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-muted);
  margin-bottom: 2px;
}
.text-input {
  width: 100%;
  background: var(--bg-4);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  padding: 9px 12px;
  color: var(--text);
  font-family: inherit;
  font-size: 13px;
}
.text-input.mono { font-family: var(--font-mono); font-size: 12px; }
.text-input:focus {
  outline: none;
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-bg-strong);
}

.muted { color: var(--text-dim); font-size: 13px; line-height: 1.5; }
.muted.small { font-size: 11.5px; color: var(--text-muted); }
.muted a { color: var(--accent); }
.muted code {
  background: rgba(255, 255, 255, 0.05);
  padding: 1px 5px; border-radius: 3px;
  font-family: var(--font-mono); font-size: 11.5px;
  color: var(--accent);
}

.action-row {
  display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap;
  margin-top: 4px;
}
.action-row.inline { margin-top: 0; }
.btn {
  padding: 8px 14px;
  font-family: inherit;
  font-size: 13px;
  font-weight: 500;
  border-radius: var(--r-md);
  border: 1px solid var(--border-strong);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  transition: all 0.15s;
}
.btn:hover:not([disabled]) { color: var(--text); background: rgba(255, 255, 255, 0.04); }
.btn.primary {
  background: var(--primary); color: #ffffff;
  border-color: var(--primary);
}
.btn.primary:hover:not([disabled]) { background: var(--primary-active); border-color: var(--primary-active); }
.btn.danger { color: var(--danger); border-color: rgba(239, 68, 68, 0.3); }
.btn.danger:hover:not([disabled]) { background: var(--danger-bg); }
.btn[disabled] { opacity: 0.5; cursor: not-allowed; }

.error-msg {
  padding: 8px 12px;
  background: var(--danger-bg);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: var(--r-md);
  color: var(--danger);
  font-size: 13px;
}
.success-msg {
  padding: 8px 12px;
  background: var(--success-bg);
  border: 1px solid rgba(16, 185, 129, 0.3);
  border-radius: var(--r-md);
  color: var(--success);
  font-size: 13px;
}

.muted-card { padding: 14px 18px; }
.muted-card p { font-size: 12.5px; color: var(--text-muted); line-height: 1.5; margin: 0; }
.muted-card strong { color: var(--text-dim); }

@media (max-width: 768px) {
  .card-body.row-flow { flex-direction: column; align-items: flex-start; }
  .action-row.inline { width: 100%; justify-content: flex-end; }
  /* Repo URL: ellipsis truncating the full GitHub URL on mobile hid
     the value users actually need to copy. Wrap instead so the URL
     reads as multi-line but stays visible/selectable. */
  .repo-url-line code {
    white-space: normal;
    word-break: break-all;
    max-width: 100%;
  }
  /* Card header: icon + title-block + status-chip squeezed on one row
     forced awkward wrapping. Stack: row 1 (icon + title), row 2
     (status chip aligned right). */
  .card-header { flex-wrap: wrap; }
  .card-title-block { flex: 1; min-width: 0; }
  .card-header .status-chip {
    width: auto;
    align-self: flex-start;
  }
  /* Action row + form buttons full-width tap targets. */
  .action-row { flex-wrap: wrap; gap: 8px; }
  .action-row > .btn { flex: 1; min-width: 0; }
}
</style>
