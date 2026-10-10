/**
 * CSVForge — Authentication context.
 *
 * WHAT:
 *   Provides application-wide authentication state and actions via React Context.
 *   Any component can call useAuth() to get the current user or trigger login/logout.
 *
 * TWO PATHS (AD-FE-03):
 *
 *   LOCAL DEV PATH (VITE_COGNITO_USER_POOL_ID is empty):
 *     No Cognito calls. A synthetic dev user is created immediately on mount.
 *     The API client sends X-Dev-User-Id / X-Dev-User-Email headers (backend
 *     reads these when ENVIRONMENT=local). Login page is bypassed entirely.
 *     Use this path while the AWS Cognito infrastructure is not yet deployed.
 *
 *   PRODUCTION PATH (VITE_COGNITO_USER_POOL_ID is set):
 *     Uses amazon-cognito-identity-js to authenticate against the real Cognito
 *     User Pool. The flow is:
 *       1. User submits email + password on the Login page.
 *       2. AuthContext.login() calls Cognito InitiateAuth (USER_PASSWORD_AUTH).
 *       3. On success, Cognito returns ID token + Access token + Refresh token.
 *       4. Access token is stored in module memory (AD-10, no localStorage).
 *       5. Access token is passed to the API client via setAuthToken().
 *       6. User email is extracted from the ID token payload (base64 decode).
 *       7. isAuthenticated becomes true; components render protected content.
 *
 * TOKEN STORAGE (AD-10):
 *   Tokens are held in React state (in-memory only). They are lost on page
 *   refresh — the user must log in again. This is the intentional MVP trade-off:
 *   localStorage is accessible to any JavaScript on the page (XSS risk).
 *   In-memory storage has no XSS risk but no persistence.
 *
 * SILENT REFRESH:
 *   Out of MVP scope. When the access token expires, API calls will return 401,
 *   the error handler redirects to login. The user re-authenticates.
 *   See AD-10 for the documented trade-off.
 *
 * COGNITO LIBRARY:
 *   amazon-cognito-identity-js v6.3.15. This is the low-level Cognito SDK.
 *   It provides the InitiateAuth flow without requiring Amplify, which would
 *   add significant bundle weight and configuration complexity.
 *
 * INTERVIEW CONCEPT:
 *   "Why React Context for auth state instead of a state management library?"
 *   Auth state (isAuthenticated, user) is truly global — every component in
 *   the tree potentially needs it. React Context is the built-in mechanism for
 *   this pattern. A state management library (Redux, Zustand) would add
 *   complexity and bundle size with no benefit for a single shared state slice.
 *   The trade-off: Context re-renders all consumers when auth state changes,
 *   but auth state changes rarely (login/logout only), so this is acceptable.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import {
  AuthenticationDetails,
  CognitoUser,
  CognitoUserPool,
  type CognitoUserSession,
} from 'amazon-cognito-identity-js';
import { setAuthToken, setDevIdentity } from '../api/client';

// ── Configuration ─────────────────────────────────────────────────────────────

const COGNITO_USER_POOL_ID = import.meta.env.VITE_COGNITO_USER_POOL_ID as string;
const COGNITO_APP_CLIENT_ID = import.meta.env.VITE_COGNITO_APP_CLIENT_ID as string;

/**
 * True when Cognito is not configured (both env vars are empty).
 * Controls the local dev bypass path throughout this module.
 */
const IS_LOCAL_DEV = !COGNITO_USER_POOL_ID || !COGNITO_APP_CLIENT_ID;

/**
 * Cognito UserPool instance — created once at module load.
 * Only used in production mode. In local dev, this is null.
 *
 * CognitoUserPool wraps the User Pool ID and App Client ID into a single
 * object that all Cognito SDK calls require as context.
 */
const userPool: CognitoUserPool | null = IS_LOCAL_DEV
  ? null
  : new CognitoUserPool({
      UserPoolId: COGNITO_USER_POOL_ID,
      ClientId: COGNITO_APP_CLIENT_ID,
    });

// ── Types ─────────────────────────────────────────────────────────────────────

export interface AuthUser {
  userId: string;    // Cognito sub claim — authoritative identity (AD-23)
  email: string;     // for display only — never used for auth decisions
  isAdmin: boolean;  // from cognito:groups claim
}

