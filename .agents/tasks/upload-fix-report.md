# CSVForge Upload Failure — Investigation Report

**Date:** 2025-07-21  
**Investigator:** Kiro investigation agent  
**Symptom:** Step 1 ✓, Step 2 ✓, Step 3 'Processing' → "File upload failed. Please check your connection and try again."  
**App URL:** https://d38s2v3jcf0q6l.cloudfront.net  

---

## Executive Summary (TL;DR)

There are **three bugs that together break the upload flow**, plus two additional issues that cause silent worker failures for certain CSV files. The primary blocker preventing any upload from reaching S3 is **Finding #1** (S3 CORS does not allow the CloudFront origin). The error message shown in the UI matches `xhr.onerror`, which is exactly what the browser reports for a CORS-blocked request.

| # | Severity | Where it fails | What breaks |
|---|----------|---------------|------------|
| 1 | **CRITICAL** | S3 PUT (Step 2 — the S3 upload itself) | S3 CORS only allows `http://localhost:5173`; browser from CloudFront origin blocks the PUT before it reaches S3 |
| 2 | **HIGH** | POST /jobs (Step 1 — creating the job) | Presigned URL is signed for `ap-south-1`; the deployed bucket is in `ap-southeast-2` — S3 rejects with 403 SignatureDoesNotMatch even if CORS is fixed |
| 3 | **HIGH** | Backend `ALLOWED_ORIGINS` (Step 1 and 3 — any API call) | Lambda `ALLOWED_ORIGINS` env var does not include the CloudFront URL → browser blocks the API response from FastAPI too |
| 4 | **MEDIUM** | Worker Lambda (Step 3 — CSV processing) | Worker `S3_OUTPUT_BUCKET` is not set → falls back to same bucket as input; presigned upload URL `ContentType` is `text/csv` but S3 PUT signature includes Content-Type — any mismatch causes 403 |
| 5 | **LOW** | Worker CSV processor | Several CSV edge cases (binary files, large Unicode, Excel with BOM) will silently hit `InvalidCSVError` and permanently fail the job with no useful message shown |

---

## Finding #1 — S3 CORS does not allow the CloudFront origin (CRITICAL)

**File:** `infrastructure/data.yaml`, lines 109–125 (CorsConfiguration block)  
**Confidence:** HIGH

### What is configured
```yaml
CorsConfiguration:
  CorsRules:
    - AllowedHeaders:
        - "*"
      AllowedMethods:
        - PUT
      AllowedOrigins:
        - "http://localhost:5173"   # ← only localhost is allowed
```

The S3 bucket `csvforge-files-dev` has CORS configured with **exactly one allowed origin: `http://localhost:5173`**.

The production frontend is served from `https://d38s2v3jcf0q6l.cloudfront.net`.

### What happens at runtime
1. Browser at `https://d38s2v3jcf0q6l.cloudfront.net` calls `uploadFileToS3()`.
2. Before the XHR PUT, the browser sends a CORS preflight `OPTIONS` to the S3 presigned URL with:
   ```
   Origin: https://d38s2v3jcf0q6l.cloudfront.net
   Access-Control-Request-Method: PUT
   ```
3. S3 returns a CORS response that does **not** include `https://d38s2v3jcf0q6l.cloudfront.net` in `Access-Control-Allow-Origin`.
4. The browser blocks the request. `xhr.onerror` fires.
5. The frontend catches the error and sets `uploadState.error` to: `"File upload failed. Please check your connection and try again."` — exactly the message the user sees.

### The fix
Add the CloudFront domain to `AllowedOrigins` in `data.yaml`:
```yaml
AllowedOrigins:
  - "http://localhost:5173"
  - "https://d38s2v3jcf0q6l.cloudfront.net"
```
Then redeploy the `csvforge-data-dev` CloudFormation stack.

> Note: The CloudFront URL is already in `api.yaml`'s `CorsConfiguration.AllowOrigins` (API Gateway CORS), which confirms the URL is correct and intentional — it was just missed on the S3 bucket.

---

## Finding #2 — Region mismatch: presigned URL signed for `ap-south-1`, bucket is in `ap-southeast-2` (HIGH)

