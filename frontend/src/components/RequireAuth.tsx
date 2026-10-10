/**
 * RequireAuth — route guard component.
 *
 * WHAT:
 *   Wraps a route to require authentication. If the user is not authenticated,
 *   redirects to /login while preserving the originally requested path so the
 *   user is sent back after logging in.
 *
 * USAGE:
 *   <Route path="/jobs" element={<RequireAuth><JobListPage /></RequireAuth>} />
 *
 * LOADING STATE:
 *   Shows nothing (null) while auth state is initializing. This prevents a
 *   flash of the login page on page refresh when the user has a valid Cognito
 *   session that is being restored asynchronously.
 *
 * ADMIN GUARD:
 *   Pass requireAdmin={true} to additionally require the csvforge-admins group.
 *   Returns 403 content instead of redirecting (admin path is known).
 */

import React from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { Box, Typography } from '@mui/material';
import { useAuth } from '../auth/AuthContext';

interface RequireAuthProps {
  children: React.ReactNode;
  requireAdmin?: boolean;
}

export default function RequireAuth({
  children,
  requireAdmin = false,
}: RequireAuthProps): React.ReactElement | null {
  const { isAuthenticated, isLoading, user } = useAuth();
  const location = useLocation();

  // Still determining auth state — render nothing to avoid flash.
  if (isLoading) return null;

  if (!isAuthenticated) {
    return (
      <Navigate
        to="/login"
        state={{ from: location.pathname }}
        replace
      />
    );
  }

  if (requireAdmin && !user?.isAdmin) {
    return (
      <Box sx={{ p: 4, textAlign: 'center' }}>
        <Typography variant="h5" gutterBottom>
          Access Denied
        </Typography>
        <Typography color="text.secondary">
          This area requires administrator access.
        </Typography>
      </Box>
    );
  }

  return <>{children}</>;
}
