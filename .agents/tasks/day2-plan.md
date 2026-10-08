# CSVForge Day 2 — Implementation Plan

## What Day 1 Already Built (do not duplicate)

- `backend/main.py` — FastAPI app, CORS middleware, `/health` endpoint
- `backend/db/connection.py` — engine, SessionLocal, Base, get_db(), local/production credential paths
- `backend/db/job_repository.py` — full CRUD + state-machine transitions (create_job, get_job_by_id, get_job_by_id_and_user, list_jobs_for_user, list_all_jobs, count_all_jobs, transition_to_queued, claim_job_for_processing, complete_job, fail_job)
- `backend/models/job.py` — Job ORM model (15 columns), JobStatus enum (5 states), 3 indexes
- `backend/alembic/versions/0001_initial_jobs_table.py` — initial migration; creates `jobs` table
- `worker/__init__.py` — placeholder package
- `tests/conftest.py`, `tests/unit/`, `tests/integration/` — 53 unit tests all passing
- `docker-compose.yml` — PostgreSQL 16 + FastAPI with hot-reload

## AWS Environment Status

- **AWS CLI version:** 2.37.8 (macOS Darwin arm64)
- **Region configured:** `ap-south-1` (Mumbai) — this will be the Day 2 development region
- **Credential type:** static access key from shared-credentials-file (key suffix `...ODDA`)
- **`aws sts get-caller-identity` result:** `InvalidClientTokenId` — the current credentials are **invalid or expired**

⚠️  **STOP — ACTION REQUIRED before implementing any AWS resource:**  
The credentials in `~/.aws/credentials` return `InvalidClientTokenId`. This means one of:  
1. The access key has been deactivated or deleted in IAM.  
2. The wrong profile is active.  
3. The key was never activated.  
Before any `aws s3`, `aws sqs`, or `aws cloudformation` command will work, this must be fixed. The implementer must run `aws sts get-caller-identity` and get a valid JSON response with Account/UserId/Arn before proceeding with any AWS resource creation. Instructions: open the AWS Console → IAM → Users → find the user → Security credentials → create a new access key, then run `aws configure` to update `~/.aws/credentials`.

- **IAM access level:** Unknown (credentials invalid). Once fixed, verify the principal does NOT have AdministratorAccess on the **application runtime IAM roles**. The developer's personal IAM user may reasonably have broad permissions for this learning exercise; the Lambda execution roles (API Lambda role, Worker Lambda role) must be least-privilege (AD-20, AD-15).

## AWS Region Decision

**Use `ap-south-1` (Mumbai)** — this is already configured in `~/.aws/config`. All Day 2 resources (S3 bucket, SQS queue, DLQ) will be created in this region. The `.env` file must set `AWS_DEFAULT_REGION=ap-south-1`. Document why regional consistency matters: all resources in a single region avoid cross-region latency, avoid cross-region data transfer costs, and simplify IAM resource ARNs.

## Day 2 Tasks (ordered by dependency)

---

### TASK 1 — Verify and fix AWS credentials

**Description:** Run `aws sts get-caller-identity` and confirm a valid JSON response. Document the Account ID (safe to log), UserId, and Arn. Verify the active principal is not using `AdministratorAccess` on runtime roles.

**Files:** none (operational step)

**Verify:** `aws sts get-caller-identity` returns valid JSON with Account, UserId, Arn.

---

### TASK 2 — Create S3 bucket for CSVForge uploads and processed output

**Description:** Create the S3 bucket `csvforge-files-dev` in `ap-south-1`. The bucket must be private (public access fully blocked). Add the CORS configuration needed for direct browser PUT uploads. Do not use LocalStack; use the real AWS account.

**Bucket name:** `csvforge-files-dev` (set in `.env` as `S3_BUCKET_NAME=csvforge-files-dev`)

**Object key structure:**
- Uploads: `uploads/{user_id}/{job_id}/original.csv`
- Processed: `processed/{user_id}/{job_id}/result.csv`

Why job_id in the key: the key is deterministic — given a job record, you always know exactly where its file lives without querying S3 for a listing. It also namespaces files per job so a re-upload can't overwrite a different job's file.

**CORS config needed for presigned PUT (add to S3 bucket):**
```json
[{"AllowedHeaders": ["*"], "AllowedMethods": ["PUT"], "AllowedOrigins": ["http://localhost:5173"], "ExposeHeaders": ["ETag"]}]
```