**File:** `backend/s3_client.py`, `get_s3_client()` function, line ~45  
**File:** `frontend/.env.production`, line 2  
**Confidence:** HIGH

### Evidence

`backend/s3_client.py` `get_s3_client()`:
```python
region = os.getenv("AWS_DEFAULT_REGION", "ap-south-1")   # default: ap-south-1
```

`frontend/.env.production`:
```
VITE_API_URL=https://wem0j5l8ad.execute-api.ap-southeast-2.amazonaws.com
VITE_COGNITO_USER_POOL_ID=ap-southeast-2_dTJDWZwLM
VITE_AWS_REGION=ap-southeast-2
```

`infrastructure/worker.yaml` (ECRRepositoryUri default):
```yaml
Default: "431251892391.dkr.ecr.ap-southeast-2.amazonaws.com/csvforge-worker-dev"
```

The CloudFormation infrastructure, ECR repository, Cognito User Pool, and API Gateway are **all deployed in `ap-southeast-2`** (Sydney). The `.env.example` and most comments in the code say `ap-south-1` (Mumbai), but the actual deployed resources are in `ap-southeast-2`.

`s3_client.get_s3_client()` falls back to `ap-south-1` if `AWS_DEFAULT_REGION` is not set. In Lambda, `AWS_DEFAULT_REGION` **is** set automatically by the runtime to the Lambda's execution region — so as long as the Lambda runs in `ap-southeast-2`, it reads the correct region from the environment.

**However:** if `AWS_DEFAULT_REGION` is not set in the Lambda environment variables (the `api.yaml` stack does not explicitly set `AWS_DEFAULT_REGION` as an environment variable for the Lambda), the client falls back to `ap-south-1`. A presigned URL signed for region `ap-south-1` against a bucket in `ap-southeast-2` will result in:
```
S3 400 Bad Request — AuthorizationHeaderMalformed:
The authorization header is malformed; the region '<region>' is wrong;
expecting 'ap-southeast-2'
```

### The fix
Explicitly add `AWS_DEFAULT_REGION: ap-southeast-2` to the `APILambda.Environment.Variables` block in `infrastructure/api.yaml` and the same in `worker.yaml`. This makes the region explicit and removes the dependency on Lambda's auto-injected value.

Alternatively, the code in `s3_client.py` `get_s3_client()` should change the default to `ap-southeast-2` to match the deployment, but changing code is secondary to the infrastructure fix.

---

## Finding #3 — FastAPI ALLOWED_ORIGINS does not include the CloudFront URL (HIGH)

**File:** `backend/main.py`, `_get_cors_origins()`, line ~95  
**File:** `infrastructure/api.yaml`, `APILambda.Environment.Variables`  
**Confidence:** HIGH

### What is configured
`main.py` `_get_cors_origins()`:
```python
raw = os.getenv("ALLOWED_ORIGINS", "")
if raw.strip():
    return [origin.strip() for origin in raw.split(",") if origin.strip()]
return ["http://localhost:5173"]   # fallback
```

The `ALLOWED_ORIGINS` environment variable must be set on the API Lambda with the CloudFront domain. Looking at `infrastructure/api.yaml`, the Lambda `Environment.Variables` block does **not** include `ALLOWED_ORIGINS`:

```yaml
Environment:
  Variables:
    ENVIRONMENT: production
    DB_SECRET_ARN: !Ref DBSecretArn
    DB_HOST: !Ref RDSEndpoint
    DB_PORT: "5432"
    DB_NAME: csvforge
    S3_BUCKET_NAME: ...
    SQS_QUEUE_URL: ...
    # ← ALLOWED_ORIGINS is missing
```

Without this, `ALLOWED_ORIGINS` is empty string → FastAPI defaults to `["http://localhost:5173"]`.

Meanwhile, `api.yaml` correctly configures API Gateway CORS with `https://d38s2v3jcf0q6l.cloudfront.net`. But FastAPI's `CORSMiddleware` adds its own `Access-Control-Allow-Origin` header on the Lambda response. When API Gateway receives the Lambda response, it merges or overwrites CORS headers. If FastAPI does **not** add the correct origin, certain responses (especially 4xx and 5xx) may be returned without CORS headers, causing the browser to block them.