interface AuthContextValue {
  /** True when the user is authenticated and ready to use the app. */
  isAuthenticated: boolean;
  /** True while auth state is being initialized (prevents flash of login page). */
  isLoading: boolean;
  /** The current user, or null if not authenticated. */
  user: AuthUser | null;
  /**
   * Authenticate with email and password.
   * Resolves on success. Throws Error with a user-facing message on failure.
   */
  login: (email: string, password: string) => Promise<void>;
  /** Clear auth state and tokens. In production, also signs out from Cognito. */
  logout: () => void;
}

// ── Context ───────────────────────────────────────────────────────────────────

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * useAuth — access authentication state and actions from any component.
 *
 * THROWS if called outside <AuthProvider>. This is intentional: components
 * that call useAuth() must be inside the provider tree. The error message
 * makes the misconfiguration immediately obvious during development.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within <AuthProvider>');
  }
  return ctx;
}

// ── Provider ──────────────────────────────────────────────────────────────────

interface AuthProviderProps {
  children: React.ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps): React.ReactElement {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [user, setUser] = useState<AuthUser | null>(null);

  // ── Initialization ──────────────────────────────────────────────────────────
  // On mount: either set up the dev bypass user (local dev) or check if there
  // is a current Cognito session (production — handles page refresh).
  useEffect(() => {
    if (IS_LOCAL_DEV) {
      // Local dev bypass: automatically authenticate as a synthetic dev user.
      // This eliminates the need to log in during development while Cognito
      // infrastructure is not yet deployed.
      const devUser: AuthUser = {
        userId: 'dev-user-001',
        email: 'dev@csvforge.local',
        isAdmin: false,
      };
      setDevIdentity(devUser.userId, devUser.email);
      setUser(devUser);
      setIsAuthenticated(true);
      setIsLoading(false);
      return;
    }

    // Production: check for an existing Cognito session.
    // getCognitoCurrentUser() reads the last authenticated user from the
    // Cognito SDK's local storage (it stores the username, not tokens).
    // If found, getSession() validates the session and refreshes if needed.
    const cognitoUser = userPool?.getCurrentUser();
    if (!cognitoUser) {
      setIsLoading(false);
      return;
    }

    cognitoUser.getSession(
      (err: Error | null, session: CognitoUserSession | null) => {
        if (err || !session?.isValid()) {
          // Session invalid or expired — user must log in again.
          setIsLoading(false);
          return;
        }
        // Valid session found — restore auth state from the session tokens.
        const accessToken = session.getAccessToken().getJwtToken();
        setAuthToken(accessToken);
        const authUser = extractUserFromSession(session);
        setUser(authUser);
        setIsAuthenticated(true);
        setIsLoading(false);
      },
    );
  }, []);

  // ── Login ───────────────────────────────────────────────────────────────────

  const login = useCallback(async (email: string, password: string): Promise<void> => {
    if (IS_LOCAL_DEV) {
      // Local dev: accept any credentials, set dev user.
      const devUser: AuthUser = {
        userId: 'dev-user-001',
        email: email || 'dev@csvforge.local',
        isAdmin: false,
      };
      setDevIdentity(devUser.userId, devUser.email);
      setUser(devUser);
      setIsAuthenticated(true);
      return;
    }

    if (!userPool) throw new Error('Cognito is not configured.');

    return new Promise((resolve, reject) => {
      const cognitoUser = new CognitoUser({
        Username: email,
        Pool: userPool,
      });

      const authDetails = new AuthenticationDetails({
        Username: email,
        Password: password,
      });

      cognitoUser.authenticateUser(authDetails, {
        onSuccess: (session: CognitoUserSession) => {
          const accessToken = session.getAccessToken().getJwtToken();
          setAuthToken(accessToken);
          const authUser = extractUserFromSession(session);
          setUser(authUser);
          setIsAuthenticated(true);
          resolve();
        },
        onFailure: (err: { message?: string; code?: string }) => {
          // Map Cognito error codes to user-friendly messages.
          const message = friendlyCognitoError(err.code, err.message);
          reject(new Error(message));
        },
        newPasswordRequired: () => {
          // The user must set a new password (temporary password from admin).
          // Out of MVP scope — show a clear message instead of a blank screen.
          reject(
            new Error(
              'Your account requires a password change. Please contact support.',
            ),
          );
        },
      });
    });
  }, []);

  // ── Logout ──────────────────────────────────────────────────────────────────

  const logout = useCallback((): void => {
    setAuthToken(null);
    setUser(null);
    setIsAuthenticated(false);

    if (!IS_LOCAL_DEV && userPool) {
      // Sign out from Cognito — invalidates refresh tokens on the server.
      const cognitoUser = userPool.getCurrentUser();
      cognitoUser?.signOut();
    }
  }, []);

  // ── Context value ───────────────────────────────────────────────────────────
  // useMemo prevents recreating the context value object on every render.
  // Without it, every render would create a new object reference, causing all
  // consumers to re-render even when the values haven't changed.
  const value = useMemo<AuthContextValue>(
    () => ({ isAuthenticated, isLoading, user, login, logout }),
    [isAuthenticated, isLoading, user, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Extract AuthUser fields from a Cognito session.
 *
 * Access token claims used:
 *   sub              → userId (authoritative, immutable UUID — AD-23)
 *   cognito:groups   → isAdmin (space-separated string of group names)
 *
 * ID token used for email only (email is NOT in the access token — AD-23).
 * The email is informational display only, never used for authorization.
 */
function extractUserFromSession(session: CognitoUserSession): AuthUser {
  const accessPayload = decodeJwtPayload(
    session.getAccessToken().getJwtToken(),
  );
  const idPayload = decodeJwtPayload(session.getIdToken().getJwtToken());

  // sub is the Cognito user UUID — authoritative identity.
  const userId = (accessPayload['sub'] as string) ?? '';

  // email comes from the ID token (not present in the access token).
  const email = (idPayload['email'] as string) ?? '';

  // cognito:groups is a space-separated string in the JWT claims.
  const groupsRaw = (accessPayload['cognito:groups'] as string) ?? '';
  const groups = groupsRaw ? groupsRaw.split(' ') : [];
  const isAdmin = groups.includes('csvforge-admins');

  return { userId, email, isAdmin };
}

/**
 * Decode the payload of a JWT without verifying the signature.
 *
 * SECURITY NOTE:
 *   This decodes WITHOUT signature verification. In the frontend, this is
 *   acceptable because:
 *   1. The token was just issued by Cognito during authentication — it is
 *      fresh and trustworthy for the purpose of reading user claims for display.
 *   2. The backend (API Gateway JWT Authoriser) performs full signature
 *      verification for every API call. The frontend's decoded claims are
 *      only used for UI display (show the user's email, determine if admin
 *      menu should appear). Actual authorization is enforced server-side.
 *   3. A malicious user can forge their own token payload here, but it will
 *      be rejected by API Gateway — there is no security bypass.
 *
 * WHY NOT verify in the frontend:
 *   Signature verification requires downloading Cognito's JWKS keys, which
 *   adds network overhead and complexity. API Gateway handles this correctly.
 *   Duplicating it in the browser provides no security benefit.
 */
function decodeJwtPayload(token: string): Record<string, unknown> {
  try {
    const payload = token.split('.')[1];
    // atob() decodes base64. JWT payload uses base64url encoding
    // (- instead of +, _ instead of /). Restore standard base64 first.
    const base64 = payload.replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(atob(base64)) as Record<string, unknown>;
  } catch {
    return {};
  }
}

/**
 * Convert a Cognito error code to a user-friendly message.
 *
 * Cognito error codes are documented at:
 * https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/
 */
function friendlyCognitoError(
  code: string | undefined,
  defaultMessage: string | undefined,
): string {
  switch (code) {
    case 'NotAuthorizedException':
      return 'Incorrect email or password. Please try again.';
    case 'UserNotFoundException':
      // Do not reveal whether the user exists — same message as wrong password.
      return 'Incorrect email or password. Please try again.';
    case 'UserNotConfirmedException':
      return 'Please verify your email address before logging in.';
    case 'PasswordResetRequiredException':
      return 'Your password must be reset. Please contact support.';
    case 'TooManyRequestsException':
      return 'Too many login attempts. Please wait a moment and try again.';
    case 'LimitExceededException':
      return 'Too many attempts. Please try again later.';
    case 'NetworkError':
    case 'NetworkingError':
      return 'Cannot connect to the authentication service. Please check your connection.';
    default:
      return defaultMessage || 'Authentication failed. Please try again.';
  }
}
