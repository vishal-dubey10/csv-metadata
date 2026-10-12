# CSVForge Mental Map

One-page reference for the architecture, data flow, and job lifecycle.
Use this for revision before labs or interviews.

---

## The big picture in one sentence

A user uploads a CSV → it lands in S3 → a Lambda worker cleans it → the user downloads the result.
The API server never holds the file bytes; PostgreSQL holds the job state; SQS is the trigger.

---

## Component map

```
┌─────────────────────────────────────────────────────────────────────┐
│  Browser                                                            │
│  React 19 + TypeScript + MUI + React Admin                          │
│  http://localhost:5173 (dev) or S3 static website (prod)           │
└────────┬────────────────────────────────────────────────────────────┘
         │ HTTPS  Authorization: Bearer <access_token>
         │
┌────────▼────────────────────────────────────────────────────────────┐
│  API Gateway (HTTP API)                                             │
│  JWT Authoriser → validates Cognito access token against JWKS      │
│  On failure: 401. On success: injects claims into event context     │
└────────┬────────────────────────────────────────────────────────────┘
         │ Lambda proxy (payload format 2.0)
         │
┌────────▼────────────────────────────────────────────────────────────┐
│  FastAPI (csvforge-api-dev Lambda)                                  │
│  backend/main.py — 8 endpoints                                      │
│  backend/auth.py — get_current_user(), require_admin()              │
│  backend/schemas.py — Pydantic request/response validation          │
│  backend/db/job_repository.py — all SQL for the jobs table          │
│  backend/s3_client.py — presigned URLs, HeadObject, up/download     │
│  backend/sqs_client.py — send_job_message(), parse_job_message()    │
└─────┬──────────────────────────────┬───────────────────────────────┘
      │ SQLAlchemy + psycopg 3        │ boto3
      │                              │
┌─────▼──────────────┐   ┌───────────▼─────────────────────────────┐
│  PostgreSQL 16      │   │  S3 bucket (csvforge-files-dev)         │
│  RDS, private VPC   │   │  uploads/{user_id}/{job_id}/original.csv│
│  jobs table (1 tbl) │   │  processed/{user_id}/{job_id}/result.csv│
│  15 columns         │   │  Private; CORS for browser PUT upload   │
│  3 indexes          │   └─────────────────┬───────────────────────┘
└─────────────────────┘                     │ (browser uploads here directly)
                                            │
┌───────────────────────────────────────────▼────────────────────────┐
│  SQS (csvforge-jobs-dev)                                           │
│  main queue + DLQ (csvforge-jobs-dev-dlq)                          │
│  Message: {job_id, s3_key}                                         │
│  VisibilityTimeout: 1800s. MaxReceiveCount: 5. DLQ after 5 fails.  │
└───────────────────────────────────────────┬────────────────────────┘
                                            │ event source mapping
                                            │
┌───────────────────────────────────────────▼────────────────────────┐
│  Worker Lambda (csvforge-worker-dev)                                │
│  worker/handler.py — lambda_handler()                               │
│  worker/csv_processor.py — parse_and_analyse(), clean(), write_csv()│
│  worker/exceptions.py — InvalidCSVError                            │
│  Container image from ECR (psycopg binary compiled for Lambda)     │
│  1024 MB memory, 300s timeout                                      │
└────────────────────────────────────────────────────────────────────┘
```

---

## The job state machine

```
pending_upload   (hidden from users — upload not yet confirmed)
      │
      │  POST /uploads/{id}/complete  (S3 confirmed + SQS published)
      ▼
   queued         ← first user-visible state
      │
      │  worker: claim_job_for_processing() conditional UPDATE
      ▼
 processing
      │
   ┌──┴──┐
   │     │
   ▼     ▼
completed  failed
```

**Terminal states:** `completed` and `failed`. No transitions out.

**Key invariant:** `claim_job_for_processing()` is an atomic conditional UPDATE:
`UPDATE jobs SET status='processing' WHERE id=? AND status IN ('queued','processing')`
`rowcount == 1` → this worker owns the job. `rowcount == 0` → job is in a terminal state.

---

## The upload flow (step by step)

