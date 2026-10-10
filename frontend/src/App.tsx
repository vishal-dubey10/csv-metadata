/**
 * CSVForge — Application root with routing.
 *
 * WHAT:
 *   Defines the complete React Router v7 route tree.
 *   Wraps all routes in AuthProvider so every component can call useAuth().
 *
 * ROUTE STRUCTURE:
 *
 *   /login              → LoginPage (public)
 *   /                   → AppLayout (authenticated shell)
 *   ├── index           → JobListPage (home)
 *   ├── /upload         → UploadPage
 *   ├── /jobs/:jobId    → JobDetailPage
 *   └── *               → redirect to /
 *
 *   /admin/*            → AdminApp (React Admin, requires isAdmin=true)
 *
 * REACT ROUTER v7 NOTES:
 *   - Uses createBrowserRouter + RouterProvider (the "data router" API).
 *   - This enables future use of loader/action functions without refactoring.
 *   - The <Outlet /> in AppLayout renders the matching child route.
 *   - RequireAuth wraps the AppLayout element, protecting all child routes.
 *
 * WHY REACT ADMIN HAS ITS OWN ROUTE:
 *   React Admin creates its own internal router. Mounting it inside the main
 *   router causes duplicate router conflicts. Instead, AdminApp is rendered
 *   at /admin and takes over the full page — it never renders inside AppLayout.
 *
 * MUI THEME:
 *   A minimal theme is applied globally. The default MUI v9 theme is used
 *   with a CSVForge-specific primary color. All components inherit from this.
 */

import React from 'react';
import {
  createBrowserRouter,
  RouterProvider,
  Navigate,
} from 'react-router-dom';
import { ThemeProvider, createTheme, CssBaseline } from '@mui/material';
import { AuthProvider } from './auth/AuthContext';
import RequireAuth from './components/RequireAuth';
import AppLayout from './components/AppLayout';
import LoginPage from './pages/LoginPage';
import JobListPage from './pages/JobListPage';
import JobDetailPage from './pages/JobDetailPage';
import UploadPage from './pages/UploadPage';
import AdminApp from './admin/AdminApp';

// ── MUI Theme ─────────────────────────────────────────────────────────────────

const theme = createTheme({
  palette: {
    primary: {
      main: '#1976d2', // MUI default blue — clear, professional, accessible
    },
    secondary: {
      main: '#0288d1', // slightly lighter blue for secondary actions
    },
  },
  typography: {
    fontFamily: [
      '-apple-system',
      'BlinkMacSystemFont',
      '"Segoe UI"',
      'Roboto',
      '"Helvetica Neue"',
      'Arial',
      'sans-serif',
    ].join(','),
  },
  shape: {
    borderRadius: 8,
  },
});

// ── Router ────────────────────────────────────────────────────────────────────

const router = createBrowserRouter([
  // ── Public routes ─────────────────────────────────────────────────────────
  {
    path: '/login',
    element: <LoginPage />,
  },

  // ── Admin surface (React Admin manages its own routing under /admin) ───────
  {
    path: '/admin/*',
    element: (
      <RequireAuth requireAdmin>
        <AdminApp />
      </RequireAuth>
    ),
  },

  // ── Authenticated user routes (all inside AppLayout) ──────────────────────
  {
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <JobListPage /> },
      { path: 'upload', element: <UploadPage /> },
      { path: 'jobs/:jobId', element: <JobDetailPage /> },
      // Catch-all: redirect unknown paths to home
      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
]);

// ── App ───────────────────────────────────────────────────────────────────────

export default function App(): React.ReactElement {
  return (
    <ThemeProvider theme={theme}>
      {/*
        CssBaseline applies a consistent CSS reset across browsers:
          - box-sizing: border-box on all elements
          - removes default margin on body
          - sets a consistent baseline font size
        Without it, padding/margin behaviour differs across browsers,
        causing subtle layout issues.
      */}
      <CssBaseline />
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </ThemeProvider>
  );
}
