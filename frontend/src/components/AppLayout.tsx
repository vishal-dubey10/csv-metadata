/**
 * CSVForge — main application shell layout.
 *
 * WHAT:
 *   Top navigation bar + page content area. Wraps all authenticated user pages.
 *   The admin section uses its own React Admin layout.
 *
 * NAVIGATION:
 *   - Logo/title → links to job list (/)
 *   - "Upload" button → /upload (primary action)
 *   - User email display (read-only)
 *   - Logout button
 *   - "Admin" link visible only to users with isAdmin=true
 */

import React from 'react';
import {
  AppBar,
  Box,
  Button,
  Container,
  Toolbar,
  Typography,
} from '@mui/material';
import { Link as RouterLink, Outlet } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';

export default function AppLayout(): React.ReactElement {
  const { user, logout } = useAuth();

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      {/* ── Top navigation bar ───────────────────────────────────────────── */}
      <AppBar position="static" elevation={0} sx={{ borderBottom: 1, borderColor: 'divider' }}>
        <Toolbar>
          {/* App name / home link */}
          <Typography
            variant="h6"
            component={RouterLink}
            to="/"
            sx={{
              flexGrow: 1,
              textDecoration: 'none',
              color: 'inherit',
              fontWeight: 700,
            }}
          >
            CSVForge
          </Typography>

          {/* Upload primary CTA */}
          <Button
            component={RouterLink}
            to="/upload"
            variant="contained"
            color="secondary"
            size="small"
            sx={{ mr: 2 }}
          >
            Upload CSV
          </Button>

          {/* Admin link (only for admins) */}
          {user?.isAdmin && (
            <Button
              component="a"
              href="/admin"
              color="inherit"
              size="small"
              sx={{ mr: 2 }}
            >
              Admin
            </Button>
          )}

          {/* User email */}
          {user?.email && (
            <Typography
              variant="body2"
              sx={{ mr: 2, opacity: 0.8, display: { xs: 'none', sm: 'block' } }}
            >
              {user.email}
            </Typography>
          )}

          {/* Logout */}
          <Button color="inherit" size="small" onClick={logout}>
            Sign out
          </Button>
        </Toolbar>
      </AppBar>

      {/* ── Page content ─────────────────────────────────────────────────── */}
      <Box component="main" sx={{ flexGrow: 1, backgroundColor: 'grey.50' }}>
        <Container maxWidth="lg" sx={{ py: 4 }}>
          <Outlet />
        </Container>
      </Box>
    </Box>
  );
}