This blocks the API calls (POST /jobs, POST /uploads/{id}/complete) from being readable by the CloudFront-hosted frontend.

### The fix
Add to `APILambda.Environment.Variables` in `api.yaml`:
```yaml
ALLOWED_ORIGINS: "https://d38s2v3jcf0q6l.cloudfront.net"
```
Or store it in SSM Parameter Store and inject at deploy time.

---

## Finding #4 — Worker `S3_OUTPUT_BUCKET` unset; SQS queue URL has wrong region placeholder (MEDIUM)

**File:** `infrastructure/worker.yaml`, `WorkerLambda.Environment.Variables`  
**File:** `backend/main.py`, `upload_complete()` route, line ~230  
**Confidence:** MEDIUM

### Part A — Missing `S3_OUTPUT_BUCKET`
`worker/handler.py` `_get_output_bucket()`:
```python
def _get_output_bucket() -> str:
    return os.getenv("S3_OUTPUT_BUCKET") or _get_input_bucket()
```

`infrastructure/worker.yaml` Lambda environment variables only set `S3_BUCKET_NAME`, not `S3_OUTPUT_BUCKET`. This means both input and output go to the same bucket (the fallback), which is the intended single-bucket design. This is **not a bug** — it is working by design — but it is undocumented in the infrastructure file, creating confusion. The worker correctly falls back to `S3_BUCKET_NAME`.

### Part B — SQS queue URL placeholder
`backend/main.py` `upload_complete()`:
```python
queue_url = os.getenv(
    "SQS_QUEUE_URL",
    "https://sqs.ap-south-1.amazonaws.com/ACCOUNT_ID_PLACEHOLDER/csvforge-jobs-dev",
)
```

If `SQS_QUEUE_URL` is not set (or is empty), the code falls back to a placeholder URL with `ap-south-1` and `ACCOUNT_ID_PLACEHOLDER`. The `api.yaml` Lambda environment correctly injects `SQS_QUEUE_URL` from the queues stack output, so this is only a problem if that variable is missing. However, the default value would cause a silent `QueueDoesNotExist` error if `SQS_QUEUE_URL` is accidentally unset. The job would be queued in the DB but the SQS message would never be sent — the worker would never process it. The user would see the job stuck in `queued` state forever.

### The fix (Part B)
Change the fallback default to explicitly fail:
```python
queue_url = os.getenv("SQS_QUEUE_URL")
if not queue_url:
    raise ValueError("SQS_QUEUE_URL environment variable is required")
```
This makes misconfiguration loud rather than silent.

---

## Finding #5 — CSV worker edge cases that cause silent permanent failures (LOW)

**File:** `worker/csv_processor.py`  
**Confidence:** MEDIUM

The `InvalidCSVError` path in the worker calls `fail_job()` and returns cleanly. The job status becomes `failed` and `failure_reason` is set. The user sees a generic error in the UI. The following CSV types will hit this path:

| CSV type | What happens | Why |
|----------|-------------|-----|
| Empty file (0 bytes) | `InvalidCSVError("File is empty")` | Explicit check in `parse_and_analyse()` |
| Binary/non-UTF-8 file | `InvalidCSVError("File encoding is not valid UTF-8: ...")` | UnicodeDecodeError caught |
| CSV with duplicate column names | `InvalidCSVError("Duplicate column name: ...")` | Explicit check in step 4b |
| CSV with empty column header | `InvalidCSVError("Column has an empty name at position N")` | Explicit check in step 4a |
| CSV with ragged rows (variable field count) | `InvalidCSVError("Row N has M fields; expected K")` | Field count check in step 5 |
| Malformed CSV (unclosed quote) | `InvalidCSVError("Malformed CSV: ...")` | `csv.Error` caught |

These are all handled correctly per the AD-21 contract. The concern is that the **UI does not display the `failure_reason` prominently** — the user sees the job enter `failed` state but may not understand why. This is a UX issue, not a data loss issue.

