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
        // Forward the browser's original Host so the backend can derive
        // the WebAuthn RP ID from it. ``changeOrigin: true`` rewrites
        // the upstream Host to the target (``127.0.0.1:8001``), which
        // would make the backend believe the user is on ``127.0.0.1``
        // and reject the passkey ceremony from a ``localhost`` page
        // with "RP ID is invalid for this domain". Setting
        // ``X-Forwarded-Host`` mirrors what nginx/Caddy do in
        // production so the backend's RP-ID derivation works the same
        // in both environments.
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq, req) => {
            const host = req.headers.host
            if (host) proxyReq.setHeader('X-Forwarded-Host', host)
          })
        },
      },
      // /ws/voice + any future WebSocket route. ws:true is required for the
      // upgrade handshake; without it the dev frontend can't open a socket
      // and the VoiceBar mic toggle just spins on "Connecting…".
      //
      // Forward X-Forwarded-Host on the WS upgrade so the backend's
      // ``_expected_ws_origin`` (routes/ws_voice.py) sees the browser's
      // original host (localhost:3000) instead of the proxy target
      // (localhost:8000). Without this, the Origin-allowlist check
      // rejects the cookie auth path with
      // "cookie valid but Origin rejected: http://localhost:3000".
      '/ws': {
        target: (process.env.VITE_PROXY_TARGET || 'http://localhost:8000').replace(/^http/, 'ws'),
        ws: true,
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('proxyReqWs', (proxyReq, req) => {
            const host = req.headers.host
            if (host) proxyReq.setHeader('X-Forwarded-Host', host)
          })
        },
      },
    },
  },
})
