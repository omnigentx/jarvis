<script setup>
/**
 * VoiceFAB — persistent voice trigger that lets the user start a
 * hands-free turn from any screen except the chat itself (where the
 * composer already carries a mic).
 *
 * Position: fixed bottom-right, sits ABOVE the mobile tab bar AND
 * the mini audio player when either is visible. Reads --mini-player-h
 * (set by AppLayout) so it never collides with the floating player.
 *
 * Active state mirrors useVoiceSession().status — cyan glow while
 * listening, indigo gradient idle.
 *
 * The component is intentionally mobile-only (renders nothing on
 * `>= 768px`); the desktop topbar exposes Cmd+K which serves the
 * same "voice from anywhere" affordance with the keyboard.
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useBreakpoint } from '../composables/useBreakpoint'
import { useVoiceSession } from '../composables/useVoiceSession'
import { useFabVisibility } from '../composables/useFabVisibility'

const route = useRoute()
const { isMobile } = useBreakpoint()
const voice = useVoiceSession()
const { visible: fabShown } = useFabVisibility()

// Don't render on surfaces that already carry a primary mic — the
// chat composer mic, the setup wizard voice step, login screens.
const HIDDEN_ROUTES = ['/chat', '/setup', '/login']

const visible = computed(() => {
  if (!isMobile.value) return false
  return !HIDDEN_ROUTES.some((p) => route.path.startsWith(p))
})

const isActive = computed(() => {
  const s = voice.status.value
  return s === 'listening' || s === 'thinking' || s === 'speaking'
})

async function onTap() {
  if (isActive.value) {
    voice.stop()
  } else {
    try {
      await voice.start()
    } catch (_) { /* surfaced via voice.error */ }
  }
}
</script>

<template>
  <button
    v-if="visible"
    class="voice-fab jv"
    :class="{ 'voice-fab--active': isActive, 'voice-fab--hidden': !fabShown }"
    :aria-pressed="isActive"
    :aria-label="isActive ? 'Stop voice' : 'Start voice'"
    @click="onTap"
  >
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
      <path
        d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM5 11a7 7 0 0 0 14 0M12 18v3M9 21h6"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    </svg>
  </button>
</template>

<style scoped>
.voice-fab {
  position: fixed;
  right: 16px;
  /* Stack above the tab bar (56px content + safe-area) AND the mini
     audio player when it's visible. --mini-player-h is 0 by default,
     64 while playing. */
  bottom: calc(var(--mobile-tabbar-h) + var(--safe-bottom) + var(--mini-player-h, 0px) + 12px);
  z-index: 195;
  width: 52px;
  height: 52px;
  border-radius: 50%;
  border: 0;
  /* Translucent + frosted so content behind the FAB stays partly visible
     (covers less). The white icon stays fully opaque for contrast. */
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--primary-hover) 82%, transparent),
    color-mix(in srgb, var(--primary) 82%, transparent));
  -webkit-backdrop-filter: blur(8px);
  backdrop-filter: blur(8px);
  color: white;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  box-shadow: 0 8px 24px var(--primary-glow);
  transition: transform 0.2s var(--ease-out), opacity 0.2s var(--ease-out),
              background 0.22s var(--ease-out), box-shadow 0.22s var(--ease-out);
}
.voice-fab:active { transform: scale(0.96); }
/* Hidden (manual grip or scroll-down): remove entirely so no phantom box /
   blurred square is left over content (a translated opacity:0 button keeps its
   52px layout box + can ghost its backdrop-filter in Chrome). */
.voice-fab--hidden { display: none; }
.voice-fab--active {
  background: var(--accent);
  color: #0B0D12;
  box-shadow: 0 0 28px var(--shadow-glow-cyan);
  animation: voiceFabPulse 1.4s ease-in-out infinite;
}
@keyframes voiceFabPulse {
  0%, 100% { box-shadow: 0 0 28px var(--shadow-glow-cyan); }
  50%      { box-shadow: 0 0 18px var(--shadow-glow-cyan), 0 0 0 6px rgba(34, 211, 238, 0.18); }
}
</style>
