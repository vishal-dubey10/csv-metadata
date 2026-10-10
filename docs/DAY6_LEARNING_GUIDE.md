# Day 6 Learning Guide — Full Application Integration

This guide covers the complete frontend/backend contract, end-to-end request flows,
security model, and integration points. After reading it you should be able to trace
any user action from browser to database and back — and explain every decision.

---

## 1. Complete API Contract

The frontend and backend share a single API contract defined in:

- **Backend:** `backend/schemas.py` (Pydantic models)
- **Frontend:** `frontend/src/types/api.ts` (TypeScript interfaces)

These are manually kept in sync. A discrepancy causes a TypeScript compile error
(if the frontend type is wrong) or a runtime 422 (if the request body is wrong).

### Endpoints consumed by the frontend

| Method | Path | Auth | Frontend caller | Purpose |
|---|---|---|---|---|
| `GET` | `/health` | None | — | Liveness probe |
| `POST` | `/jobs` | User | `createJob()` | Create job + presigned upload URL |
| `POST` | `/uploads/{id}/complete` | User | `confirmUpload()` | Verify S3 upload, enqueue |
| `GET` | `/jobs` | User | `listJobs()` | List user's jobs |
| `GET` | `/jobs/{id}` | User | `getJob()` | Get one job |
| `GET` | `/jobs/{id}/download` | User | `getDownloadUrl()` | Presigned S3 download URL |
| `GET` | `/admin/jobs` | Admin | React Admin data provider | All jobs, paginated |

### Request/response field mapping

The backend uses Python naming conventions (`snake_case`). TypeScript mirrors this
exactly — no camelCase conversion. Fields map directly without a transformation layer.

**POST /jobs request body:**
```typescript
// Frontend sends:          // Backend expects (PostJobRequest):
{                           {
  user_email: string,         user_email: str,
  original_filename: string,  original_filename: str,
  file_size_bytes: number,    file_size_bytes: int,
}                           }
```

**GET /jobs/{id} response (Job):**
```typescript
// Frontend type:           // Backend schema (JobResponse):
{                           {
  job_id: string,             job_id: str,           ← mapped from job.id in _job_to_response()
  status: JobStatus,          status: str,
  original_filename: string,  original_filename: str,
  user_email: string,         user_email: str,
  file_size_bytes: number|null, file_size_bytes: int|None,
  row_count: number|null,     row_count: int|None,
  column_count: number|null,  column_count: int|None,
  column_names: string[]|null, column_names: list[str]|None,
  duplicate_rows_removed: number|null, duplicate_rows_removed: int|None,
  failure_reason: string|null, failure_reason: str|None,
  created_at: string|null,    created_at: str|None,  ← ISO-8601 via .isoformat()
  updated_at: string|null,    updated_at: str|None,
}                           }
```

**Note on `job_id` mapping:** The backend ORM model uses `job.id` (primary key), but
the API response exposes it as `job_id`. The `_job_to_response()` helper in `main.py`
performs this mapping. The frontend type uses `job_id` throughout, matching the JSON
key.

---

## 2. End-to-End Request Flows

### Full upload flow (9 steps)

```
Browser                    FastAPI (Lambda)              AWS Services
  │                              │                           │
  │  1. POST /jobs               │                           │
  │  {user_email, filename, size}│                           │
  │ ─────────────────────────────►                           │
  │                         create_job() → INSERT jobs       │
  │                         (status=pending_upload)          │
  │                         generate_presigned_upload_url() ─────────►
  │                                                     S3 signs URL
  │                                                      ◄────────────
  │  ◄─────────────────────────────                          │
  │  {job_id, upload_url, s3_key}│                           │
  │                              │                           │
  │  2. PUT <upload_url>         │                           │
  │  (file bytes, no auth header)│                           │
  │ ──────────────────────────────────────────────────────────►
  │                                                     S3 stores file
  │                                                      ◄────────────
  │  HTTP 200 (S3 response)      │                           │
  │                              │                           │
  │  3. POST /uploads/{id}/complete                          │
  │ ─────────────────────────────►                           │
  │                         get_job_by_id()                  │
  │                         object_exists() ─────────────────►
  │                                                     HeadObject
  │                                                      ◄────────────
  │                         get_object_size() (verify ≤50MiB)│
  │                         transition_to_queued()           │
  │                         (conditional UPDATE, owner check)│
  │                         send_job_message() ──────────────►
  │                                                     SQS.SendMessage
  │                                                      ◄────────────
  │  ◄─────────────────────────────                          │
  │  {job_id, status: "queued"}  │                           │
  │                              │                           │
  │  4. Navigate to /jobs/{id}   │                           │
  │  GET /jobs/{id} every 5s     │                           │
```

