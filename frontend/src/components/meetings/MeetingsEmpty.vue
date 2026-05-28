<script setup>
/**
 * MeetingsEmpty — placeholder shown when no meeting is selected. The
 * MeetingsView always renders this in the detail column until the user
 * picks a meeting from the list, so users on a fresh /v2/meetings load
 * see a "select a meeting" prompt instead of a blank pane.
 */
</script>

<template>
  <div class="mt-empty">
    <div class="mt-empty-card">
      <div class="mt-empty-icon">🎙</div>
      <h3 class="mt-empty-title">Select a meeting</h3>
      <p class="mt-empty-body">
        Pick a meeting from the list to view its transcript in real time.
        When agents call <code>create_meeting(agenda, participants, max_rounds)</code>,
        meetings appear in the left rail and auto-stream once a participant joins.
      </p>
      <div class="mt-empty-mono">
        END BY VERDICT · [DECISION] VERDICT: PASS — &lt;summary&gt;
      </div>
    </div>
  </div>
</template>

<style scoped>
.mt-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 28px;
  background: var(--bg-0);
}
.mt-empty-card {
  max-width: 420px;
  padding: 28px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  text-align: center;
}
.mt-empty-icon { font-size: 32px; margin-bottom: 10px; }
.mt-empty-title {
  margin: 0 0 8px;
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
}
.mt-empty-body {
  margin: 0 0 14px;
  font-size: 12.5px;
  color: var(--text-dim);
  line-height: 1.55;
}
.mt-empty-body code {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--accent);
  background: var(--bg-3);
  padding: 1px 6px;
  border-radius: var(--r-sm);
}
.mt-empty-mono {
  font-family: var(--font-mono);
  font-size: 9.5px;
  letter-spacing: 0.16em;
  color: var(--text-muted);
}
</style>