**Infrastructure as code:** After creating via AWS CLI, capture in `infrastructure/data.yaml` (CloudFormation) — the `AWS::S3::Bucket` resource with `BlockPublicAcls: true`, `BlockPublicPolicy: true`, `IgnorePublicAcls: true`, `RestrictPublicBuckets: true`, and the CORS configuration.

**Files to create/modify:**
- `infrastructure/data.yaml` — add S3 bucket resource (CloudFormation)
- `.env` — update `S3_BUCKET_NAME=csvforge-files-dev`

**Verify:** `aws s3 ls s3://csvforge-files-dev` succeeds; `aws s3api get-bucket-acl --bucket csvforge-files-dev` confirms no public grants.

---

### TASK 3 — Create SQS queue + DLQ

**Description:** Create two SQS Standard queues in `ap-south-1`: `csvforge-jobs-dev` (main queue) and `csvforge-jobs-dev-dlq` (dead-letter queue). Configure per AD-17: visibility timeout 1800s, max receive count 5, message retention 4 days.

**Queue names:**
- Main: `csvforge-jobs-dev`
- DLQ: `csvforge-jobs-dev-dlq`

**Configuration:**
- `VisibilityTimeout`: 1800 (6 × Lambda timeout of 300s — see AD-17)
- `MessageRetentionPeriod`: 345600 (4 days in seconds)
- `MaxReceiveCount` on redrive policy: 5
- DLQ ARN wired into the main queue's redrive policy

**Infrastructure as code:** Capture in `infrastructure/queues.yaml` (CloudFormation) — `AWS::SQS::Queue` for both queues with correct properties.

**Update `.env`:**
- `SQS_QUEUE_URL=https://sqs.ap-south-1.amazonaws.com/{account_id}/csvforge-jobs-dev`

**Files to create/modify:**
- `infrastructure/queues.yaml` — two SQS queue resources
- `.env` — update `SQS_QUEUE_URL`

**Verify:** `aws sqs get-queue-attributes --queue-url <url> --attribute-names All` shows correct visibility timeout and redrive policy.

---

### TASK 4 — Implement `backend/s3_client.py`

**Description:** Create the S3 client wrapper with presigned URL generation (including `content-length-range` enforcement per AD-27), HeadObject checks, and file upload/download for the worker. This is T-07 from tasks.md.

**Functions to implement:**
- `get_s3_client()` — boto3 S3 client; reads `AWS_ENDPOINT_URL` from env for optional LocalStack override
- `generate_presigned_upload_url(bucket, key, expiry_seconds=900) -> str` — presigned PUT with `ContentType: text/csv` and `content-length-range` condition `[0, 52428800]` (50 MiB)
- `generate_presigned_download_url(bucket, key, expiry_seconds=900) -> str` — presigned GET
- `object_exists(bucket, key) -> bool` — HeadObject; returns False for 404; re-raises other errors
- `get_object_size(bucket, key) -> int` — HeadObject ContentLength
- `upload_file(bucket, key, file_path) -> None` — worker use
- `download_file(bucket, key, dest_path) -> None` — worker use

**Files to create:**
- `backend/s3_client.py`
- `tests/unit/test_s3_client.py` — unit tests using moto[s3]

**Verify:** `PYTHONPATH=. .venv/bin/pytest tests/unit/test_s3_client.py -v` passes; total unit test count ≥ 53 + new tests.

---

### TASK 5 — Implement `backend/sqs_client.py`

**Description:** Create the SQS client wrapper for publishing job messages. The message body contains only `job_id` and `s3_key` — never file content (SQS 256 KB limit; file content belongs in S3). This is T-08 from tasks.md.

**Functions to implement:**
- `get_sqs_client()` — boto3 SQS client
- `send_job_message(queue_url, job_id, s3_key) -> str` — sends `{"job_id": ..., "s3_key": ...}` JSON; returns message ID
- `parse_job_message(body: str) -> dict` — parses JSON body; returns dict with `job_id` and `s3_key`

**Files to create:**
- `backend/sqs_client.py`
- `tests/unit/test_sqs_client.py` — unit tests using moto[sqs]

**Verify:** `PYTHONPATH=. .venv/bin/pytest tests/unit/test_sqs_client.py -v` passes.

---

### TASK 6 — Implement `worker/csv_processor.py` (parser + cleaner)

