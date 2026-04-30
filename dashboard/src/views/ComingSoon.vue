<script setup>
import { useRoute } from 'vue-router'

const route = useRoute()
</script>

<template>
  <div class="flex flex-col items-center justify-center py-20 text-center">
    <div class="text-6xl mb-4">🚧</div>
    <h2 class="text-xl font-bold text-slate-200 mb-2">Coming Soon</h2>
    <p class="text-sm text-slate-400 max-w-md">
      The <span class="text-blue-400 font-medium">{{ route.meta.title || 'feature' }}</span>
      dashboard is under development and will be available in a future update.
    </p>
    <div class="mt-6 px-4 py-2 rounded-lg bg-slate-700/50 text-xs text-slate-500">
      Phase 2 — Stay tuned!
    </div>
  </div>
</template>
