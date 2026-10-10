/**
 * Tests for AuthContext and AuthProvider.
 *
 * WHAT:
 *   Verifies the auth context behaves correctly in local dev mode (no Cognito):
 *     - AuthProvider auto-authenticates in local dev mode
 *     - useAuth() returns the dev user identity
 *     - login() accepts any credentials in local dev mode
 *     - logout() clears the user
 *
 * LOCAL DEV MODE:
 *   VITE_COGNITO_USER_POOL_ID is empty (set by .env.local in the project).
 *   In the test environment, import.meta.env.VITE_COGNITO_USER_POOL_ID is
 *   an empty string (the default from vite-env.d.ts), so AuthContext
 *   enters local dev bypass mode automatically.
 *
 * WHY NOT TEST THE COGNITO PATH:
 *   The Cognito path calls amazon-cognito-identity-js which makes real HTTP
 *   requests to AWS. Mocking that library would require complex setup and
 *   would test the mock, not our code. The Cognito path is verified manually
 *   after AWS infrastructure is deployed (pending — see PROGRESS.md).
 *
 * TESTING PATTERN — renderHook + act:
 *   renderHook renders a component that only calls a hook, letting us test
 *   hook behaviour in isolation. act() wraps state updates — without it,
 *   React would warn about state updates outside of act and the test
 *   assertions might run before the state has settled.
 */

import React from 'react';
import { renderHook, act } from '@testing-library/react';
import { AuthProvider, useAuth } from '../auth/AuthContext';

// ── Wrapper ───────────────────────────────────────────────────────────────────

function wrapper({ children }: { children: React.ReactNode }): React.ReactElement {
  return <AuthProvider>{children}</AuthProvider>;
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('AuthContext — local dev mode', () => {
  it('auto-authenticates with a dev user on mount', async () => {
    const { result } = renderHook(() => useAuth(), { wrapper });

    // Wait for the useEffect initialization to complete
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(result.current.isAuthenticated).toBe(true);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.user).not.toBeNull();
    expect(result.current.user?.userId).toBe('dev-user-001');
  });

  it('provides user email in local dev mode', async () => {
    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(result.current.user?.email).toBe('dev@csvforge.local');
  });

  it('login() accepts any credentials in local dev mode', async () => {
    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await result.current.login('anyuser@test.com', 'anypassword');
    });

    expect(result.current.isAuthenticated).toBe(true);
    expect(result.current.user?.email).toBe('anyuser@test.com');
  });

  it('logout() clears the auth state', async () => {
    const { result } = renderHook(() => useAuth(), { wrapper });

    // First ensure we're logged in
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(result.current.isAuthenticated).toBe(true);

    // Now logout
    act(() => {
      result.current.logout();
    });

    expect(result.current.isAuthenticated).toBe(false);
    expect(result.current.user).toBeNull();
  });

  it('throws if useAuth is called outside AuthProvider', () => {
    // Suppress React error boundary output for this test
    const consoleSpy = vi
      .spyOn(console, 'error')
      .mockImplementation(() => undefined);

    expect(() => renderHook(() => useAuth())).toThrow(
      'useAuth must be used within <AuthProvider>',
    );

    consoleSpy.mockRestore();
  });
});
