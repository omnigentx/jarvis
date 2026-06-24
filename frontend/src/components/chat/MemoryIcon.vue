<!--
  Shared memory glyph for the chat chips (recall "memories used" + live
  "saved/pending"). Monochrome line-icon in the app's SVG language
  (viewBox 16, stroke-width 1.2); `currentColor` lets it tint with the
  chip's state colour. Single source so the two chips never drift.
-->
<template>
  <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
    <path d="M4 2.75h8a.5.5 0 0 1 .5.5v10.04a.4.4 0 0 1-.62.34L8 11.3l-3.88 2.33a.4.4 0 0 1-.62-.34V3.25a.5.5 0 0 1 .5-.5z"
          stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>
  </svg>
</template>
