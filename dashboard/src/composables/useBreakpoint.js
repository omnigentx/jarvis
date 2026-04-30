/**
 * useBreakpoint — Reactive breakpoint detection.
 *
 * Usage:
 *   const { isMobile, isTablet, isDesktop } = useBreakpoint()
 */
import { ref, onMounted, onUnmounted } from 'vue'

const MOBILE_BREAKPOINT = 768

export function useBreakpoint() {
  const isMobile = ref(false)
  const windowWidth = ref(0)

  function update() {
    windowWidth.value = window.innerWidth
    isMobile.value = window.innerWidth < MOBILE_BREAKPOINT
  }

  onMounted(() => {
    update()
    window.addEventListener('resize', update)
  })

  onUnmounted(() => {
    window.removeEventListener('resize', update)
  })

  return {
    isMobile,
    windowWidth,
  }
}
