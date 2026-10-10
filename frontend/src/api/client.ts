/**
 * CSVForge — centralized API client.
 *
 * WHAT:
 *   A single module that handles all HTTP communication with the FastAPI backend.
 *   All components import from here — no component ever constructs a fetch() call
 *   directly or hardcodes the API base URL.
 *
 * WHY A CENTRALIZED CLIENT:
 *   1. Single base URL: change VITE_API_URL once, all calls update.
 *   2. Auth injection: the Bearer token is added in one place, not every component.
 *   3. Error normalization: all errors are converted to an ApiClientError with a
 *      human-readable message. Components get a string, not a raw Response object.
 *   4. Type safety: every function returns a typed Promise, not Promise<any>.
 *   5. S3 upload isolation: the PUT to the presigned URL is separate from the
 *      API calls. S3 must not receive the Authorization header (AD-FE-04).
 *
 * LOCAL DEV BYPASS (AD-FE-03):
 *   When VITE_COGNITO_USER_POOL_ID is empty, the client includes
 *   X-Dev-User-Id and X-Dev-User-Email headers instead of Bearer tokens.
 *   The backend (ENVIRONMENT=local) reads these headers as the user identity.
 *   The bypass is transparent to components — they call the same functions.
 *
 * ERROR HANDLING CONTRACT:
 *   All functions throw ApiClientError on failure.
 *   Components catch it and display error.message to the user.
 *   Internal details (status codes, raw responses) are available on the error
 *   object for logging but are never shown directly to users.
 *
 * ARCHITECTURE:
 *   Component
 *     └── api/client.ts (this file)
 *           ├── Prepares headers (Bearer token OR dev bypass headers)
 *           ├── Calls fetch() to FastAPI backend
 *           ├── Parses response JSON
 *           └── Returns typed result OR throws ApiClientError
 *
 *   S3 Upload (special case):
 *     Component
 *       └── api/client.ts uploadFileToS3()
 *             └── XMLHttpRequest PUT to presigned URL (NO Authorization header)
 *                   └── Reports progress via onProgress callback
 */

import type {
  CreateJobRequest,
  CreateJobResponse,
  Job,
  JobDownloadResponse,
  JobListResponse,
  UploadCompleteResponse,
} from '../types/api';

// ── Configuration ─────────────────────────────────────────────────────────────

/**
 * Base URL for the FastAPI backend.
 * Configured via VITE_API_URL environment variable (no trailing slash).
 * Local dev default: http://localhost:8000
 */
const API_BASE_URL = import.meta.env.VITE_API_URL as string;

/**
 * Whether Cognito is configured.
 * Empty VITE_COGNITO_USER_POOL_ID → local dev bypass mode.
 * Non-empty → production Cognito mode.
 */
const IS_LOCAL_DEV = !import.meta.env.VITE_COGNITO_USER_POOL_ID;

// ── Error type ────────────────────────────────────────────────────────────────

/**
 * All API failures are represented as ApiClientError.
 *
 * WHAT:
 *   A typed Error subclass that carries a human-readable message (shown to users),
 *   the HTTP status code (for conditional handling), and the raw API detail
 *   (for logging — never shown directly to users).
 *
 * WHY A CUSTOM ERROR CLASS (not just throw new Error("...")):
 *   Components often need to make decisions based on the status code:
 *     - 401 → redirect to login
 *     - 404 → show "not found" message
 *     - 409 → show "already in progress" message
 *   A plain Error has no status code. ApiClientError carries status so
 *   callers can distinguish these cases without string-matching the message.
 */
export class ApiClientError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly detail?: string,
  ) {
    super(message);
    this.name = 'ApiClientError';
  }
}

// ── Auth state accessor ───────────────────────────────────────────────────────

/**
 * Token storage for the API client.
 *
 * WHAT:
 *   The AuthContext calls setAuthToken() after a successful Cognito login.
 *   The API client reads this token when building request headers.
 *   Tokens are held in module-scope memory (not localStorage) per AD-10.
 *
 * WHY MODULE SCOPE (not passed as a parameter):
 *   Every API call function would need to accept a token parameter, which
 *   every component would need to pass. Module-scope state eliminates this
 *   parameter plumbing. The AuthContext is the only writer; the client is
 *   the only reader — no other code touches this variable.
 *
 * SECURITY (AD-10):
 *   In-memory storage has no XSS risk. localStorage and sessionStorage
 *   are accessible to any JavaScript on the page. If an XSS payload
 *   executes, it cannot steal tokens from module memory (it would have to
 *   call this module's functions, not just read storage).
 *   Trade-off: tokens are lost on page refresh. The user must log in again.
 */
let _accessToken: string | null = null;
let _devUserId: string = 'dev-user-001';
let _devUserEmail: string = 'dev@csvforge.local';

/** Called by AuthContext after successful Cognito login. */
export function setAuthToken(token: string | null): void {
  _accessToken = token;
}

/** Read the current access token (used by AdminApp httpClient). */
export function getAuthToken(): string | null {
  return _accessToken;
}

