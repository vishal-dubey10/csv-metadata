/**
 * CSVForge — Login page.
 *
 * WHAT:
 *   A simple email + password login form that calls AuthContext.login().
 *   Redirects to the app on success. In local dev mode (no Cognito configured),
 *   this page is never shown — the AuthProvider automatically authenticates.
 *
 * SECURITY:
 *   - Passwords are never logged or stored.
 *   - Error messages do not reveal whether an email exists (same message for
 *     wrong email and wrong password — prevents user enumeration).
 *   - No "remember me" option — tokens are in-memory only (AD-10).
 *
 * ACCESSIBILITY:
 *   - Labels are associated with inputs via htmlFor/id.
 *   - Error messages use role="alert" for screen reader announcement.
 *   - The submit button is disabled during the login request.
 *   - Keyboard: Enter in either field submits the form.
 *
 * MUI v9 API CHANGES:
 *   - fontWeight, display, textAlign must go in sx (not as direct Typography props).
 *   - TextField.inputProps is removed. Use slotProps={{ htmlInput: {...} }} to pass
 *     attributes to the underlying <input> element.
 *
 *   WHY slotProps INSTEAD OF inputProps:
 *     MUI v9 adopted the "slots" pattern for composing component internals.
 *     A TextField is composed of several slots: the outer wrapper, the label,
 *     the input element itself (htmlInput), and the helper text. slotProps
 *     lets you pass props to any specific slot without ambiguity.
 *     The old inputProps was ambiguous — did it target the wrapper div or the
 *     inner <input>? slotProps.htmlInput is explicit: it targets the <input>.
 */

import React, { useState } from 'react';
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Container,
  Paper,
  TextField,
  Typography,
} from '@mui/material';
import { useNavigate, useLocation } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';

export default function LoginPage(): React.ReactElement {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  // Redirect to the page the user tried to access before being sent to login,
  // or fall back to the home page.
  const from = (location.state as { from?: string } | null)?.from ?? '/';

  const handleSubmit = async (e: React.FormEvent): Promise<void> => {
    e.preventDefault();
    if (!email.trim() || !password) return;

    setError(null);
    setIsLoading(true);

    try {
      await login(email.trim(), password);
      navigate(from, { replace: true });
    } catch (err) {
      const message =
        err instanceof Error ? err.message : 'Login failed. Please try again.';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <Box
      sx={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        backgroundColor: 'grey.50',
      }}
    >
      <Container maxWidth="xs">
        {/* App header */}
        <Box sx={{ textAlign: 'center', mb: 4 }}>
          {/*
            fontWeight and color are applied via sx (MUI v9 requirement).
            component="h1" uses polymorphic rendering: the Typography renders
            as a semantic <h1> element while keeping the "h4" visual style.
            This gives correct document outline (SEO + accessibility) without
            styling conflicts.
          */}
          <Typography
            variant="h4"
            component="h1"
            color="primary"
            sx={{ fontWeight: 700 }}
          >
            CSVForge
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
            CSV inspection, cleaning, and download
          </Typography>
        </Box>

        <Paper elevation={2} sx={{ p: 4, borderRadius: 2 }}>
          <Typography variant="h6" component="h2" sx={{ mb: 3 }}>
            Sign in
          </Typography>

          {/* Error alert — role="alert" ensures screen readers announce it */}
          {error && (
            <Alert severity="error" role="alert" sx={{ mb: 2 }}>
              {error}
            </Alert>
          )}

          <Box component="form" onSubmit={handleSubmit} noValidate>
            {/*
              slotProps.htmlInput passes aria-label to the underlying <input>
              element. This gives screen readers a descriptive label even if
              the visual label is not connected via htmlFor/id (belt-and-suspenders
              accessibility).

              In MUI v9 the legacy inputProps prop was removed. slotProps is the
              correct replacement: each "slot" in the TextField composition can
              receive its own props without ambiguity.
            */}
            <TextField
              id="email"
              label="Email address"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              autoFocus
              fullWidth
              required
              disabled={isLoading}
              sx={{ mb: 2 }}
              slotProps={{ htmlInput: { 'aria-label': 'Email address' } }}
            />

            <TextField
              id="password"
              label="Password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              fullWidth
              required
              disabled={isLoading}
              sx={{ mb: 3 }}
              slotProps={{ htmlInput: { 'aria-label': 'Password' } }}
            />

            <Button
              type="submit"
              variant="contained"
              fullWidth
              size="large"
              disabled={isLoading || !email.trim() || !password}
              sx={{ position: 'relative' }}
            >
              {isLoading ? (
                <>
                  <CircularProgress
                    size={20}
                    sx={{ position: 'absolute', left: '50%', ml: '-10px' }}
                    aria-label="Signing in"
                  />
                  {/* Invisible placeholder preserves button height while spinner shows */}
                  <Box component="span" sx={{ visibility: 'hidden' }}>Sign in</Box>
                </>
              ) : (
                'Sign in'
              )}
            </Button>
          </Box>
        </Paper>

        {/*
          display="block" is no longer a direct Typography prop in MUI v9.
          Use sx={{ display: 'block' }} instead. Same for textAlign — use sx.
        */}
        <Typography
          variant="caption"
          color="text.disabled"
          sx={{ display: 'block', textAlign: 'center', mt: 3 }}
        >
          Your CSV files are processed securely and never stored permanently.
        </Typography>
      </Container>
    </Box>
  );
}