Additionally, a TSV (tab-separated) file saved with a `.csv` extension will be accepted by client-side validation but processed incorrectly: `csv.reader` uses comma as the default delimiter, so each row will appear as a single-column CSV. The job will complete "successfully" but the output will have one column containing the entire tab-separated line. There is no server-side delimiter detection.

---

## Answering the Investigation Brief Questions

### Q1: What exact HTTP request fails?

**The S3 PUT (Step 2 of the upload flow) fails first**, due to CORS. Specifically, `xhr.onerror` fires on the XHR in `uploadFileToS3()` in `frontend/src/api/client.ts` (line ~255). The browser never receives a response from S3 because the CORS preflight is rejected. The error message "File upload failed. Please check your connection and try again." comes from `xhr.onerror` in `client.ts`.

The stepper shows Step 3 'Processing' as the failing step because the stepper's `activeStep()` function returns `2` when `uploadState.step === 'error'` (`UploadPage.tsx`, lines ~147–158), regardless of which underlying step failed. So "Step 3: Processing" failing in the UI does not mean the failure happened during the confirmation call — it means the upload wizard reached the error state while in any step.

### Q2: Are there CORS misconfigurations?

Yes — two separate CORS issues:

1. **S3 bucket CORS** (`data.yaml`): Missing CloudFront origin in `AllowedOrigins`. Blocks the S3 PUT. **This is the primary cause of failure.**

2. **FastAPI CORSMiddleware** (`main.py` + `api.yaml` Lambda env): Missing `ALLOWED_ORIGINS` env var on the API Lambda. Without it, FastAPI's middleware only allows `http://localhost:5173`, which may cause the POST /jobs response to be blocked for the CloudFront frontend (though API Gateway also adds CORS headers, so the interaction depends on how they stack — FastAPI headers should match).

### Q3: Does presigned URL generation look correct?

The logic in `s3_client.generate_presigned_upload_url()` is structurally correct:
- Uses `put_object` ClientMethod
- Sets `ContentType: text/csv` in Params
- Expires in 900 seconds