```
Browser                    FastAPI                    S3                  SQS         Worker
  │                           │                       │                   │             │
  │── POST /jobs ────────────►│                       │                   │             │
  │   {user_email, filename}  │── INSERT jobs ──────► PostgreSQL          │             │
  │                           │   status=pending_upload                   │             │
  │                           │── generate presigned PUT URL             │             │
  │◄── {job_id, upload_url} ──│                       │                   │             │
  │                           │                       │                   │             │
  │── PUT file bytes ─────────────────────────────►   │                   │             │
  │   (Content-Type: text/csv)│                       │                   │             │
  │◄── 200 OK ─────────────────────────────────────── │                   │             │
  │                           │                       │                   │             │
  │── POST /uploads/{id}/complete ──────────────────► │                   │             │
  │                           │── HeadObject ────────►│                   │             │
  │                           │◄── ContentLength ─────│                   │             │
  │                           │── UPDATE status='queued' ─► PostgreSQL    │             │
  │                           │── SendMessage ─────────────────────────►  │             │
  │◄── {job_id, "queued"} ────│                                           │             │
  │                           │                                           │             │
  │── GET /jobs/{id} ─────────►                                           │── trigger ─►│
  │   (poll every 5s)         │                                           │             │
  │◄── {status: "processing"} │                                           │             │── UPDATE status='processing'
  │                           │                                           │             │── download CSV from S3
  │── GET /jobs/{id} ─────────►                                           │             │── clean CSV
  │◄── {status: "completed",  │                                           │             │── upload to S3
  │    row_count, columns...} │                                           │             │── UPDATE status='completed'
  │                           │                                           │             │
  │── GET /jobs/{id}/download ►                                           │             │
  │◄── {download_url} ────────│                                           │             │
  │── GET presigned URL ────────────────────────────►                     │             │
  │◄── CSV bytes ──────────────────────────────────── │                   │             │
```

---

## Key files and their roles

| File | Role |
|------|------|
| `backend/main.py` | FastAPI app, all 8 HTTP endpoints, CORS middleware |
| `backend/auth.py` | `get_current_user()`, `require_admin()` dependencies |
| `backend/schemas.py` | Pydantic request/response models (validation + serialization) |
| `backend/db/connection.py` | Engine, SessionLocal, Base, `get_db()` dependency |
| `backend/db/job_repository.py` | All SQL: create, read, list, state transitions |
| `backend/models/job.py` | `Job` ORM model, `JobStatus` enum, 15 columns |
| `backend/s3_client.py` | Presigned URL generation, `HeadObject`, `upload_file`, `download_file` |
| `backend/sqs_client.py` | `send_job_message()`, `parse_job_message()` |
| `worker/handler.py` | Lambda entry point, 12-step processing pipeline |
| `worker/csv_processor.py` | `parse_and_analyse()`, `clean()`, `write_csv()` |
| `worker/exceptions.py` | `InvalidCSVError` — permanent vs transient failure signal |
| `frontend/src/api/client.ts` | All HTTP calls; injects auth token; XHR for S3 upload |
| `frontend/src/auth/AuthContext.tsx` | Cognito auth + local dev bypass; `useAuth` hook |
| `frontend/src/pages/UploadPage.tsx` | 3-step upload wizard |
| `frontend/src/pages/JobListPage.tsx` | Job table with 5-second polling |
| `infrastructure/network.yaml` | VPC, subnets, security groups, VPC endpoints |
| `infrastructure/data.yaml` | S3 bucket with CORS and encryption |
| `infrastructure/queues.yaml` | SQS main queue + DLQ |
| `infrastructure/api.yaml` | Cognito, API Lambda, API Gateway |
| `infrastructure/worker.yaml` | ECR, Worker Lambda, SQS event source mapping |

---

## Five patterns you must be able to explain

**1. Presigned URL**
A time-limited, pre-authenticated URL that grants the bearer permission to perform one
specific S3 operation (PUT or GET on one specific key). Generated server-side using IAM
credentials. The browser holds no AWS credentials.

**2. Conditional UPDATE (idempotency guard)**
`UPDATE jobs SET status='processing' WHERE id=? AND status IN ('queued','processing')`
Atomic: check + set in one SQL statement. No SELECT + UPDATE race condition. `rowcount`
tells you whether you won. One call site: `claim_job_for_processing()`.

**3. At-least-once delivery + idempotency**
SQS Standard delivers each message at least once. Rare duplicates happen. The worker's
`claim_job_for_processing()` conditional UPDATE is idempotent: the second invocation either
also matches (job still in processing, first Lambda died) or finds `completed`/`failed`
and returns False immediately.

**4. Repository pattern**
`job_repository.py` is the only place in the codebase that writes SQL for the `jobs`
table. Routes and the worker import functions from it, never write raw queries. This
separates the concern of "what query" (repository) from "when to query" (route/worker).

**5. S3-before-DB write order (AD-14)**
The worker uploads the processed CSV to S3 BEFORE updating the DB to `completed`.
If the DB update fails, the job retries (S3 upload was idempotent — same key, same data).
If S3 upload fails first, the DB never says `completed` — the retry downloads and
re-uploads. The reverse order creates an unrecoverable state: a `completed` DB row with
no S3 object, causing broken presigned download URLs.

---

## What is NOT yet deployed to AWS

- RDS PostgreSQL instance (required by both Lambdas)
- Frontend S3 static website bucket
- CloudFront distribution (optional — needed for HTTPS on the frontend)
- Alembic migration against real RDS
- Worker Docker image pushed to ECR
- API Lambda zip package uploaded to S3 deployment bucket
- Cognito test user created
