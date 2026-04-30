<script setup>
defineProps({
  status: {
    type: String,
    default: 'idle',
  },
  size: {
    type: String,
    default: 'md',
  },
})

// Figma: badge frame fill=#22c55e fillOpacity=0.15, cornerRadius=12, h=24, px=8
// dot: Ellipse 6x6 fill=#22c55e
// text: 11px SemiBold #22c55e
const statusConfig = {
  running:   { label: 'Running',   dotColor: '#22c55e', textColor: '#22c55e', bgColor: 'rgba(34,197,94,0.15)', pulse: true },
  paused:    { label: 'Paused',    dotColor: '#f59e0b', textColor: '#f59e0b', bgColor: 'rgba(245,158,11,0.15)', pulse: false },
  idle:      { label: 'Idle',      dotColor: '#64748b', textColor: '#64748b', bgColor: 'rgba(100,116,139,0.12)', pulse: false },
  blocked:   { label: 'Blocked',   dotColor: '#ef4444', textColor: '#ef4444', bgColor: 'rgba(239,68,68,0.15)', pulse: false },
  error:     { label: 'Error',     dotColor: '#ff4560', textColor: '#ff4560', bgColor: 'rgba(255,69,96,0.15)', pulse: false },
  completed: { label: 'Completed', dotColor: '#00c896', textColor: '#00c896', bgColor: 'rgba(0,200,150,0.15)', pulse: false },
  spawning:  { label: 'Spawning',  dotColor: '#ffb547', textColor: '#ffb547', bgColor: 'rgba(255,181,71,0.15)', pulse: true },
}
</script>

<template>
  <!-- Figma: h=24, cornerRadius=12, px=8, gap=6 -->
  <span
    class="inline-flex items-center shrink-0"
    :style="{
      height: '24px',
      padding: '0 8px',
      gap: '6px',
      borderRadius: '12px',
      background: statusConfig[status]?.bgColor || 'rgba(100,116,139,0.12)',
    }"
  >
    <!-- Figma: Ellipse 6x6 -->
    <span
      class="shrink-0"
      :class="statusConfig[status]?.pulse ? 'animate-pulse-dot' : ''"
      :style="{
        width: '6px',
        height: '6px',
        borderRadius: '50%',
        background: statusConfig[status]?.dotColor || '#64748b',
      }"
    ></span>
    <!-- Figma: 11px SemiBold -->
    <span
      :style="{
        fontSize: '11px',
        fontWeight: '600',
        lineHeight: '13px',
        color: statusConfig[status]?.textColor || '#64748b',
      }"
    >
      {{ statusConfig[status]?.label || status }}
    </span>
  </span>
</template>