**Description:** Implement the CSV parsing, validation, and cleaning pipeline in the worker package. This covers T-05 and T-06 from tasks.md. The order of operations is parse → validate → trim → deduplicate. Trim MUST run before dedup (rows identical only after trimming must count as duplicates).

**Files to create:**
- `worker/exceptions.py` — `InvalidCSVError(Exception)` with `.message` attribute
- `worker/csv_processor.py` — `AnalysisResult` dataclass, `CleanResult` dataclass, `parse_and_analyse(file_path) -> AnalysisResult`, `clean(rows) -> CleanResult`, `write_csv(file_path, column_names, rows) -> None`
- `tests/unit/test_csv_processor.py` — full validation contract test suite (see AD-21 for the complete contract)

**Verify:** `PYTHONPATH=. .venv/bin/pytest tests/unit/test_csv_processor.py -v` passes.

---

### TASK 7 — Add FastAPI upload endpoints to `backend/main.py`

**Description:** Add the two upload-flow endpoints per AD-02. FastAPI does NOT process the CSV — it generates a presigned URL and queues a message. This is part of T-14 from tasks.md.

**Endpoints:**

**POST /uploads/intent**
- Creates a Job(pending_upload) via `create_job()`
- Generates a presigned S3 PUT URL via `generate_presigned_upload_url()`
- Returns `{"job_id": ..., "upload_url": ..., "s3_key": ...}`
- Local dev auth bypass: reads `X-Dev-User-Id` and `X-Dev-User-Email` headers when `ENVIRONMENT=local`

**POST /uploads/{job_id}/complete**
- Calls `get_object_size()` to verify the file was actually uploaded to S3
- Validates `ContentLength <= 52_428_800` (50 MiB) — returns 422 if too large (AD-27)
- Calls `transition_to_queued()` — returns 409 if already transitioned
- Publishes SQS message via `send_job_message()`
- Returns `{"job_id": ..., "status": "queued"}`

**Pydantic schemas needed (new file `backend/schemas.py`):**
- `UploadIntentRequest` — `user_email: str`, `original_filename: str`, `file_size_bytes: int`
- `UploadIntentResponse` — `job_id: str`, `upload_url: str`, `s3_key: str`
- `UploadCompleteResponse` — `job_id: str`, `status: str`

**Files to create/modify:**
- `backend/schemas.py` — Pydantic request/response models
- `backend/main.py` — add the two upload routes; import s3_client, sqs_client, job_repository
- `tests/unit/test_upload_endpoints.py` — unit tests using moto + TestClient

**Verify:** `PYTHONPATH=. .venv/bin/pytest tests/unit/ -v` — all unit tests pass including new upload endpoint tests.

---

### TASK 8 — Implement worker Lambda handler skeleton (`worker/handler.py`)

**Description:** Implement the worker's Lambda entry point and CSV processing pipeline. The handler receives an SQS event, claims the job, downloads the CSV from S3, runs the parser and cleaner, uploads the result, and updates PostgreSQL. This is T-09 from tasks.md.

**Processing pipeline:**
1. Parse SQS event → extract `job_id`, `s3_key`
2. `claim_job_for_processing(db, job_id)` — return early if False (terminal state)
3. Download CSV from S3 to `/tmp/{job_id}.csv`
4. `parse_and_analyse(file_path)` — catch `InvalidCSVError` → `fail_job()` and return (no exception raised; SQS deletes message)
5. `clean(analysis.rows)` → `CleanResult`
6. `write_csv(output_path, column_names, cleaned_rows)`
7. `upload_file(bucket, processed_key, output_path)` — S3 write before DB update (AD-14)
8. `complete_job(db, job_id, processed_s3_key, file_size_bytes, row_count, column_count, column_names, duplicate_rows_removed)`

**Transient failure handling:** any exception NOT caught as `InvalidCSVError` propagates out of the handler, which causes SQS to redeliver after visibility timeout.

**Files to create:**
- `worker/handler.py` — `lambda_handler(event, context)` entry point
- `tests/unit/test_worker_handler.py` — unit tests with mocked S3, SQS, and DB

**Verify:** `PYTHONPATH=. .venv/bin/pytest tests/unit/ -v` — all unit tests pass.

---

### TASK 9 — Alembic migration: no schema changes needed

**Note:** Day 2 adds no new columns to the `jobs` table. The existing 15 columns in migration `0001` already have all the fields the worker and API need. No new migration file is needed. This is intentional — the schema was designed in Day 1 to support the full Day 2+ workflow.