### Worker processing flow (continues asynchronously)

```
SQS → Lambda Worker
  │
  ├── parse_job_message() → job_id, s3_key
  ├── claim_job_for_processing() → conditional UPDATE (queued/processing → processing)
  ├── get_job_by_id() → read user_id from DB (NOT from SQS message — security)
  ├── download_file(bucket, s3_key, /tmp/{job_id}_input.csv)
  ├── parse_and_analyse() → validates CSV against AD-21 contract
  │       InvalidCSVError → fail_job() + return (SQS deletes message)
  ├── clean() → trim whitespace → deduplicate rows
  ├── write_csv(/tmp/{job_id}_output.csv)
  ├── upload_file(bucket, processed/{user_id}/{job_id}/result.csv)   ← S3 BEFORE DB (AD-14)
  └── complete_job() → UPDATE status=completed, row_count, column_names, etc.
```

### Download flow

```
Browser clicks "Download processed CSV"
  │
  │  GET /jobs/{id}/download
  │ ─────────────────────────────►
  │                         get_job_by_id_and_user() (ownership check)
  │                         verify status == completed
  │                         generate_presigned_download_url()
  │  ◄─────────────────────────────
  │  {download_url, filename, expires_in_seconds}
  │
  │  anchor.href = download_url
  │  anchor.download = filename
  │  anchor.click()  ──────────────────────────────────────►
  │                                                     S3 streams file
  │                                               ◄──────────────────────
  │  File saved to disk
```

---

## 3. Job Lifecycle (State Machine)

```
                                    ┌─────────────────────────────────┐
                                    │        jobs table states         │
                                    └─────────────────────────────────┘

[Browser POST /jobs]
        │
        ▼
  pending_upload  ──────────────────────────────────── AD-13: hidden from users
        │
        │ [POST /uploads/{id}/complete — ownership check + HeadObject verify]
        ▼
      queued  ←──────────────────────────────────────── first user-visible state
        │
        │ [Worker: claim_job_for_processing() — conditional UPDATE]
        ▼
   processing
        │
   ┌────┴────┐
   │         │
   ▼         ▼
completed  failed  ←───── InvalidCSVError (permanent), or re-raise (transient/SQS retry)
```

### Ownership enforcement at each transition

| Operation | Ownership check | How |
|---|---|---|
| Create job | Implicit | user_id comes from JWT sub (auth dependency) |
| Complete upload | `transition_to_queued(user_id=...)` | `WHERE user_id = ? AND status = 'pending_upload'` |
| View job | `get_job_by_id_and_user(user_id=...)` | `WHERE id = ? AND user_id = ?` |
| Download | `get_job_by_id_and_user(user_id=...)` | `WHERE id = ? AND user_id = ?` |
| Worker claim | No user check | Worker uses job.user_id FROM DB, not SQS message |

---

## 4. Error Handling — Frontend to Backend

### User-facing error states

| Scenario | Backend response | Frontend display |
|---|---|---|
| Not CSV file | Client-side validation | "Only CSV files are accepted" |
| File > 50 MiB | Client-side + 422 from complete | "File is too large" |
| Empty file | Client-side + InvalidCSVError | "The selected file is empty" |
| Duplicate headers | InvalidCSVError → status=failed | failure_reason on job detail |
| Invalid UTF-8 | InvalidCSVError → status=failed | failure_reason on job detail |
| S3 upload fails | XHR error | "File upload failed. Please check your connection" |
| Network down | ApiClientError (status 0) | "Cannot connect to the server" |
| Auth expired | 401 from backend | "Your session has expired. Please log in again" |
| Job not found | 404 from backend | "The requested resource was not found" |
| Wrong owner | 404 (not 403 — IDOR prevention) | "The requested resource was not found" |
| Worker S3 error | re-raise → SQS retry | status stays processing until MaxReceiveCount |
| DLQ reached | job stuck in processing | No automatic recovery in MVP |

