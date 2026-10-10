/**
 * ESLint 9 flat config format.
 * ESLint 9 replaced the legacy .eslintrc format with "flat config" (eslint.config.js).
 * Each entry in the array is a config object that applies to files matching `files`.
 */
import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import reactPlugin from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';

export default tseslint.config(
  { ignores: ['dist', 'node_modules', 'coverage'] },

  // Base JS rules
  js.configs.recommended,

  // TypeScript rules (strict mode catches more issues)
  ...tseslint.configs.recommended,

  // React-specific rules
  {
    files: ['**/*.{ts,tsx}'],
    plugins: {
      react: reactPlugin,
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    settings: {
      react: { version: 'detect' },
    },
    rules: {
      ...reactPlugin.configs.recommended.rules,
      ...reactHooks.configs.recommended.rules,
      // React 19 with new JSX transform doesn't need React in scope
      'react/react-in-jsx-scope': 'off',
      'react/prop-types': 'off', // TypeScript handles this
      // Warn on components that can't be hot-reloaded safely
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // Prefer const where possible
      'prefer-const': 'error',
      // No any — use unknown for untyped external data
      '@typescript-eslint/no-explicit-any': 'error',
      // Allow _prefixed unused variables (common pattern for destructuring)
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    },
  },

  // Relax rules for test files
  {
    files: ['**/*.test.{ts,tsx}', 'src/test/**'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
);
