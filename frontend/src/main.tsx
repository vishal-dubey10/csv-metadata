/**
 * CSVForge — application entry point.
 *
 * WHAT:
 *   The module that Vite's index.html references as the entry point
 *   (<script type="module" src="/src/main.tsx">). This file mounts the
 *   React application into the DOM element with id="root".
 *
 * React.StrictMode:
 *   Wrapping the app in <StrictMode> causes React to intentionally double-invoke
 *   components, effects, and state initialisers in development (not production).
 *   This surfaces bugs caused by side effects in renders (which should be pure)
 *   and effects that don't clean up correctly. If your component breaks in
 *   StrictMode, it has a bug that would manifest as a subtle issue in production.
 *   Always leave StrictMode enabled during development.
 *
 * React 19 note:
 *   React 19 uses the new "createRoot" API (introduced in React 18). The old
 *   ReactDOM.render() is removed in React 19. createRoot() enables concurrent
 *   features (Suspense, transitions, etc.) that React 19 relies on.
 */

import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error(
    'Root element not found. Ensure index.html has <div id="root">.',
  );
}

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