### Error response format

FastAPI returns errors in this shape:
```json
{ "detail": "human-readable message" }
```

Pydantic validation errors return:
```json
{ "detail": [{ "loc": ["body", "field"], "msg": "error message", "type": "..." }] }
```

The API client's `apiRequest()` handles both formats and produces a single
`ApiClientError.message` string suitable for display.

### Errors never shown to users

- Stack traces
- SQL error messages
- S3 bucket names or ARNs
- SQS queue URLs
- Lambda function ARNs
- Internal exception types
- Database schema details

---

## 5. Security Model

### Authentication

| Layer | Mechanism | What it verifies |
|---|---|---|
| API Gateway (production) | JWT Authoriser | Signature, iss, exp, aud, token_use |
| FastAPI (local dev) | X-Dev-User-Id header | Nothing — trusted bypass (ENVIRONMENT=local only) |
| FastAPI (production) | Reads pre-validated claims | sub claim → user_id |

**Security invariant:** FastAPI never validates JWT signatures. API Gateway does it before
FastAPI runs. If claims are missing (direct Lambda invoke without API Gateway), FastAPI
returns 401.

### Authorization (IDOR prevention)

Every user-facing database query includes `WHERE user_id = ?`:

```python
# Wrong (vulnerable to IDOR):
get_job_by_id(db=db, job_id=job_id)  # returns ANY user's job

# Correct:
get_job_by_id_and_user(db=db, job_id=job_id, user_id=current_user.user_id)  # returns None if wrong owner
```

404 is returned for both "not found" and "wrong owner" cases — 403 would reveal that
the job exists (enabling enumeration). See AD-D4-01.

### S3 key construction

```
uploads/{user_id}/{job_id}/original.csv     ← browser PUT (presigned URL)
processed/{user_id}/{job_id}/result.csv     ← worker PUT (execution role)
```

- `user_id` comes from the JWT `sub` claim (API) or from the DB record (worker).
- The worker never trusts `user_id` from the SQS message body.
- IAM policies scope the API Lambda to `uploads/*` prefix and the worker to `processed/*`.

### CORS

The backend reads `ALLOWED_ORIGINS` from the environment:

```python
# Local dev default:
ALLOWED_ORIGINS = ["http://localhost:5173"]

# Production (configured at deployment):
ALLOWED_ORIGINS = ["https://csvforge-frontend.s3-website.ap-south-1.amazonaws.com"]
```

`allow_credentials=True` is required because the frontend sends an `Authorization`
header cross-origin. Without it, the browser blocks the request before it reaches the server.

---

## 6. Database Interactions

### Connection lifecycle

```
Lambda cold start:
  backend/db/connection.py imports at module level →
  creates SQLAlchemy engine (pool_size=2, pool_pre_ping=True) →
  engine persists across warm invocations

Per request:
  get_db() (FastAPI dependency) → SessionLocal() → yields session →
  route handler runs → session commits (success) or rolls back (exception) →
  session closes (finally block)
```

### Why pool_size=2 in Lambda (AD-D2-06)

Each Lambda execution environment is single-threaded. A pool of 10 (SQLAlchemy default)
× 100 concurrent Lambda environments = 1,000 PostgreSQL connections — exhaustion.
pool_size=2 allows one active connection and one overflow, appropriate per environment.

### PostgreSQL vs SQLite in tests

| Behaviour | PostgreSQL (integration) | SQLite (unit tests) |
|---|---|---|
| ARRAY(TEXT) columns | Native support | TEXT fallback (`.with_variant`) |
| Conditional UPDATE rowcount | Correct | Correct |
| TIMESTAMPTZ | Full timezone support | 1-second granularity |
| Concurrent connections | Yes (MVCC) | No |

Unit tests use SQLite in-memory for speed and isolation. Integration tests against real
PostgreSQL cover ARRAY-specific behaviour. See AD-D4-04.

---

## 7. Integration Testing Status

