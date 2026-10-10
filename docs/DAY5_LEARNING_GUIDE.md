# Day 5 Learning Guide — React Frontend

This guide explains the complete frontend architecture built in Day 5.
After reading it, you should be able to explain every component, the authentication flow,
the upload wizard contract, the polling strategy, and every technical decision — without
looking at the code.

---

## 1. What Day 5 Built

| Component | File | What it does |
|---|---|---|
| Type definitions | `src/types/api.ts` | Mirrors backend Pydantic schemas as TypeScript interfaces |
| API client | `src/api/client.ts` | Centralized fetch wrapper with auth injection and error normalization |
| Auth context | `src/auth/AuthContext.tsx` | Cognito auth + local dev bypass, token storage, useAuth hook |
| Login page | `src/pages/LoginPage.tsx` | Email/password form calling AuthContext.login() |
| App layout | `src/components/AppLayout.tsx` | Top nav bar + Outlet for child routes |
| Route guard | `src/components/RequireAuth.tsx` | Redirects unauthenticated users to /login |
| Upload wizard | `src/pages/UploadPage.tsx` | Three-step flow: POST /jobs → PUT S3 → POST complete |
| Job list | `src/pages/JobListPage.tsx` | Lists user's jobs with 5s polling for active states |
| Job detail | `src/pages/JobDetailPage.tsx` | Shows status, results, and download for one job |
| Status badge | `src/components/JobStatusChip.tsx` | Colour-coded MUI Chip for each job status |
| Admin app | `src/admin/AdminApp.tsx` | React Admin panel at /admin for all jobs |
| App router | `src/App.tsx` | React Router v7 route tree with auth guards |
| Entry point | `src/main.tsx` | createRoot + StrictMode |

---

## 2. Dependency Decisions (verified October 2026)

The VERSIONS.md estimates were corrected after a live npm registry check:

| Package | Estimate | Actual | Reason |
|---|---|---|---|
| `react-admin` | 5.8.0 | **5.15.4** | Latest stable; 5.8.0 was an estimate |
| `react-router` | 8.2.0 | **7.1.1** | v8 incompatible with React Admin (requires `>=19.2.7`) |
| `@mui/material` | 9.0.0 | **9.5.0** | Latest 9.x; supported by react-admin 5.15.4 |
| `@vitejs/plugin-react` | 4.3.4 | **6.1.2** | v6 required for Vite 8 support |
| `typescript` | 6.0.0 | **6.0.3** | Latest 6.0 patch |
| `typescript-eslint` | 8.20.0 | **8.71.1** | Required for TS 6.0 support |

**Key finding:** React Admin 5.15.4 supports MUI v9 directly — no split MUI installation
needed, no `--legacy-peer-deps` required. See `docs/DECISIONS.md` AD-FE-01 through AD-FE-05.

---

## 3. Project Structure

```
frontend/
├── index.html                    # Vite entry HTML, Content-Security-Policy
├── vite.config.ts                # Build tool config (no test key in Vite 8)
├── vitest.config.ts              # Vitest config (separate from vite.config.ts — Vite 8 requirement)
├── tsconfig.json                 # References app + node configs
├── tsconfig.app.json             # App source TypeScript config
├── tsconfig.node.json            # vite.config.ts TypeScript config
├── eslint.config.js              # ESLint 9 flat config
├── .env.example                  # Documents all VITE_ environment variables
├── .env.local                    # Local dev values (gitignored)
└── src/
    ├── vite-env.d.ts             # Declares import.meta.env types (required for TS)
    ├── main.tsx                  # createRoot entry point
    ├── App.tsx                   # Theme + Router + AuthProvider
    ├── types/
    │   └── api.ts                # All API types mirroring backend schemas
    ├── api/
    │   └── client.ts             # Centralized API client
    ├── auth/
    │   └── AuthContext.tsx       # Cognito integration + useAuth hook
    ├── components/
    │   ├── AppLayout.tsx         # Navigation shell
    │   ├── RequireAuth.tsx       # Route guard
    │   └── JobStatusChip.tsx     # Status badge
    ├── pages/
    │   ├── LoginPage.tsx
    │   ├── JobListPage.tsx
    │   ├── JobDetailPage.tsx
    │   └── UploadPage.tsx
    ├── admin/
    │   └── AdminApp.tsx          # React Admin (mounted at /admin)
    └── test/
        ├── setup.ts              # @testing-library/jest-dom import
        ├── apiClient.test.ts
        ├── AuthContext.test.tsx
        └── JobStatusChip.test.tsx
```