---

### TASK 10 — Update `.env.example` and `.env` with real Day 2 values

**Description:** Update `.env` with real bucket name, queue URL, and confirm region. Update `.env.example` to document the new values. Never commit real secrets.

**Files to modify:**
- `.env` — set `S3_BUCKET_NAME`, `SQS_QUEUE_URL`, `AWS_DEFAULT_REGION=ap-south-1`
- `.env.example` — already has placeholders; update comments if needed

**Verify:** `cat .env.example | grep S3_BUCKET_NAME` shows `csvforge-files-dev` as the example value.

---

### TASK 11 — End-to-end smoke test against real AWS

**Description:** With real AWS credentials active, run a manual end-to-end test: create a job via the API, call the upload intent endpoint, upload a small CSV directly to the presigned URL, call the complete endpoint, verify the SQS message is published, verify the job transitions to `queued` in PostgreSQL.

**This is a manual test** — not an automated pytest test — because it requires real AWS credentials and resources.

**Steps:**
1. `docker compose up -d`
2. `alembic upgrade head`
3. `uvicorn backend.main:app --reload`
4. `curl -X POST http://localhost:8000/uploads/intent -H "X-Dev-User-Id: test-user-1" ...`
5. Upload test CSV to the presigned URL using curl
6. `curl -X POST http://localhost:8000/uploads/{job_id}/complete`
7. `aws sqs receive-message --queue-url <SQS_QUEUE_URL>` — confirm message appears
8. Check PostgreSQL: `SELECT status FROM jobs WHERE id = '<job_id>'` → `queued`

**Verify:** All 8 steps complete without error; job status is `queued` in the database.

---

## Schema Changes in Day 2

**None.** The `jobs` table schema from Day 1 (15 columns, migration `0001`) is complete. Day 2 adds no new columns.

## New Files to Create

| File | Purpose |
|------|---------|
| `backend/s3_client.py` | S3 operations (presigned URLs, HeadObject, upload/download) |
| `backend/sqs_client.py` | SQS publish and parse |
| `backend/schemas.py` | Pydantic request/response models for upload endpoints |
| `worker/exceptions.py` | `InvalidCSVError` |
| `worker/csv_processor.py` | CSV parser, cleaner, writer |
| `worker/handler.py` | Lambda entry point |
| `infrastructure/data.yaml` | CloudFormation: S3 bucket |
| `infrastructure/queues.yaml` | CloudFormation: SQS + DLQ |
| `tests/unit/test_s3_client.py` | moto-based S3 client tests |
| `tests/unit/test_sqs_client.py` | moto-based SQS client tests |
| `tests/unit/test_csv_processor.py` | CSV validation contract tests |
| `tests/unit/test_upload_endpoints.py` | Upload endpoint tests |
| `tests/unit/test_worker_handler.py` | Worker handler unit tests |

## AWS Account Context

- **Account ID:** Unknown — `aws sts get-caller-identity` returned `InvalidClientTokenId`
- **Credentials status:** ⚠️ INVALID — must be fixed before any AWS resource creation
- **Region:** `ap-south-1` (Mumbai) — already set in `~/.aws/config`
- **Safety note:** The developer's personal IAM key appears to be stale/deactivated. Rotate or recreate it. Once working, verify the associated IAM user does not have `AdministratorAccess` attached to the application Lambda execution roles (it is acceptable on the developer's personal user for this learning exercise, but must NOT be on runtime roles per AD-20).

## The Factory Analogy (Mental Model)

```
Customer → Browser → Reception Desk (FastAPI) → Warehouse (S3)
                                              → Job Ticket Queue (SQS)
                                              → Factory Worker (Lambda)
                                              → Processed Goods (S3)
                                              → Factory Ledger (PostgreSQL)

IAM       = Security guards (who can open what door)
CloudWatch = CCTV / factory monitoring
DLQ        = Failed jobs room (messages that couldn't be processed)
Alembic    = Blueprint/version history
```

Why this architecture scales: FastAPI returns in milliseconds. The CSV never passes through Lambda's memory. The worker processes asynchronously. If 1000 files are uploaded simultaneously, FastAPI creates 1000 jobs and enqueues 1000 SQS messages in ~100ms each. Lambda scales independently to consume the queue. The API and worker are fully decoupled — neither knows the other exists at runtime.
