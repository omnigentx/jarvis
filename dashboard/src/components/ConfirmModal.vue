<script setup>
/**
 * ConfirmModal — Reusable dark-themed confirmation dialog.
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
 * Props:
 *   visible     — controls show/hide
 *   title       — modal heading
 *   message     — description text (supports HTML via slot)
 *   confirmText — label for confirm button (default: "Confirm")
 *   cancelText  — label for cancel button (default: "Cancel")
 *   variant     — "danger" | "warning" | "info" (default: "danger")
 *   loading     — shows spinner on confirm button
 *   error       — inline error message
 *   icon        — slot for custom icon
 */
import { computed } from 'vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
  title: { type: String, default: 'Confirm' },
  message: { type: String, default: '' },
  confirmText: { type: String, default: 'Confirm' },
  cancelText: { type: String, default: 'Cancel' },
  variant: { type: String, default: 'danger' }, // danger | warning | info
  loading: { type: Boolean, default: false },
  error: { type: String, default: '' },
})

const emit = defineEmits(['confirm', 'cancel'])

const variantColors = {
  danger:  { main: '#ef4444', bg: 'rgba(239, 68, 68, 0.08)', border: 'rgba(239, 68, 68, 0.15)', btnBg: 'rgba(239, 68, 68, 0.15)', btnBorder: 'rgba(239, 68, 68, 0.25)', btnHoverBg: 'rgba(239, 68, 68, 0.25)', btnHoverBorder: 'rgba(239, 68, 68, 0.4)' },
  warning: { main: '#f59e0b', bg: 'rgba(245, 158, 11, 0.08)', border: 'rgba(245, 158, 11, 0.15)', btnBg: 'rgba(245, 158, 11, 0.15)', btnBorder: 'rgba(245, 158, 11, 0.25)', btnHoverBg: 'rgba(245, 158, 11, 0.25)', btnHoverBorder: 'rgba(245, 158, 11, 0.4)' },
  info:    { main: '#3b82f6', bg: 'rgba(59, 130, 246, 0.08)', border: 'rgba(59, 130, 246, 0.15)', btnBg: 'rgba(59, 130, 246, 0.15)', btnBorder: 'rgba(59, 130, 246, 0.25)', btnHoverBg: 'rgba(59, 130, 246, 0.25)', btnHoverBorder: 'rgba(59, 130, 246, 0.4)' },
}

const colors = computed(() => variantColors[props.variant] || variantColors.danger)
</script>

<template>
  <Teleport to="body">
    <Transition name="confirm-modal">
      <div v-if="visible" class="cm-overlay" @click.self="emit('cancel')">
        <div class="cm-card">
          <!-- Icon -->
          <div class="cm-icon-wrap" :style="{ background: colors.bg, borderColor: colors.border }">
            <slot name="icon">
              <!-- Default: trash icon for danger, warning triangle for warning, info circle for info -->
              <svg v-if="variant === 'danger'" width="28" height="28" viewBox="0 0 24 24" fill="none" :stroke="colors.main" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                <line x1="10" y1="11" x2="10" y2="17"/>
                <line x1="14" y1="11" x2="14" y2="17"/>
              </svg>
              <svg v-else-if="variant === 'warning'" width="28" height="28" viewBox="0 0 24 24" fill="none" :stroke="colors.main" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                <line x1="12" y1="9" x2="12" y2="13"/>
                <line x1="12" y1="17" x2="12.01" y2="17"/>
              </svg>
              <svg v-else width="28" height="28" viewBox="0 0 24 24" fill="none" :stroke="colors.main" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"/>
                <line x1="12" y1="16" x2="12" y2="12"/>
                <line x1="12" y1="8" x2="12.01" y2="8"/>
              </svg>
            </slot>
          </div>

          <!-- Title -->
          <h3 class="cm-title">{{ title }}</h3>

          <!-- Message -->
          <div class="cm-desc">
            <slot>{{ message }}</slot>
          </div>

          <!-- Error -->
          <p v-if="error" class="cm-error" :style="{ color: colors.main, background: colors.bg, borderColor: colors.border }">
            {{ error }}
          </p>

          <!-- Actions -->
          <div class="cm-actions">
            <button class="cm-btn cm-btn-cancel" @click="emit('cancel')" :disabled="loading">
              {{ cancelText }}
            </button>
            <button
              class="cm-btn cm-btn-confirm"
              :style="{
                background: colors.btnBg,
                color: colors.main,
                borderColor: colors.btnBorder,
                '--hover-bg': colors.btnHoverBg,
                '--hover-border': colors.btnHoverBorder,
              }"
              @click="emit('confirm')"
              :disabled="loading"
            >
              <span v-if="loading" class="cm-spinner" :style="{ borderColor: colors.bg, borderTopColor: colors.main }"></span>
              {{ loading ? 'Processing…' : confirmText }}
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
  z-index: 9999;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
}

.cm-card {
  background: #0c0e15;
  border: 1px solid #1a1d2e;
  border-radius: 16px;
  padding: 32px;
  width: 400px;
  max-width: 90vw;
  text-align: center;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255,255,255,0.03);
  animation: cmSlideIn 0.25s ease-out;
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
  color: #f0f2f5;
  margin: 0 0 8px;
  font-family: 'Inter', sans-serif;
}

.cm-desc {
  font-size: 13px;
  color: #8b8fa3;
  line-height: 1.6;
  margin: 0 0 24px;
}

.cm-desc :deep(strong) {
  color: #c4c8d4;
  font-weight: 600;
}

.cm-error {
  font-size: 12px;
  border: 1px solid;
  border-radius: 8px;
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
  border-radius: 10px;
  font-size: 13px;
  font-weight: 600;
  font-family: 'Inter', sans-serif;
  cursor: pointer;
  transition: all 0.2s;
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
  background: #111318;
  color: #c4c8d4;
  border-color: #1a1d2e;
}

.cm-btn-cancel:hover:not(:disabled) {
  background: #1e2233;
  color: #f0f2f5;
  border-color: #2a3556;
}

.cm-btn-confirm:hover:not(:disabled) {
  background: var(--hover-bg) !important;
  border-color: var(--hover-border) !important;
}

.cm-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid;
  border-radius: 50%;
  animation: cmSpin 0.6s linear infinite;
}

@keyframes cmSpin {
  to { transform: rotate(360deg); }
}

/* Transition */
.confirm-modal-enter-active,
.confirm-modal-leave-active {
  transition: opacity 0.2s ease;
}
.confirm-modal-enter-from,
.confirm-modal-leave-to {
  opacity: 0;
}
</style>