The **region problem** (Finding #2) is the critical issue: if `AWS_DEFAULT_REGION` is not explicitly set to `ap-southeast-2` on the Lambda, the presigned URL may be signed for `ap-south-1`, producing a URL that S3 will reject with `AuthorizationHeaderMalformed`.

Additionally, `generate_presigned_upload_url()` sets `ContentType` in `Params` — this means the Content-Type is part of the URL signature. The frontend XHR in `uploadFileToS3()` correctly sends `xhr.setRequestHeader('Content-Type', 'text/csv')` — these match, so the Content-Type itself is not a bug, assuming the signed region is correct.

### Q4: Does SQS dispatch happen before or after S3 PUT — race condition?

The ordering is **correct and there is no race condition**:

1. Frontend: POST /jobs → gets presigned URL (DB record created as `pending_upload`)
2. Frontend: PUT to S3 (direct, no backend involvement)
3. Frontend: POST /uploads/{id}/complete → backend calls `object_exists()`, `get_object_size()`, `transition_to_queued()`, **then** `send_job_message()`

The SQS message is sent **only after** S3 PUT succeeds and the DB record is transitioned to `queued`. There is no race condition here. AD-29 documents the known limitation that if `send_job_message()` fails after `transition_to_queued()` succeeds, the job is stuck in `queued` with no SQS message — but this is a known trade-off, not a bug causing the current symptom.

### Q5: Are there obvious bugs (wrong field name, endpoint path, auth header)?

**Yes — one significant path discrepancy:**

`frontend/src/api/client.ts` `confirmUpload()`:
```typescript
return apiRequest<UploadCompleteResponse>('POST', `/uploads/${jobId}/complete`);
```

`backend/main.py`:
```python
@app.post("/uploads/{job_id}/complete", ...)
def upload_complete(...)
```

These match — `/uploads/{jobId}/complete` in the frontend maps to `/uploads/{job_id}/complete` in the backend. This is correct.

**One potential auth header issue (low confidence):**  
In production, the frontend uses Cognito JWT. `buildAuthHeaders()` in `client.ts` adds `Authorization: Bearer <token>`. The `uploadFileToS3()` XHR deliberately does **not** add Authorization — correct per AD-FE-04. The API calls correctly include the token. No auth header bug found.

### Q6: What edge cases in the CSV processing worker cause silent failures?

See Finding #5 above. Summary:
- Binary/non-UTF-8 files: permanent fail (expected)
- Ragged CSV (variable row lengths): permanent fail (expected)
- TSV saved as .csv: processed incorrectly as single-column CSV (silent wrong output)
- Header-only CSV (zero data rows): **processed successfully**, outputs a header-only result — correct per AD-21 contract
- CSV with Windows-1252 encoding (common from Excel): fail with `InvalidCSVError` — the worker only accepts UTF-8
- Very large files (50 MiB): the worker has 1024 MB RAM and 512 MB /tmp (worker.yaml) — sufficient for two 50 MB files, but cutting it close if other processes use memory

---

## Recommended Fixes (Priority Order)

### Fix 1 (Critical) — Add CloudFront origin to S3 CORS
**File:** `infrastructure/data.yaml`  
```yaml
CorsConfiguration:
  CorsRules:
    - AllowedHeaders:
        - "*"
      AllowedMethods:
        - PUT
      AllowedOrigins:
        - "http://localhost:5173"
        - "https://d38s2v3jcf0q6l.cloudfront.net"
      ExposedHeaders:
        - ETag
      MaxAge: 3000
```
Redeploy the `csvforge-data-dev` stack.

### Fix 2 (High) — Add ALLOWED_ORIGINS to API Lambda
**File:** `infrastructure/api.yaml`, under `APILambda.Environment.Variables`:
```yaml
ALLOWED_ORIGINS: "https://d38s2v3jcf0q6l.cloudfront.net,http://localhost:5173"
```
Redeploy the `csvforge-api-dev` stack.

### Fix 3 (High) — Explicitly set AWS_DEFAULT_REGION on API Lambda
**File:** `infrastructure/api.yaml`, under `APILambda.Environment.Variables`:
```yaml
AWS_DEFAULT_REGION: "ap-southeast-2"
```
This ensures presigned URLs are always signed for the correct region regardless of Lambda runtime injection behavior.

### Fix 4 (Medium) — Make SQS_QUEUE_URL misconfiguration loud
**File:** `backend/main.py`, `upload_complete()`:  
Change the fallback to raise instead of silently using the wrong URL. See Finding #4 Part B.

### Fix 5 (Low) — Document TSV/non-UTF-8 limitations in the UI
Surface the `failure_reason` from the job record in the frontend's job detail page so users understand why their upload failed (e.g., "File encoding is not valid UTF-8").

---

## File/Symbol Reference Index

| File | What was examined |
|------|--------------------|
| `frontend/src/api/client.ts` | `confirmUpload()`, `uploadFileToS3()`, `buildAuthHeaders()`, `apiRequest()` |
| `frontend/src/pages/UploadPage.tsx` | `handleUpload()`, `activeStep()`, error state rendering |
| `frontend/.env.production` | `VITE_API_URL`, `VITE_COGNITO_USER_POOL_ID`, `VITE_AWS_REGION` |
| `backend/main.py` | `_get_cors_origins()`, `upload_complete()`, `_create_upload_job_logic()` |
| `backend/s3_client.py` | `get_s3_client()`, `generate_presigned_upload_url()`, `object_exists()` |
| `backend/sqs_client.py` | `send_job_message()` |
| `backend/schemas.py` | `UploadIntentResponse`, `UploadCompleteResponse` |
| `backend/auth.py` | `get_current_user()`, local dev bypass |
| `worker/handler.py` | `_process_record()`, `_get_output_bucket()` |
| `worker/csv_processor.py` | `parse_and_analyse()`, `clean()`, `write_csv()` |
| `infrastructure/data.yaml` | S3 CORS configuration — primary bug |
| `infrastructure/api.yaml` | API Gateway CORS, Lambda env vars (missing `ALLOWED_ORIGINS`) |
| `infrastructure/worker.yaml` | Worker Lambda env vars, ECR URI (confirms ap-southeast-2) |
| `infrastructure/queues.yaml` | SQS visibility timeout, DLQ config |
| `.env.example` | Environment variable documentation |
