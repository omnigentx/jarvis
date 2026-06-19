<script setup>
/**
 * ConfirmModal — Reusable themed confirmation dialog.
 *
 * Usage:
 *   <ConfirmModal
 *     :visible="showModal"
 *     title="Remove Agent"
 *     message="Are you sure?"
 *     confirm-text="Remove"
 *     variant="danger"
 *     :loading="isDeleting"
 *     :error="errorMsg"
 *     @confirm="handleConfirm"
 *     @cancel="handleCancel"
 *   />
 *
 * Props / events / slots are preserved verbatim — visual restyle only.
 */
import { computed } from 'vue'
import { useLang } from '../composables/useLang'

const { t } = useLang()

const props = defineProps({
  visible: { type: Boolean, default: false },
  title: { type: String, default: '' },
  message: { type: String, default: '' },
  confirmText: { type: String, default: '' },
  cancelText: { type: String, default: '' },
  variant: { type: String, default: 'danger' }, // danger | warning | info
  loading: { type: Boolean, default: false },
  error: { type: String, default: '' },
})

const emit = defineEmits(['confirm', 'cancel'])

// Variant drives the icon ring + (for danger/warning) the confirm
// button styling. Info variants get the primary indigo treatment
// because that's the default "go ahead" affirmative.
const variantTokens = {
  danger:  { main: 'var(--danger)',  bg: 'var(--danger-bg)',  border: 'rgba(239, 68, 68, 0.25)'  },
  warning: { main: 'var(--warning)', bg: 'var(--warning-bg)', border: 'rgba(245, 158, 11, 0.25)' },
  info:    { main: 'var(--primary)', bg: 'var(--primary-bg)', border: 'var(--primary-bg-strong)' },
}
const colors = computed(() => variantTokens[props.variant] || variantTokens.danger)
const usePrimaryConfirm = computed(() => props.variant === 'info')
</script>