---

## 4. API Client Architecture

The API client (`src/api/client.ts`) is the single communication channel between
the React frontend and the FastAPI backend. No component ever calls `fetch()` directly.

### Why a centralized client?

1. **Single base URL:** `VITE_API_URL` is read once. Changing the backend URL requires
   one change, not hunting through every component.
2. **Auth injection:** Bearer tokens and dev bypass headers are added in one function
   (`buildAuthHeaders()`), not in every component.
3. **Error normalization:** All failures become `ApiClientError` with a human-readable
   message. Components get a string they can display, not raw Response objects.
4. **Type safety:** Every function returns a typed Promise — no `any` leaking from the
   network boundary.

### Two authentication paths

```
IS_LOCAL_DEV (VITE_COGNITO_USER_POOL_ID is empty)
    │
    ├── YES → Send X-Dev-User-Id and X-Dev-User-Email headers
    │          Backend reads these when ENVIRONMENT=local
    │
    └── NO  → Send Authorization: Bearer <access_token>
               API Gateway JWT Authoriser validates the token
```

### S3 upload isolation (AD-FE-04)

The `uploadFileToS3()` function is deliberately separate from `apiRequest()`:

```
apiRequest()         → adds Authorization: Bearer token to all calls
uploadFileToS3()     → uses raw XMLHttpRequest, NO custom headers

Why: S3 presigned URL = self-contained AWS SigV4 signature in the query string.
     If you also send Authorization: Bearer, S3 returns:
     400 InvalidArgument: Only one auth mechanism allowed
```

XMLHttpRequest is used (not fetch) because XHR exposes `onprogress` events for the
upload body, which enables the progress bar. `fetch()` does not expose request body
upload progress.

---

## 5. Authentication Architecture

```
VITE_COGNITO_USER_POOL_ID empty?
        │
        ├── YES (local dev)
        │   AuthProvider.useEffect() → sets devUser immediately
        │   Login page never shown
        │   API calls use X-Dev-User-Id: dev-user-001
        │
        └── NO (production)
            User visits /login
            Email + password → amazon-cognito-identity-js.authenticateUser()
            Cognito → ID token + Access token + Refresh token
            Access token → setAuthToken() → stored in module memory (NOT localStorage)
            User info → extracted from JWT payload (no signature verification in browser)
            → isAuthenticated = true → navigate to /
```

### Token storage decision (AD-10)

Tokens are stored in **module-scope memory** in `client.ts`. They are lost on page
refresh — the user must log in again.

- **Why not localStorage?** localStorage is readable by any JavaScript on the page.
  An XSS vulnerability would allow theft of the access token. Module memory has no XSS
  risk: a malicious script would have to call the module's own functions to read it.

- **Trade-off:** No persistence across page refreshes. Acceptable for an MVP where
  the user is working actively with their files.

### JWT decoding without verification

The frontend decodes the JWT payload with `atob()` to extract `sub` (user ID) and
`email` (from ID token). This decoding does NOT verify the signature.

This is intentional and safe because:
1. The token was just issued by Cognito (fresh and trustworthy for display).
2. The backend (API Gateway JWT Authoriser) verifies the signature for every API call.
3. A malicious user can forge their own display claims — but those are rejected server-side.
4. Frontend decoding is for UI purposes only: show the user's email, check if the admin
   menu should appear. No authorization decision is made based on frontend-decoded claims.

---

## 6. Upload Wizard Flow

```
User selects file → client-side validation
         │
         ├── Not .csv → show error, stop
         ├── Empty file → show error, stop
         └── > 50 MB → show error, stop

Step 1: POST /jobs
        body: { user_email, original_filename, file_size_bytes }
        ← { job_id, upload_url, s3_key }
        (creates pending_upload record in DB)

Step 2: PUT <upload_url>
        XHR upload to S3 presigned URL
        No Authorization header (AD-FE-04)
        onprogress → update progress bar 0→100%
        (file goes directly from browser to S3)

Step 3: POST /uploads/{job_id}/complete
        (backend: HeadObject → verify size ≤ 50 MiB → status=queued → SQS)
        ← { job_id, status: "queued" }

Navigate to /jobs/{job_id}
```

### Duplicate submission prevention

The `uploadState.step` state machine prevents re-entry:
- Button is `disabled` while any step is active
- `isUploading` check guards the `onClick` handler
- Each step fails independently — a step 2 failure does not retry step 1

### S3 upload contract note