/** Called by AuthContext in local dev mode to set the dev identity. */
export function setDevIdentity(userId: string, email: string): void {
  _devUserId = userId;
  _devUserEmail = email;
}

// ── Header builder ────────────────────────────────────────────────────────────

/**
 * Build authentication headers for API requests.
 *
 * PRODUCTION PATH:
 *   Authorization: Bearer <access_token>
 *   The backend (via API Gateway JWT Authoriser) validates this token.
 *
 * LOCAL DEV PATH (IS_LOCAL_DEV = true):
 *   X-Dev-User-Id: dev-user-001
 *   X-Dev-User-Email: dev@csvforge.local
 *   The backend reads these when ENVIRONMENT=local (AD-FE-03).
 *
 * THROWS if in production mode and no token is available.
 * This should not happen — components should only call API functions
 * when the user is authenticated (AuthContext ensures this).
 */
function buildAuthHeaders(): Record<string, string> {
  if (IS_LOCAL_DEV) {
    return {
      'X-Dev-User-Id': _devUserId,
      'X-Dev-User-Email': _devUserEmail,
    };
  }

  if (!_accessToken) {
    throw new ApiClientError('Not authenticated. Please log in.', 401);
  }

  return {
    Authorization: `Bearer ${_accessToken}`,
  };
}

// ── Core fetch wrapper ────────────────────────────────────────────────────────

/**
 * Make an authenticated JSON request to the FastAPI backend.
 *
 * WHAT:
 *   Wraps fetch() with authentication headers, JSON serialization, and
 *   error normalization. All API functions use this internally.
 *
 * ERROR HANDLING:
 *   - Network failure (no response): throws ApiClientError with status 0.
 *   - 4xx/5xx response: reads the {"detail": ...} body, throws ApiClientError
 *     with a user-friendly message derived from the detail.
 *   - Successful response: returns the parsed JSON as T.
 *
 * WHY NOT axios:
 *   The native fetch API is sufficient for this use case. Adding axios would
 *   add 13KB+ to the bundle for no functional benefit. Learning fetch directly
 *   is also more educational — axios is just a wrapper around XHR/fetch anyway.
 */
async function apiRequest<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  let response: Response;

  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        ...buildAuthHeaders(),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    // fetch() itself threw — no network response at all.
    // This happens when the server is down or unreachable.
    throw new ApiClientError(
      'Cannot connect to the server. Please check your connection.',
      0,
    );
  }

  if (!response.ok) {
    // Server returned 4xx or 5xx — parse the FastAPI error body.
    let errorDetail = `HTTP ${response.status}`;
    try {
      const errorBody = await response.json() as { detail?: unknown };
      if (typeof errorBody.detail === 'string') {
        errorDetail = errorBody.detail;
      } else if (Array.isArray(errorBody.detail)) {
        // Pydantic validation error: [{loc, msg, type}]
        errorDetail = errorBody.detail
          .map((e: { msg?: string }) => e.msg ?? JSON.stringify(e))
          .join('; ');
      }
    } catch {
      // Could not parse error body — use status text.
      errorDetail = response.statusText || errorDetail;
    }

    const userMessage = friendlyErrorMessage(response.status, errorDetail);
    throw new ApiClientError(userMessage, response.status, errorDetail);
  }

  // Parse successful response.
  // Handle 204 No Content (no body to parse).
  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

/**
 * Convert a status code + raw detail into a user-friendly message.
 *
 * PRINCIPLE: Users should see clear, actionable messages — not HTTP codes.
 * Internal details (raw detail string) are preserved on the error object
 * for logging but not shown in the UI.
 */
function friendlyErrorMessage(status: number, detail: string): string {
  switch (status) {
    case 401:
      return 'Your session has expired. Please log in again.';
    case 403:
      return 'You do not have permission to access this resource.';
    case 404:
      return 'The requested resource was not found.';
    case 409:
      return detail || 'This operation cannot be completed in the current state.';
    case 413:
      return 'The file is too large to process.';
    case 422:
      return detail || 'The request was invalid. Please check your input.';
    case 429:
      return 'Too many requests. Please wait a moment and try again.';
    case 500:
    case 502:
    case 503:
      return 'A server error occurred. Please try again later.';
    default:
      return detail || `An error occurred (${status}).`;
  }
}

// ── Job API ───────────────────────────────────────────────────────────────────

/**
 * POST /jobs — create a new upload job and get a presigned S3 PUT URL.
 *
 * WHAT:
 *   Step 1 of the upload wizard. Creates a Job record in the database (status:
 *   pending_upload) and returns the presigned URL for direct S3 upload.
 *
 * Returns 201 with {job_id, upload_url, s3_key}.
 */
export async function createJob(
  userEmail: string,
  originalFilename: string,
  fileSizeBytes: number,
): Promise<CreateJobResponse> {
  const body: CreateJobRequest = {
    user_email: userEmail,
    original_filename: originalFilename,
    file_size_bytes: fileSizeBytes,
  };
  return apiRequest<CreateJobResponse>('POST', '/jobs', body);
}

