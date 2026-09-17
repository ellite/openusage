import { defineConfig } from 'astro/config';

// Dev only -- the built site is served by the FastAPI backend in
// production, which is same-origin with /api so no proxy is needed there.
const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:4188';

export default defineConfig({
  output: 'static',
  vite: {
    server: {
      proxy: {
        '/api': BACKEND_URL,
      },
    },
  },
});
