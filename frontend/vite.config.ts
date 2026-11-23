// `defineConfig` comes from vitest rather than vite: it is the same function
// with the `test` block typed, so the Vitest options below are checked instead
// of being reported as an unknown property.
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    // Proxy the API so the frontend can use same-origin paths. This keeps
    // cookies, SSE, and CORS out of the picture entirely in development.
    //
    // The backend port is a variable rather than a literal because 8000 can be
    // taken by a stale socket on Windows — a process that has exited but whose
    // listening socket is still reserved, which makes every later bind fail
    // with WSAEADDRINUSE while `tasklist` reports no such process. Overriding
    // via BACKEND_PORT is quicker than rebooting to reclaim the port.
    //
    // The fallback is **8001, not 8000**, matching `scripts/dev_run.ps1` and
    // `scripts/restart_backend.ps1`. It was 8000 here while every script used
    // 8001, so the dev server proxied to a port nothing was listening on and
    // every call answered 500 — measured: `connect ECONNREFUSED 127.0.0.1:8000`
    // in the Vite log while the backend was healthy on 8001.
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${process.env.BACKEND_PORT ?? '8001'}`,
        changeOrigin: true,
        // SSE must not be buffered, and the default timeout would cut a long
        // run's stream short.
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (proxyRes.headers['content-type']?.includes('text/event-stream')) {
              proxyRes.headers['cache-control'] = 'no-cache, no-transform';
            }
          });
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: [],
  },
});
