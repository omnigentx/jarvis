<script setup>
/**
 * MeetingDetailView — Track 5 new view.
 *
 * MeetingsView already does master-detail in one screen with the detail
 * rendered as a panel. This file exists so deep-link routes like
 * /meetings/:meetingId can resolve to the same surface. It just redirects
 * to /meetings?id=… so the panel-driven master-detail keeps a single source
 * of truth (one SSE subscription, one selection state).
 *
 * Not wired to a route by default — Track 5's router budget is a single
 * insertion for /meetings. If a future track wants per-meeting deep links,
 * add { path: '/meetings/:meetingId', component: () => import('./views/MeetingDetailView.vue') }
 * and this file will Just Work.
 */
import { onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

onMounted(() => {
  const id = route.params.meetingId
  router.replace({ name: 'Meetings', query: id ? { id } : {} })
})
</script>

<template>
  <div class="meeting-detail-redirect">
    <span class="hint">Loading meeting…</span>
  </div>
</template>

<style scoped>
.meeting-detail-redirect {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-0);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 12px;
}
</style>