/**
 * POST /uploads/{jobId}/complete — confirm S3 upload and enqueue processing.
 *
 * WHAT:
 *   Step 3 of the upload wizard (after the S3 PUT succeeds). Tells the backend
 *   that the file is in S3. The backend verifies it via HeadObject, transitions
 *   the job to 'queued', and publishes an SQS message.
 *
 * Returns {job_id, status: "queued"}.
 */
export async function confirmUpload(
  jobId: string,
): Promise<UploadCompleteResponse> {
  return apiRequest<UploadCompleteResponse>(
    'POST',
    `/uploads/${jobId}/complete`,
  );
}

/**
 * GET /jobs — list all jobs for the authenticated user.
 *
 * Returns {jobs: Job[], total: number}.
 */
export async function listJobs(): Promise<JobListResponse> {
  return apiRequest<JobListResponse>('GET', '/jobs');
}

/**
 * GET /jobs/{jobId} — get the current state of one job.
 *
 * Returns the Job or throws ApiClientError(404) if not found or not owned.
 */
export async function getJob(jobId: string): Promise<Job> {
  return apiRequest<Job>('GET', `/jobs/${jobId}`);
}

/**
 * GET /jobs/{jobId}/download — get a presigned S3 download URL.
 *
 * WHAT:
 *   Only valid when job.status === 'completed'. Returns a presigned GET URL
 *   valid for 15 minutes. The frontend opens this URL in a new tab or triggers
 *   an anchor download — the file is served directly from S3.
 *
 * Returns {job_id, download_url, filename, expires_in_seconds}.
 */
export async function getDownloadUrl(
  jobId: string,
): Promise<JobDownloadResponse> {
  return apiRequest<JobDownloadResponse>('GET', `/jobs/${jobId}/download`);
}

// ── S3 direct upload ──────────────────────────────────────────────────────────

/**
 * Upload a file directly to S3 using a presigned PUT URL.
 *
 * WHAT:
 *   Step 2 of the upload wizard. The browser sends the file bytes directly to
 *   S3 — the file never passes through the FastAPI backend (AD-02, AD-FE-04).
 *
 * WHY XMLHttpRequest (not fetch):
 *   XMLHttpRequest supports `onprogress` events, which report how many bytes
 *   have been uploaded. fetch() does not expose upload progress (ReadableStream
 *   only covers the response body, not the request body). For a meaningful
 *   progress bar, XHR is necessary.
 *
 * WHY NO Authorization HEADER:
 *   The presigned URL carries its own AWS SigV4 signature in query string
 *   parameters. S3 validates this signature against the signing key. If the
 *   request also includes an Authorization header (our Cognito Bearer token),
 *   S3 receives two authentication mechanisms and returns:
 *     400 InvalidArgument: Only one auth mechanism allowed
 *   The XHR here intentionally sends NO custom headers (AD-FE-04).
 *
 * @param presignedUrl - The S3 presigned PUT URL from createJob().
 * @param file         - The File object from the file input.
 * @param onProgress   - Callback with progress 0–100. Called on XHR progress events.
 * @returns            - Resolves on successful upload (S3 returns 200).
 * @throws             - Rejects with ApiClientError on failure.
 */
export function uploadFileToS3(
  presignedUrl: string,
  file: File,
  onProgress?: (percent: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();

    // Track upload progress (fires as bytes are sent over the network).
    if (onProgress) {
      xhr.upload.onprogress = (event: ProgressEvent) => {
        if (event.lengthComputable) {
          const percent = Math.round((event.loaded / event.total) * 100);
          onProgress(percent);
        }
      };
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(100);
        resolve();
      } else {
        // S3 returns XML error bodies — extract a readable message.
        const xmlMessage = extractS3ErrorMessage(xhr.responseText);
        reject(
          new ApiClientError(
            xmlMessage || `File upload failed (S3 returned ${xhr.status}).`,
            xhr.status,
          ),
        );
      }
    };

    xhr.onerror = () => {
      reject(
        new ApiClientError(
          'File upload failed. Please check your connection and try again.',
          0,
        ),
      );
    };

    xhr.ontimeout = () => {
      reject(
        new ApiClientError(
          'File upload timed out. The file may be too large or your connection is slow.',
          0,
        ),
      );
    };

    // PUT the file bytes directly to the presigned URL.
    // Content-Type: text/csv tells S3 what MIME type to store.
    // No Authorization header — the presigned URL signature is in the query string.
    xhr.open('PUT', presignedUrl);
    xhr.setRequestHeader('Content-Type', 'text/csv');
    xhr.send(file);
  });
}

/**
 * Extract the human-readable message from an S3 XML error response.
 * S3 returns errors as XML: <Error><Code>...</Code><Message>...</Message></Error>
 */
function extractS3ErrorMessage(xmlText: string): string {
  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(xmlText, 'text/xml');
    const message = doc.querySelector('Message')?.textContent;
    const code = doc.querySelector('Code')?.textContent;
    if (message) return code ? `${code}: ${message}` : message;
  } catch {
    // DOMParser failed — return empty string, caller uses fallback.
  }
  return '';
}