If step 3 (`POST /uploads/{id}/complete`) fails after step 2 succeeds, the file
is in S3 but the job stays in `pending_upload`. The user can retry the complete call,
but there is no automatic recovery. The `pending_upload` row accumulates silently.
A cleanup background job for stale `pending_upload` rows is out of MVP scope (AD-13).

---

## 7. Job Status Polling

Both `JobListPage` and `JobDetailPage` poll the API while jobs are active:

```
Initial load → GET /jobs (or GET /jobs/{id})
         │
         ├── Any job in queued/processing state?
         │       YES → setInterval(fetchJobs, 5000)  [silent — no spinner]
         │       NO  → no interval
         │
         └── On unmount → clearInterval()
```

**Why 5 seconds?** Fast enough to feel responsive (job takes 5–60s), slow enough not
to hammer the API. With 100 concurrent users all polling every 5s, that's 20 req/s —
well within Lambda's free tier.

**Why `useRef` for the interval ID?** The interval ID is infrastructure state, not
UI state. Storing it in `useState` would trigger re-renders on every start/stop.
`useRef` stores a mutable value that persists across renders without causing them.

**Why `useCallback` for `fetchJobs`?** Without `useCallback`, `fetchJobs` is recreated
on every render. The polling `useEffect` lists `fetchJobs` as a dependency — if the
function identity changes every render, the effect re-runs every render, creating an
infinite polling loop. `useCallback` memoizes the function so it's only recreated when
its dependencies change (none — it closes over only state setters, which are stable).

---

## 8. Download Flow

```
User clicks "Download processed CSV"
         │
GET /jobs/{jobId}/download
         ← { download_url, filename, expires_in_seconds }
         │
Create <a href={download_url} download={filename}>
Append to document.body (cross-browser requirement)
anchor.click()
Remove from document.body

→ Browser opens download manager, saves file as <filename>
```

**Why not `window.open(url)`?** Opens in a new tab, which the user must then close.
An anchor with `download` attribute triggers the download manager directly — better UX.

**Why append to DOM?** Firefox and Safari require the anchor to be in the document
before `.click()` fires the download. Chrome works without it, but appending is the
cross-browser safe approach.

**Presigned URL security:** The URL is a time-limited bearer token (valid 15 minutes).
Anyone with the URL can download the file. The frontend initiates the download
immediately — the 15-minute window is generous.

---

## 9. React Admin Panel

React Admin is mounted at `/admin` as a completely standalone component — not inside
the main `AppLayout`. This is intentional:

- React Admin creates its own internal router. Mounting it inside React Router's
  route tree creates duplicate router conflicts.
- The `/admin/*` route in App.tsx renders `AdminApp` directly, bypassing `AppLayout`.
- The admin URL is accessed via `<Button component="a" href="/admin">` (a full page
  navigation, not a SPA route change), which resets the browser context cleanly.

### ra-data-simple-rest contract

React Admin calls the backend with these query parameters:
```
GET /admin/jobs?_start=0&_end=25&_sort=created_at&_order=DESC&status=failed
```

The backend's `admin_list_jobs` endpoint already handles these parameters (implemented
in Day 4). The response includes a `Content-Range: jobs 0-24/150` header that React
Admin reads for pagination.

### Auth in React Admin

React Admin's `httpClient` calls `getAuthToken()` from `api/client.ts` to read the
in-memory access token. This is the same token set by `AuthContext.login()`. In local
dev mode, the X-Dev-Is-Admin header is included (backend grants admin access when
`ENVIRONMENT=local`).

---

## 10. MUI v9 Breaking Changes

Several MUI v9 API changes required fixes during implementation:

| Old (MUI v8) | New (MUI v9) | Why |
|---|---|---|
| `<Typography fontWeight={600}>` | `<Typography sx={{ fontWeight: 600 }}>` | Non-semantic CSS props removed from component API |
| `<Typography display="block">` | `<Typography sx={{ display: 'block' }}>` | Same — layout props moved to sx |
| `<Stack flexWrap="wrap">` | `<Stack sx={{ flexWrap: 'wrap' }}>` | Same |
| `<TextField inputProps={{ ... }}>` | `<TextField slotProps={{ htmlInput: { ... } }}>` | v9 uses "slots" pattern for component composition |
| `@mui/icons-material/CheckCircleOutline` | `@mui/icons-material/CheckCircleOutlined` | Icon name correction |
| Chip as `role="button"` | Chip as `<div>` with `aria-label` | Chip without onClick renders as div in v9 |

The `sx` prop change is a deliberate MUI v9 design decision: component props should be
semantic (what a component IS), not presentational (how it LOOKS). CSS-style properties
belong in `sx`. This makes components more composable and the API more predictable.

