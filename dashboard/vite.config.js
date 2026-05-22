import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    tailwindcss(),
  ],

  server: {
    port: Number(process.env.VITE_PORT || 3000),
    proxy: {
      '/api': {
        target: process.env.VITE_PROXY_TARGET || 'http://localhost:8000',
        changeOrigin: true,
      },
      // /ws/voice + any future WebSocket route. ws:true is required for the
      // upgrade handshake; without it the dev frontend can't open a socket
      // and the VoiceBar mic toggle just spins on "Connecting…".
      '/ws': {
        target: (process.env.VITE_PROXY_TARGET || 'http://localhost:8000').replace(/^http/, 'ws'),
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