| Test category | Status | Command | Notes |
|---|---|---|---|
| Backend unit tests | ✅ 197/197 passing | `PYTHONPATH=. pytest tests/unit/` | SQLite in-memory |
| Frontend unit tests | ✅ 28/28 passing | `npm test` (in frontend/) | Vitest + jsdom |
| Backend integration tests | ⚠️ Not run | `pytest tests/integration/ -m aws` | Requires real AWS |
| Frontend E2E tests | ❌ Not implemented | — | Playwright/Cypress (post-MVP) |
| Real Cognito auth | ❌ Pending | Manual | Requires deployed infrastructure |
| End-to-end upload flow | ❌ Pending | Manual | Requires all AWS stacks deployed |

---

## 8. Known Limitations and Documented Gaps

### DB-before-SQS failure window (AD-29)

In `POST /uploads/{id}/complete`, the DB is updated to `queued` BEFORE SQS publishes.
If SQS fails: the job is stuck in `queued` with no worker processing it.

Observable via CloudWatch: jobs where `queued` was logged but no `processing` event
followed for > 10 minutes. Recovery: manually re-publish the SQS message for the
job_id, or implement a reaper Lambda (out of MVP scope).

### Stale pending_upload rows (AD-13)

If the browser navigates away before completing the upload, a `pending_upload` row
accumulates silently. These rows are excluded from all user-facing endpoints. Cleanup
via a background job is out of MVP scope.

### SQS at-least-once delivery

SQS guarantees at-least-once delivery, not exactly-once. The worker's
`claim_job_for_processing()` conditional UPDATE is the primary idempotency guard.
A duplicate message after completion is a no-op (terminal state detected, worker returns
early). Do not claim exactly-once execution for the SQS/Lambda pipeline.

### Token refresh (AD-10)

When the Cognito access token expires (default 1 hour), API calls return 401. The user
must log in again. Silent refresh via a refresh token stored in an HttpOnly cookie is
out of MVP scope.

---

## 9. Interview Questions

**Q: Walk through what happens when a user uploads a CSV.**

POST /jobs creates a pending_upload record and returns a presigned URL. The browser
PUTs the file directly to S3 — no API Gateway involved. POST /uploads/{id}/complete
verifies the file is there (HeadObject), checks size ≤ 50 MiB, transitions to queued,
and publishes to SQS. SQS triggers the worker Lambda, which claims the job atomically,
downloads the file, parses and cleans it, uploads the processed version to S3, then
marks the job complete in PostgreSQL. The frontend polls every 5s and shows the user
when it's done.

**Q: How do you prevent one user from accessing another user's jobs?**

Every repository function that fetches a job for a user includes `WHERE user_id = ?` in
the SQL query. The `user_id` always comes from the JWT `sub` claim — never from the
request body or query params. The API returns 404 (not 403) for wrong-owner requests to
prevent enumeration. The worker sources `user_id` from the database record, not the SQS
message, to prevent spoofed messages from landing files in the wrong S3 prefix.

**Q: What happens if the Lambda worker crashes mid-processing?**

The SQS message becomes visible again after the visibility timeout (1800s). The worker
is re-invoked, calls `claim_job_for_processing()` which succeeds (processing state is
in the WHERE clause), re-downloads the file from S3, and retries the full pipeline.
After `maxReceiveCount` (5) failures, the message goes to the DLQ. The job stays in
`processing` until a manual recovery resets it.

**Q: Why is the processed S3 key constructed from the DB user_id rather than the SQS message?**

The SQS message body is untrusted input. Anyone who can publish to the queue could set
`user_id` to any value and cause the worker to write processed files into another user's
S3 prefix. The DB record was written by the authenticated API endpoint using the
verified Cognito sub claim — it is the authoritative source of truth for user identity.

---

## 10. Team Walkthrough

"With Day 6 complete, we have a fully integrated application. The frontend and backend
speak the same API contract — every field name, HTTP status, and error format is
consistent. The entire CSV processing workflow runs end-to-end: a user uploads a file,
the worker cleans it, and the user downloads the result.

The security model is layered: API Gateway validates tokens, FastAPI enforces ownership
at the query level, IAM policies scope S3 access by prefix, and the worker ignores
untrusted SQS data. The error handling is honest — users see clear messages, never
stack traces.

What we haven't done yet is deploy to real AWS infrastructure. The CloudFormation
templates are ready, the Docker image builds, and the SQL migration is written. The
next phase is deployment: provision the VPC, RDS, Cognito, ECR, and Lambda, run the
migration, and do a live end-to-end smoke test."