---

## 11. Frontend Testing

**28 tests across 3 files**, all passing.

| File | Tests | What's covered |
|---|---|---|
| `JobStatusChip.test.tsx` | 12 | Correct label per status, aria-label, size prop |
| `apiClient.test.ts` | 11 | ApiClientError class, HTTP error normalization, network failure |
| `AuthContext.test.tsx` | 5 | Local dev auto-auth, login, logout, useAuth outside provider |

**Testing strategy:**
- We test behaviour the user sees (rendered text, aria-labels), not implementation
  details (CSS classes, internal state).
- `fetch()` is mocked per-test with `vi.spyOn`, not globally. Per-test mocks prevent
  test pollution and make each test's intent explicit.
- The Cognito path is NOT tested (it would require mocking amazon-cognito-identity-js
  HTTP calls). This is the correct trade-off: the Cognito path is verified manually
  after AWS infrastructure deployment.

### Known test limitations

- No integration tests against a real backend.
- No E2E tests (Playwright/Cypress) for the full upload → process → download workflow.
- Cognito authentication path is untested (requires deployed infrastructure).
- These gaps are documented in PROGRESS.md and are acceptable for the pre-deployment phase.

---

## 12. Interview Questions

**Q: Why does the upload go directly to S3 rather than through the FastAPI backend?**

API Gateway HTTP API has a 10 MB payload limit. Routing 50 MB through Lambda wastes
memory, execution time, and API Gateway bandwidth. The presigned URL approach offloads
storage to S3's HTTP infrastructure. The backend only touches the file's metadata via
HeadObject — it never sees the bytes.

**Q: What is the purpose of the `POST /uploads/{id}/complete` call after the S3 PUT?**

S3 does not notify the API when a presigned PUT succeeds. The browser must call the
complete endpoint to confirm the file is there. The backend uses this call to: (1)
verify the object exists via HeadObject, (2) verify size ≤ 50 MiB (server-side
enforcement, AD-27), (3) transition the job to `queued`, and (4) publish to SQS.

**Q: Why is the access token stored in module memory instead of localStorage?**

localStorage is readable by any JavaScript on the page — an XSS vulnerability would
allow token theft. Module memory is not directly accessible from the global scope. The
trade-off is session loss on page refresh, which is acceptable per AD-10.

**Q: What happens if the user closes the browser tab mid-upload?**

If the tab is closed after step 1 (POST /jobs) but before step 3 (POST complete), a
`pending_upload` row exists in the database with no corresponding SQS message. The job
is never processed. Stale `pending_upload` rows accumulate silently — they are excluded
from the user's job list (AD-13) and cause no data corruption. A cleanup job is out of
MVP scope.

**Q: Why is React Admin mounted outside the main React Router tree?**

React Admin creates its own router internally. Two routers in the same app tree compete
for URL control. By rendering AdminApp at `/admin/*` and navigating to it with a full
page href (not a SPA Link), the browser creates a clean new page context with only
React Admin's router active.

**Q: Why does `useCallback` prevent an infinite polling loop?**

`fetchJobs` is declared inside the component, so without `useCallback` it's a new
function object on every render. The polling `useEffect` depends on `fetchJobs` — if
the dependency changes every render, the effect re-runs every render, which sets a new
interval, which may trigger state changes, which cause another render... an infinite
loop. `useCallback` stabilizes the function identity so the effect only runs when
genuinely needed.

---

## 13. Team Walkthrough

"Today we shipped the complete CSVForge frontend. Here's the user journey in 60 seconds:

The user lands on the app and, if Cognito is configured, signs in with their email and
password. Their Cognito access token is stored in memory — never in localStorage.

On the home page they see all their jobs in a table. If any jobs are running, the page
automatically refreshes every 5 seconds — a small spinner shows it's live.

They hit 'Upload CSV', drag a file into the drop zone or click to browse, then click
'Upload & Process'. A three-step wizard runs: the API creates a DB record and returns
a presigned S3 URL, the file goes directly from the browser to S3 (no API Gateway
involved), then the API confirms the upload, verifies the size, and queues the job.

The user is sent to the job detail page where they watch the status update from queued
→ processing → completed. When it's done, they click 'Download processed CSV' — the
browser downloads the cleaned file directly from S3.

Admins access `/admin` to see all jobs across all users, filter by status, and
investigate failures.

The build is clean: TypeScript, ESLint, and Vite all pass. 28 frontend tests cover
the core client and auth logic. The backend's 197 tests are unchanged."
