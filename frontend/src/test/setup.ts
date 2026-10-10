/**
 * Vitest global test setup.
 *
 * WHAT:
 *   This file runs once before each test file (configured via setupFiles in
 *   vite.config.ts). It extends Vitest's `expect` with the @testing-library/jest-dom
 *   matchers, which provide readable DOM assertions like:
 *     expect(element).toBeInTheDocument()
 *     expect(button).toBeDisabled()
 *     expect(input).toHaveValue('foo')
 *
 * WHY:
 *   Vitest's built-in expect only knows about JavaScript values. The jest-dom
 *   matchers understand the browser DOM — they check rendered HTML, not just
 *   JavaScript objects. Without them, you'd have to write:
 *     expect(document.body.contains(element)).toBe(true)
 *   instead of:
 *     expect(element).toBeInTheDocument()
 */
import '@testing-library/jest-dom';
