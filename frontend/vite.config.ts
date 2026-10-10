/**
 * CSVForge — Vite 8 configuration.
 *
 * WHAT:
 *   Vite is the build tool and dev server. Vite 8 uses Rolldown (a Rust-based
 *   bundler) for production builds — faster than the previous esbuild+Rollup
 *   combination. The dev server still uses esbuild for fast HMR.
 *
 * WHY @vitejs/plugin-react (v6.1.2):
 *   React 19 uses the new JSX transform (no `import React` needed in every file).
 *   The plugin enables Fast Refresh (HMR) for React components. Without it,
 *   every save would do a full page reload instead of patching just the
 *   changed component.
 *
 * NOTE ON TEST CONFIG:
 *   In Vite 8, the `test` key cannot live in vite.config.ts — Vite's own
 *   defineConfig does not include Vitest types. Vitest configuration is in
 *   vitest.config.ts, which uses Vitest's defineConfig that extends Vite's.
 *   The two configs are automatically merged by Vitest at test time.
 *
 * SERVER PROXY:
 *   Not configured. The frontend calls the API using VITE_API_URL directly.
 *   CORS is configured on the backend. Using a proxy would hide CORS errors
 *   during development, making configuration problems harder to catch.
 */
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],

  // ── Development server ────────────────────────────────────────────────────
  // Port 5173 is the Vite default and is what the backend CORS middleware
  // whitelists as the local dev origin.
  server: {
    port: 5173,
    strictPort: true, // fail if 5173 is already in use (don't silently move to 5174)
  },

  // ── Build output ──────────────────────────────────────────────────────────
  // Default build output is dist/. The CloudFormation deploy script syncs
  // this directory to the S3 frontend bucket.
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});
