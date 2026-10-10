/**
 * CSVForge — Vitest configuration.
 *
 * WHAT:
 *   Separate from vite.config.ts because Vite 8's own defineConfig does not
 *   include the `test` key in its type definitions. Vitest's defineConfig
 *   (from 'vitest/config') extends Vite's config and adds the `test` key.
 *
 * WHY SEPARATE (not in vite.config.ts):
 *   Vite 8 made a deliberate decision to remove test-related types from the
 *   core Vite config interface — keeping the build tool and test runner
 *   concerns separate. The correct pattern is:
 *     - vite.config.ts        → build tool config (plugins, server, build)
 *     - vitest.config.ts      → test config (environment, setup, coverage)
 *   Vitest merges both files automatically when running tests.
 *
 * ENVIRONMENT: 'jsdom'
 *   The jsdom environment simulates a browser DOM inside Node.js, which React
 *   Testing Library requires. Without it, APIs like document.createElement()
 *   do not exist and every test that renders a component would fail.
 *
 * GLOBALS: true
 *   Makes describe/it/expect/beforeEach available without importing them in
 *   every test file. This matches Jest's default behaviour and reduces
 *   boilerplate. The tsconfig.app.json types array includes "vitest/globals"
 *   so TypeScript also knows about these globals.
 *
 * SETUP FILES:
 *   src/test/setup.ts runs once before each test file and imports
 *   @testing-library/jest-dom, which extends expect() with DOM matchers
 *   like toBeInTheDocument(), toBeDisabled(), toHaveValue().
 */

import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['src/test/setup.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
    },
  },
});