<template>
  <Teleport to="body">
    <Transition name="confirm-modal">
      <div v-if="visible" class="cm-overlay jv" @click.self="emit('cancel')">
        <div class="cm-card hud">
          <span class="hud-br"></span>

          <!-- Icon -->
          <div class="cm-icon-wrap" :style="{ background: colors.bg, borderColor: colors.border, color: colors.main }">
            <slot name="icon">
              <svg v-if="variant === 'danger'" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                <line x1="10" y1="11" x2="10" y2="17"/>
                <line x1="14" y1="11" x2="14" y2="17"/>
              </svg>
              <svg v-else-if="variant === 'warning'" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                <line x1="12" y1="9" x2="12" y2="13"/>
                <line x1="12" y1="17" x2="12.01" y2="17"/>
              </svg>
              <svg v-else width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"/>
                <line x1="12" y1="16" x2="12" y2="12"/>
                <line x1="12" y1="8" x2="12.01" y2="8"/>
              </svg>
            </slot>
          </div>

          <h3 class="cm-title">{{ title || t('confirm.title') }}</h3>

          <div class="cm-desc">
            <slot>{{ message }}</slot>
          </div>

          <p
            v-if="error"
            class="cm-error"
            :style="{ color: colors.main, background: colors.bg, borderColor: colors.border }"
          >
            {{ error }}
          </p>

          <div class="cm-actions">
            <button
              class="cm-btn cm-btn-cancel"
              @click="emit('cancel')"
              :disabled="loading"
            >
              {{ cancelText || t('common.cancel') }}
            </button>
            <button
              class="cm-btn"
              :class="usePrimaryConfirm ? 'cm-btn-primary' : 'cm-btn-variant'"
              :style="usePrimaryConfirm ? undefined : {
                background: colors.bg,
                color: colors.main,
                borderColor: colors.border,
              }"
              @click="emit('confirm')"
              :disabled="loading"
            >
              <span v-if="loading" class="cm-spinner" :style="{ borderTopColor: usePrimaryConfirm ? '#fff' : colors.main }"></span>
              {{ loading ? t('common.processing') : (confirmText || t('confirm.confirm')) }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.cm-overlay {
  position: fixed;
  inset: 0;
  /* Must sit ABOVE other application modals that themselves use
     z-index: 9999 (e.g. SkillEditorModal). ConfirmModal is mounted
     once at app startup and teleports to <body> first; any modal that
     teleports later (after the user opens it) lands AFTER ConfirmModal
     in DOM order and would win at equal z-index, leaving the confirm
     hidden behind the host modal — the "X / Cancel can't close" stuck
     state reported 2026-05-27. Bumping to 10001 keeps the confirm on
     top regardless of teleport order. */
  z-index: 10001;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  font-family: var(--font-body);
  color: var(--text);
}

.cm-card {
  position: relative;
  background: var(--bg-2);
  border: 1px solid var(--border-bright);
  border-radius: var(--r-xl);
  padding: 32px;
  width: 400px;
  max-width: 90vw;
  /* Long descriptions / multi-line errors could push the action row
     off-screen on phones. Cap height + allow body scroll; sticky
     action row keeps Confirm/Cancel reachable. */
  max-height: calc(100dvh - 32px);
  overflow-y: auto;
  text-align: center;
  box-shadow: var(--shadow-lg);
  animation: cmSlideIn 0.25s var(--ease-out);
}

@media (max-width: 480px) {
  .cm-card {
    padding: 22px 18px;
  }
  .cm-actions {
    /* Stack on phone so each button gets a full-width 44px tap target.
       Reverse so the affirmative (primary) sits on top — matches iOS
       action-sheet convention. */
    flex-direction: column-reverse;
  }
}

@keyframes cmSlideIn {
  from { opacity: 0; transform: scale(0.95) translateY(8px); }
  to   { opacity: 1; transform: scale(1) translateY(0); }
}

.cm-icon-wrap {
  width: 52px;
  height: 52px;
  margin: 0 auto 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid;
  border-radius: 14px;
}

.cm-title {
  font-size: 18px;
  font-weight: 600;
  font-family: var(--font-display);
  color: var(--text);
  margin: 0 0 8px;
}

.cm-desc {
  font-size: 13px;
  color: var(--text-dim);
  line-height: 1.6;
  margin: 0 0 24px;
}
.cm-desc :deep(strong) {
  color: var(--text);
  font-weight: 600;
}

.cm-error {
  font-size: 12px;
  border: 1px solid;
  border-radius: var(--r-md);
  padding: 8px 12px;
  margin: 0 0 16px;
}

.cm-actions {
  display: flex;
  gap: 10px;
  justify-content: center;
}

.cm-btn {
  flex: 1;
  padding: 10px 20px;
  border-radius: var(--r-md);
  font-size: 13px;
  font-weight: 500;
  font-family: var(--font-body);
  cursor: pointer;
  transition: all 0.18s var(--ease-out);
  border: 1px solid transparent;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.cm-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.cm-btn-cancel {
  background: transparent;
  color: var(--text-dim);
  border-color: var(--border-strong);
}
.cm-btn-cancel:hover:not(:disabled) {
  background: var(--bg-3);
  color: var(--text);
  border-color: var(--border-bright);
}

.cm-btn-primary {
  background: linear-gradient(180deg, var(--primary-hover), var(--primary));
  color: #fff;
  border-color: transparent;
  box-shadow: 0 1px 0 rgba(255,255,255,0.18) inset, 0 8px 24px -8px var(--primary-glow);
}
.cm-btn-primary:hover:not(:disabled) {
  transform: translateY(-1px);
}

.cm-btn-variant:hover:not(:disabled) {
  filter: brightness(1.15);
}

.cm-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid transparent;
  border-radius: 50%;
  animation: cmSpin 0.6s linear infinite;
}

@keyframes cmSpin {
  to { transform: rotate(360deg); }
}

.confirm-modal-enter-active,
.confirm-modal-leave-active {
  transition: opacity 0.2s ease;
}
.confirm-modal-enter-from,
.confirm-modal-leave-to {
  opacity: 0;
}
</style>
