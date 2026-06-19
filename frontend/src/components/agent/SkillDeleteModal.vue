<script setup>
/**
 * SkillDeleteModal — type-to-confirm deletion of a user-created skill.
 *
 * The user must type the skill name verbatim before the Delete button
 * unlocks. This is the standard "destructive action" pattern (GitHub,
 * Stripe) — friction proportional to the blast radius.
 *
 * Built-in skills can never reach this modal: the parent disables the
 * delete control for those. As a defence in depth, the API also returns 403.
 *
 * If the skill is referenced by agent cards, we surface that count up-front
 * so the user knows the cleanup will happen. The backend strips the
 * references atomically and reports `removed_from_agents` in the response.
 */
import { ref, computed, watch } from 'vue'
import { apiFetch, ApiError } from '../../api'
import { useLang } from '../../composables/useLang'

const { t } = useLang()

const props = defineProps({
  visible: { type: Boolean, default: false },
  skillName: { type: String, default: '' },
  usedBy: { type: Array, default: () => [] },
})
const emit = defineEmits(['close', 'deleted'])

const typed = ref('')
const deleting = ref(false)
const error = ref('')

const canDelete = computed(() => typed.value === props.skillName && !deleting.value)

watch(
  () => props.visible,
  (v) => {
    if (v) {
      typed.value = ''
      error.value = ''
      deleting.value = false
    }
  },
)

async function onDelete() {
  if (!canDelete.value) return
  deleting.value = true
  error.value = ''
  try {
    const res = await apiFetch(`/api/skills/${encodeURIComponent(props.skillName)}`, {
      method: 'DELETE',
    })
    emit('deleted', res)
  } catch (err) {
    error.value = _friendly(err)
  } finally {
    deleting.value = false
  }
}

function _friendly(err) {
  if (err instanceof ApiError && err.body && typeof err.body === 'object') {
    const detail = err.body.detail
    if (detail && typeof detail === 'object') return detail.message || t('skillEditor.deleteFailed')
    if (typeof detail === 'string') return detail
  }
  return err?.message || String(err)
}
</script>

<template>
  <Teleport to="body">
    <Transition name="sd-modal">
      <div v-if="visible" class="sd-overlay jv" @click.self="emit('close')">
        <div class="sd-card">
          <div class="sd-icon-wrap">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="3 6 5 6 21 6"/>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              <line x1="10" y1="11" x2="10" y2="17"/>
              <line x1="14" y1="11" x2="14" y2="17"/>
            </svg>
          </div>
          <h3 class="sd-title">{{ t('skillEditor.deleteSkillTitle') }}</h3>
          <p class="sd-desc">
            {{ t('skillEditor.deleteDescPre') }} <strong>{{ skillName }}</strong> {{ t('skillEditor.deleteDescPost') }}
          </p>
          <p v-if="usedBy.length" class="sd-warn">
            {{ t('skillEditor.deleteReferencedBy', { n: usedBy.length }) }}
            <strong>{{ usedBy.join(', ') }}</strong>.
            {{ t('skillEditor.deleteRefsCleanup') }}
          </p>

          <label class="sd-label">
            {{ t('skillEditor.typeToConfirmPre') }} <code>{{ skillName }}</code> {{ t('skillEditor.typeToConfirmPost') }}
            <input
              v-model="typed"
              type="text"
              class="sd-input"
              autocomplete="off"
              spellcheck="false"
              :disabled="deleting"
              @keydown.enter="onDelete"
            />
          </label>

          <p v-if="error" class="sd-error">{{ error }}</p>

          <div class="sd-actions">
            <button class="sd-btn sd-btn-cancel" @click="emit('close')" :disabled="deleting">
              {{ t('skillEditor.cancel') }}
            </button>
            <button class="sd-btn sd-btn-delete" :disabled="!canDelete" @click="onDelete">
              <span v-if="deleting" class="sd-spinner"></span>
              {{ deleting ? t('skillEditor.deleting') : t('skillEditor.delete') }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.sd-overlay {
  position: fixed;
  inset: 0;
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  font-family: var(--font-body);
  color: var(--text);
}
.sd-card {
  /* Theme tokens, not hardcoded dark — this modal rendered black in
     light theme because every colour below was a literal hex. */
  background: var(--bg-2);
  border: 1px solid var(--border-bright);
  border-radius: 16px;
  padding: 28px;
  width: 440px;
  max-width: 92vw;
  /* Long usedBy lists (many dependent agents) could push the action
     row off-screen on phones; cap height + scroll the body. dvh
     accounts for iOS Safari URL bar. */
  max-height: calc(100dvh - 32px);
  overflow-y: auto;
  text-align: center;
  box-shadow: var(--shadow-lg);
}
.sd-icon-wrap {
  width: 52px; height: 52px;
  margin: 0 auto 16px;
  display: flex; align-items: center; justify-content: center;
  border: 1px solid rgba(239, 68, 68, 0.2);
  background: rgba(239, 68, 68, 0.08);
  border-radius: 14px;
}
.sd-title {
  margin: 0 0 8px;
  font-size: 18px;
  font-weight: 600;
  color: var(--text);
}
.sd-desc {
  margin: 0 0 12px;
  font-size: 13px;
  color: var(--text-dim);
}
.sd-desc strong { color: var(--text); font-family: ui-monospace, monospace; }
.sd-warn {
  margin: 0 0 16px;
  font-size: 12px;
  color: var(--warning);
  background: var(--warning-bg);
  border: 1px solid rgba(245, 158, 11, 0.2);
  border-radius: 8px;
  padding: 10px 12px;
  text-align: left;
}
.sd-warn strong { color: var(--warning); }
.sd-label {
  display: block;
  margin: 16px 0 6px;
  font-size: 12px;
  color: var(--text-muted);
  text-align: left;
}
.sd-label code {
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  padding: 1px 6px;
  border-radius: 4px;
  color: var(--text);
  font-family: ui-monospace, monospace;
}
.sd-input {
  width: 100%;
  margin-top: 6px;
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  padding: 10px 12px;
  color: var(--text);
  font-family: ui-monospace, monospace;
  font-size: 13px;
}
.sd-input:focus {
  outline: none;
  border-color: var(--danger);
  box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.15);
}
.sd-error {
  margin: 12px 0 0;
  color: var(--danger);
  font-size: 12px;
  text-align: left;
}
.sd-actions {
  display: flex;
  gap: 10px;
  margin-top: 20px;
}
.sd-btn {
  flex: 1;
  padding: 10px 16px;
  border-radius: 10px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid transparent;
}
.sd-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.sd-btn-cancel {
  background: transparent;
  border-color: var(--border-strong);
  color: var(--text-dim);
}
.sd-btn-cancel:hover:not(:disabled) { background: var(--bg-3); color: var(--text); }
.sd-btn-delete {
  background: var(--danger-bg);
  border-color: rgba(239, 68, 68, 0.3);
  color: var(--danger);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}
.sd-btn-delete:hover:not(:disabled) {
  background: rgba(239, 68, 68, 0.25);
  border-color: rgba(239, 68, 68, 0.5);
}
.sd-spinner {
  width: 13px; height: 13px;
  border: 2px solid rgba(239, 68, 68, 0.2);
  border-top-color: #f87171;
  border-radius: 50%;
  animation: sd-spin 0.7s linear infinite;
}
@keyframes sd-spin { to { transform: rotate(360deg); } }

.sd-modal-enter-active,
.sd-modal-leave-active { transition: opacity 0.2s ease; }
.sd-modal-enter-from,
.sd-modal-leave-to { opacity: 0; }
</style>
