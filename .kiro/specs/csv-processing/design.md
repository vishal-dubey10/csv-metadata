#CSVForge — Implementation Design

> This document is the engineering design for the CSVForge MVP.
> It is intended to be read by a developer who wants to *derive* the architecture
> from the problem rather than memorise a list of AWS services.
>
> Every major technology choice is justified: what problem it solves, why CSVForge
> needs it, what alternatives exist, and what trade-off we are accepting.
>
> Requirements are referenced as `[R-XXX-NN]` throughout.
>
> **Scope boundary:** Anything marked *out of MVP scope* is a known future concern,
> not something we are implementing in the two-day sprint.

---

## Table of Contents

1. [Proposed Architecture — Summary](#1-proposed-architecture)
2. [System Diagram](#2-system-diagram)
3. [Technology Justification](#3-technology-justification)
4. [Upload Flow (Direct Browser → S3)](#4-upload-flow)
5. [Worker Flow (SQS → Lambda)](#5-worker-flow)
6. [Database Model](#6-database-model)
7. [Failure and Retry Behavior](#7-failure-and-retry-behavior)
8. [Authentication and Authorization](#8-authentication-and-authorization)
9. [Local Development Approach](#9-local-development-approach)
10. [Testing Strategy](#10-testing-strategy)
11. [Infrastructure as Code — CloudFormation](#11-infrastructure-as-code)
12. [CI/CD Flow](#12-cicd-flow)
13. [Architectural Decisions](#13-architectural-decisions)
14. [Unresolved Questions](#14-unresolved-questions)
15. [Risks](#15-risks)

---

## 1. Proposed Architecture

CSVForge is a **three-tier web application** with a **serverless async processing backend**.

A user authenticates through a React SPA via Amazon Cognito. To upload a CSV, the
browser first calls the FastAPI backend to create a job record and receive a **presigned
S3 upload URL**. The browser then uploads the file directly to S3 — the CSV never passes
through a Lambda function. Once the upload completes, the browser calls a second API
endpoint to confirm the upload; the API verifies the S3 object exists, transitions the
job to `queued`, and puts a message on an SQS queue. A separate Worker Lambda reads from
the queue, downloads the file from S3, processes it, writes the output back to S3, and
updates the job status to `completed` or `failed`. The user polls for status and
downloads the result via another presigned S3 URL.

An administrator uses a React Admin panel (backed by the same API) to view all files,
jobs, and failures.

Infrastructure is defined in CloudFormation. CI/CD is CodePipeline + CodeBuild.
Observability is CloudWatch logs, metrics, and a DLQ depth alarm.

---

## 2. System Diagram

```
Browser (React SPA)
│
│  Step 1: POST /api/v1/uploads/intent  (JWT in Authorization header)
│          <- { job_id, presigned_upload_url }
│
│  Step 2: PUT <presigned_upload_url>   (direct to S3, no API involved)
│
│  Step 3: POST /api/v1/uploads/{job_id}/complete
│          <- { status: "queued" }
│
v
Amazon API Gateway (HTTP API)
  JWT Authoriser (validates Cognito token before any FastAPI code runs)
│
v
FastAPI  (deployed as Lambda zip package)
│                        │
│ reads/writes job state  │ generates presigned URLs
v                        v
PostgreSQL (RDS)        Amazon S3
                        ├── uploads/{user_id}/{job_id}/original.csv
                        └── processed/{user_id}/{job_id}/result.csv
                                │
                        API publishes message after upload confirmed
                                v
                          Amazon SQS  (csv-jobs queue)
                                │
                                │  event source mapping (batch size = 1)
                                v
                        Worker Lambda  (Docker container via ECR)
                        │                        │
                        │ GET object from S3      │ PUT object to S3
                        │ (original CSV)          │ (processed CSV)
                        │                        │
                        └── UPDATE jobs SET status = ... --> PostgreSQL

                   ┌───────────────────────────────────┐
                   │  Dead Letter Queue (csv-jobs-dlq)  │
                   │  receives messages after 3 failed  │
                   │  Lambda invocations                │
                   └───────────────────────────────────┘
                                │
                                │ CloudWatch Alarm fires when depth > 0
                                v
                          CloudWatch (logs, metrics, alarms)
```

---

## 3. Technology Justification

---

### 3.1 FastAPI (Python)

**Problem:** We need an HTTP server that handles authenticated requests, orchestrates
the upload intent/completion handshake, queries job state, and generates presigned URLs.

**Why CSVForge needs it:** The API has clear request/response semantics with structured
validation. FastAPI provides routing, Pydantic-based request validation, async support,
and automatic OpenAPI docs with minimal boilerplate.

**Alternatives:**
- Django REST Framework — heavier; includes an ORM, admin UI, and templating we do not need.
- Flask — lighter but requires assembling validation and serialisation manually.
- Node/Express — valid, but Python is the natural language for the CSV worker; keeping the
  backend in Python means one language across API and worker.

**Trade-off accepted:** FastAPI does not include authentication. Authentication is handled
at the API Gateway layer (JWT validation), which is the correct separation for Lambda deployment.

**If it fails:** A crash surfaces as a 502 from API Gateway. CloudWatch logs show the traceback.

---

### 3.2 Pydantic

**Problem:** HTTP requests arrive as untyped strings. We need validated, typed Python
objects before any data touches the database or business logic.

**Why:** FastAPI uses Pydantic natively. Request bodies become typed Python objects automatically,
with constraint checking (e.g. file size <= 50 MB in the upload intent body). No extra
integration cost.

**Trade-off accepted:** We pin the Pydantic version. v1 and v2 have breaking differences.

---

### 3.3 SQLAlchemy + PostgreSQL (RDS)

**Problem:** We need durable, queryable state for jobs — status, metadata, timestamps,
failure reasons. This state must survive Lambda restarts, support filtered queries (e.g.
all failed jobs), and allow atomic status transitions.

**Why a relational database, not S3 for state?**
S3 is object storage. It does not support queries, atomic updates, or transactions.
Querying job state from S3 would mean listing bucket objects and parsing key names —
fragile, slow, and unmaintainable. PostgreSQL is for structured state. S3 is for file blobs.

**Why PostgreSQL over DynamoDB?**
DynamoDB is valid for simple key-value access. Our admin view requires filtering by status
across all users. In DynamoDB that requires a Global Secondary Index with eventual consistency.
PostgreSQL gives us SQL for free. For MVP data volumes, RDS on a db.t3.micro instance is
the simpler and more familiar choice.

**Why SQLAlchemy?**
Standard Python ORM. SQLAlchemy's query API uses parameterised queries by default,
which significantly reduces SQL injection risk compared to string-concatenated raw SQL.
This protection applies when using the ORM or the Core expression language correctly;
passing raw SQL strings via `text()` without bound parameters would still introduce
injection vulnerabilities. We use SQLAlchemy's ORM-level operations throughout to stay
on the safe path. Handles connection pooling. Raw psycopg2 is more verbose and requires careful parameterisation discipline to achieve the same safety. (Note: CSVForge uses psycopg 3 — `psycopg[binary]` — not psycopg2, per steering decision.)

**Trade-off accepted:** RDS requires VPC configuration (subnet group, security group).
Out of MVP scope: RDS Proxy for connection pooling under Lambda concurrency spikes.

---

### 3.4 Amazon S3

**Problem:** We need durable, scalable storage for uploaded CSV files and processed
output files without putting binary blobs in a relational database.

**Why not store files in PostgreSQL?**
A 50 MB file as a BYTEA column would bloat the database, make backups enormous, and
require streaming the file through the API on every download. S3 stores files
independently; the database holds only the S3 key (a path string). Downloads are
served directly from S3 via presigned URLs, bypassing the API entirely.

**Bucket structure:**
```
csvforge-files-{env}/
├── uploads/{user_id}/{job_id}/original.csv
└── processed/{user_id}/{job_id}/result.csv
```

The bucket has public access blocked at the account level [R-SEC-04].
All file access is via presigned URLs generated by the API after an ownership check.

**Trade-off accepted:** S3 key naming must be managed carefully. The deterministic
{user_id}/{job_id} structure makes this straightforward.

---

### 3.5 Direct Browser to S3 Upload via Presigned URL

**Problem:** A CSV file up to 50 MB must reach S3. Two routes exist:

**Option A (rejected): API proxies the upload**
Browser -> API Gateway -> Lambda -> S3. The full file travels through Lambda memory.
At 50 MB, this consumes Lambda working memory, runs into API Gateway's 10 MB HTTP API
payload limit, and charges Lambda execution time proportional to file size.

**Option B (chosen): Direct browser upload via presigned URL**
The API generates a time-limited presigned PUT URL for a specific S3 key. The browser
uploads directly to that URL. S3 receives the file; Lambda never sees the bytes.

Benefits:
- Lambda memory is not consumed by file bytes.
- Upload speed depends only on the browser connection to S3, not Lambda.
- API Gateway payload limits are irrelevant for the file itself.
- Scales to many concurrent uploads without Lambda memory pressure.

**Trade-off accepted:** The upload flow requires two API calls (intent + complete) instead
of one. The complete call is necessary because S3 does not notify the API when an upload
finishes; the browser must do so. The complete call also verifies the object actually exists
before the job is queued.

---

### 3.6 Amazon SQS

**Problem:** CSV processing is not instantaneous. Synchronous processing inside the
upload completion handler would hold the HTTP connection open and expose the user to
processing failures as upload failures.

**Why a queue rather than direct Lambda invocation?**
Direct invocation has no automatic retry, no backpressure, and no DLQ. SQS provides:
- Configurable retry with visibility timeout.
- A Dead Letter Queue after N failures.
- Decoupling: the API returns immediately; the worker operates independently.

**Why SQS not SNS?** SNS is pub/sub fanout. We have one producer and one consumer.
SQS is the correct tool for a point-to-point work queue.

**Why SQS not EventBridge?** EventBridge is a routing bus for complex event patterns.
For "here is a job ID to process," it adds unnecessary complexity.

**At-least-once delivery:** SQS guarantees delivery at least once, not exactly once.
The worker must be idempotent. See sections 5 and 7.

**Trade-off accepted:** Eventual consistency between upload confirmation and processing
start. The user sees `queued` for some seconds before processing begins. This is expected
behaviour for an async processing UX.

---

### 3.7 AWS Lambda — API (zip package)

**Why Lambda for the API:** The API is stateless request/response. Lambda with API
Gateway proxy integration is the standard serverless pattern for this. No server to manage.

**Deployment as zip:** The API has no large binary dependencies. A zip package is
simpler to build and deploy than a container image in this case.

**Trade-off accepted:** Lambda cold starts (~500ms to 2s) add latency to the first
request after inactivity. Acceptable for an MVP.
Out of MVP scope: provisioned concurrency to eliminate cold starts.

---

### 3.8 AWS Lambda — Worker (Docker container image via ECR)

**Why Lambda, not ECS/Fargate?**

Lambda is chosen because CSVForge is intentionally learning the S3 -> SQS -> Lambda
serverless pattern. The constraints are accepted as explicit MVP constraints:

| Constraint       | Lambda limit  | CSVForge at 50 MB                                                                              |
|------------------|---------------|-----------------------------------------------------------------------------------------------|
| Execution timeout | 15 minutes   | Processing time will be measured during verification. 15 minutes is the known ceiling.        |
| Memory           | Up to 10 GB   | Starting at 512 MB. Adjust based on measurement.                                              |
| Ephemeral disk   | 512 MB default | 50 MB input + 50 MB output fits comfortably.                                                  |
| Cold start       | ~500ms to 2s  | Acceptable. The user is not waiting synchronously.                                            |

If CSVForge later needs files beyond Lambda's /tmp limit or runs longer than 15 minutes,
the worker can be replaced with an ECS Fargate task with no change to the API, database,
or S3 structure. The SQS queue is the decoupling point that makes this migration possible.

**Why a Docker container image for the worker?**

Two reasons:
1. Dependency consistency. A container image captures the exact Python version, system
   libraries, and pip packages. No "works on my machine" risk.
2. Learning goal. This project explicitly demonstrates Lambda container deployment via ECR.
   The worker uses a Dockerfile, is pushed to ECR, and the Lambda function is configured
   with PackageType: Image. This is a distinct and increasingly common deployment pattern.

The API Lambda remains a zip package because it has no binary dependencies that justify
the container overhead.

**Trade-off accepted:** Container image deployments have a larger cold start penalty than
zip deployments. For async queue processing this is acceptable.

---

### 3.9 Amazon API Gateway (HTTP API)

HTTP API is simpler and cheaper than REST API. It supports Lambda proxy integration and
a native JWT authoriser backed by Cognito. REST API features (request caching, usage
plans, transformations) are not needed. The 10 MB payload limit is irrelevant because
CSV files are uploaded directly to S3.

---

### 3.10 Amazon Cognito

**Problem:** We need user authentication without building it from scratch.

**What Cognito provides:** User Pool (accounts, login, JWTs), token validation via API
Gateway JWT authoriser, token expiry and refresh via the Cognito SDK.

**What Cognito does not do:** Authorisation. Whether a user can access a specific resource
is enforced in FastAPI. See section 8.

**Trade-off accepted:** We build our own login form in React using the Cognito SDK, not
the hosted UI. More work, better UX, better learning surface.

---

### 3.11 React + TypeScript + Vite

**React:** Component model, industry standard for SPAs.
**TypeScript:** Type safety; the API response shape becomes a TypeScript type.
**Vite:** Modern build tool. Starts in milliseconds in dev mode. Create React App is
deprecated. Vite is the correct choice.

---

### 3.12 Material UI (MUI)

Pre-built accessible components. React Admin is built on MUI, so using MUI for the
user-facing UI maintains visual consistency without a second component library.

---

### 3.13 React Router

Client-side navigation without full browser reloads. Standard for React SPAs.
We use v8. Note: v5, v6, and v8 have different APIs; v8 re-exports everything from the `react-router` package (not `react-router-dom`). Check which version any example targets.

---

### 3.14 React Admin

**Use:** React Admin for the admin surface (list files, list jobs, filter by status).
Plain React + MUI + React Router for the user-facing flow (upload, status, download).

**Why not React Admin for the user flow:** The user-facing flow is a wizard-style
sequence, not a CRUD list. React Admin's abstractions fight against that shape.

**What the API must expose for React Admin:** The ra-data-simple-rest provider expects
REST conventions (_sort, _order, _start, _end params; Content-Range header in list
responses). Admin endpoints are built to match these conventions.

**Trade-off accepted:** React Admin adds a framework with its own conventions. Kept
because gaining practical React Admin experience is an explicit project goal.

---

### 3.15 Docker

**Use:**
- docker-compose.yml for local development: FastAPI + PostgreSQL + optional LocalStack.
- Dockerfile for the Worker Lambda container image pushed to ECR.

LocalStack is optional for local development. See section 9.

---

### 3.16 CloudWatch

**Logs:** Lambda functions write structured JSON logs. Job lifecycle events are logged
with job_id only — never with CSV content [R-SEC-01].
**Metrics:** Lambda emits invocation count, error count, duration. SQS emits queue depth.
**Alarm:** CloudWatch Alarm fires when DLQ ApproximateNumberOfMessagesVisible > 0
[R-OBS-03, R-REL-05].

Out of MVP scope: SNS notification from the DLQ alarm to email or webhook.

---

### 3.17 IAM

Each Lambda has a least-privilege execution role:
- API Lambda: S3 presigned URL generation, SQS SendMessage, RDS access via VPC.
- Worker Lambda: S3 GetObject (uploads/ prefix), S3 PutObject (processed/ prefix),
  event source mapping handles SQS, RDS access via VPC.

No wildcard resource (*) policies. Writing least-privilege policies is a deliberate
learning step.

---

### 3.18 CloudFormation

**Why raw CloudFormation over CDK:** CDK adds an abstraction layer that obscures which
resources are actually created. For a project whose goal is to understand AWS, raw
CloudFormation is more transparent. See section 11 for template structure.

---

### 3.19 CodePipeline + CodeBuild

Automated source -> build -> deploy pipeline. CodePipeline/CodeBuild is chosen over
GitHub Actions because the learning goal includes AWS CI/CD tooling specifically.

Out of MVP scope: multi-environment promotion (dev -> staging -> prod with approval gates).

---

### 3.20 Pytest + Vitest

**Pytest:** Python test framework. Unit tests and integration tests for the backend.
**Vitest:** Vite-native test runner for TypeScript/React.

---

### Technologies on the list NOT used in the MVP

| Technology  | Reason                                                                                         |
|-------------|-----------------------------------------------------------------------------------------------|
| CloudFront  | Not used in MVP. S3 static website hosting is the MVP/demo choice. CloudFront is the production hardening step (HTTPS, CDN, custom domain). See §12.3. |
| SNS         | Not needed for MVP. Would be target for DLQ alarm notification in production.                 |
| ECS/Fargate | Not used. Lambda is the worker. Future path if file sizes exceed Lambda limits.               |

---

## 4. Upload Flow

### 4.1 Why direct browser to S3, not API proxy

API Gateway HTTP API has a 10 MB payload limit. Even with REST API workarounds, routing
a 50 MB file through Lambda wastes Lambda memory, adds latency, and incurs unnecessary
cost. The presigned URL pattern keeps the API thin: it orchestrates but does not
transport file bytes.

### 4.2 Step-by-step

```
Step 1: Upload intent

  Browser -> POST /api/v1/uploads/intent
             Body: { "filename": "data.csv", "file_size_bytes": 45000000 }
             Header: Authorization: Bearer <access_token>

  API Gateway validates JWT. If invalid -> 401.

  FastAPI:
    a. Reads user_id and user_email from JWT claims.
    b. Validates filename extension (.csv) and file_size_bytes (<= 50 MB) [R-UPL-02, R-UPL-03].
       If invalid -> 400 with a clear error message.
    c. Generates job_id (UUID4).
    d. Constructs S3 key: uploads/{user_id}/{job_id}/original.csv
    e. Creates a Job record in PostgreSQL:
         job_id, user_id, user_email, original_filename, original_s3_key,
         status = 'pending_upload', created_at, updated_at
    f. Calls S3 generate_presigned_url for a PUT operation on the key.
       Expiry: 15 minutes. ContentType condition: text/csv.
       content-length-range condition: [0, 52428800] — S3 rejects any upload exceeding 50 MiB. (AD-27)
    g. Returns 200: { "job_id": "<uuid>", "upload_url": "<presigned PUT URL>" }

Step 2: Browser uploads directly to S3

  Browser -> PUT <presigned_upload_url>
             Body: raw CSV file bytes
             Header: Content-Type: text/csv

  S3 stores the object. The API is not involved. Lambda is not involved.

Step 3: Upload completion

  Browser -> POST /api/v1/uploads/{job_id}/complete

  FastAPI:
    a. Verifies the caller's user_id matches the job's user_id [R-USR-04].
    b. Calls S3 HeadObject on uploads/{user_id}/{job_id}/original.csv.
       If object does not exist -> 422 (upload may have failed; ask user to retry).
       If object ContentLength > 52_428_800 -> 422 (file exceeds size limit; client trust boundary). (AD-27)
    c. Updates Job status from 'pending_upload' to 'queued'.
    d. Publishes SQS message: { "job_id": "...", "s3_key": "..." }
    e. Returns 202: { "job_id": "...", "status": "queued" }

Step 4: Status polling

  Browser -> GET /api/v1/jobs/{job_id}   (every 2-3 seconds)
  FastAPI queries PostgreSQL and returns current status.

Step 5: Download

  Browser -> GET /api/v1/jobs/{job_id}/download

  FastAPI:
    a. Verifies user_id owns this job [R-DWN-03].
    b. Verifies status = 'completed'.
    c. Generates presigned GET URL for processed/{user_id}/{job_id}/result.csv.
       Expiry: 15 minutes.
    d. Returns { "download_url": "..." }

  Browser redirects to the URL. S3 serves the file directly.
```

### 4.3 The pending_upload status

After the intent call, the job exists in PostgreSQL but the file has not arrived in S3.
This window must be handled cleanly.

**Decision: pending_upload is kept as an internal database state and is never exposed
to the user as a processing status.**

The full lifecycle has two layers:

- Internal database lifecycle: `pending_upload -> queued -> processing -> completed / failed`
- User-visible processing lifecycle: `queued -> processing -> completed / failed`

`pending_upload` exists to represent the window between the intent call and a confirmed
S3 upload. It lets the database record that an upload is in progress without showing the
user a job they might think is stuck.

**Transition rules for this window:**

1. `POST /uploads/intent` creates the job record with `status = pending_upload` and
   returns `{ job_id, upload_url }`. The job exists in the database; no S3 object exists yet.

2. The browser uploads directly to S3. The API is not involved in this step.

3. `POST /uploads/{job_id}/complete` is called by the browser after the S3 PUT succeeds.
   The API performs two checks before transitioning:
   - The caller's `user_id` must match the job's `user_id`.
   - `S3 HeadObject` must confirm the expected key exists:
     `uploads/{user_id}/{job_id}/original.csv`.
     If the object is missing the API returns 422. The browser may retry the S3 upload
     and call complete again within the presigned URL's validity window.
   Only if both checks pass does the API:
   - Transition the job: `pending_upload -> queued`
   - Publish the SQS message
   - Return `202 { "job_id": "...", "status": "queued" }`

**What if the browser uploads successfully but never calls the completion endpoint?**

The job record stays in `pending_upload` indefinitely. The S3 object exists but no
SQS message is ever published, so the worker never runs. The user sees no job in their
list — `pending_upload` jobs are filtered from user-facing endpoints. `GET /api/v1/jobs`
returns only jobs with status `queued`, `processing`, `completed`, or `failed`. If the
user calls `GET /api/v1/jobs/{job_id}` for a `pending_upload` job they own, the API
returns 404 — the job does not yet exist from the user's perspective.

Consequences:
- No data corruption. The raw S3 object occupies storage but causes no system harm.
- The user would need to upload again. Their previous attempt simply never completed.
- Stale `pending_upload` rows accumulate in the database over time.

**Out of MVP scope:** A background cleanup job that expires `pending_upload` records
(and their corresponding S3 objects) older than a configurable threshold (e.g. 1 hour).
This is a known operational concern, not a correctness issue. The stale row does not
affect any running job.

---

## 5. Worker Flow

### 5.1 Message format

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "s3_key":  "uploads/us-east-1:abc123/550e8400.../original.csv"
}
```

File content is never in the message. SQS has a 256 KB message limit. The worker
fetches the file from S3 using the key.

### 5.2 Step-by-step

```
1. SQS event source mapping delivers the message to the Worker Lambda.
   Batch size = 1. One message per invocation.

2. Worker parses job_id and s3_key from the message body.

3. IDEMPOTENCY CHECK
   SELECT * FROM jobs WHERE id = <job_id>
   NOTE: user_id is read from the DB record, NOT parsed from the s3_key path.
   The s3_key is storage metadata. The database record is the authoritative identity source. (AD-28)

   IF status = 'completed' OR status = 'failed':
     -> Duplicate SQS message. Log and return successfully.
        SQS deletes the message. No processing occurs.

4. CLAIM JOB — conditional UPDATE to prevent concurrent workers:

   UPDATE jobs
   SET    status = 'processing', updated_at = NOW()
   WHERE  id = <job_id>
   AND    status IN ('queued', 'processing')

   Check rows_affected:
   - 0 rows: another worker claimed the job. Return successfully.
   - 1 row: this worker owns the job. Proceed.

   The IN ('queued', 'processing') clause is intentional. A job in 'processing'
   from a prior timed-out worker is safe to re-claim: the prior worker either
   crashed or had its Lambda timeout expire. We take ownership and proceed.

5. DOWNLOAD
   Worker calls S3 GetObject, streams the file to Lambda /tmp/{job_id}/original.csv

6. PARSE
   Worker opens the CSV with Python's csv module.

   IF parsing raises an exception (binary content, malformed CSV):
     -> PERMANENT FAILURE. See section 7.1.
     UPDATE jobs SET status='failed', failure_reason='Invalid CSV: <message>'
     Delete /tmp files.
     Return successfully. Do NOT raise an exception.
     Raising would cause SQS to retry a permanently broken file.

7. ANALYSE
   Extract: row_count, column_count, column_names [R-ANL-01]

8. CLEAN — applied in strict order [R-CLN-04]:
   a. Trim leading/trailing whitespace from all cell values.
   b. Detect duplicate rows (exact match after trimming across all columns).
   c. Remove duplicates, retaining the first occurrence.
   d. Record duplicate_rows_removed count.

9. WRITE OUTPUT
   Write processed rows to /tmp/{job_id}/result.csv
   Upload to S3: processed/{user_id}/{job_id}/result.csv
   (user_id is job.user_id from the database record fetched in step 3 — never parsed from the input s3_key)

   The S3 key is deterministic. If the worker crashes after this upload
   but before the database update, the retry is safe. See section 7.2.

10. UPDATE STATUS — single transaction:

    UPDATE jobs
    SET    status = 'completed',
           processed_s3_key = 'processed/{user_id}/{job_id}/result.csv',
           file_size_bytes = <n>,
           row_count = <n>,
           column_count = <n>,
           column_names = ARRAY[...],
           duplicate_rows_removed = <n>,
           updated_at = NOW()
    WHERE  id = <job_id>

11. CLEANUP — delete /tmp files to avoid exhaustion across warm Lambda invocations.

12. Return successfully. SQS event source mapping deletes the message.
```

### 5.3 Why batch size = 1

With batch size > 1, if Lambda raises an exception SQS retries the entire batch,
including messages that already succeeded. With batch size = 1, each message is
independent. A failure affects exactly one job. Cost trade-off is negligible at MVP scale.

---

## 6. Database Model

### 6.1 Single jobs table

One upload creates one job. No separate files table for the MVP. If future requirements
introduce re-processing a file with different settings, a files table becomes warranted.

```sql
CREATE TABLE jobs (
    id                     UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                VARCHAR(128) NOT NULL,
    user_email             VARCHAR(255) NOT NULL,
    original_filename      VARCHAR(255) NOT NULL,
    original_s3_key        VARCHAR(512) NOT NULL,
    processed_s3_key       VARCHAR(512),
    status                 VARCHAR(32)  NOT NULL DEFAULT 'pending_upload',
    failure_reason         TEXT,
    file_size_bytes        BIGINT,
    row_count              INTEGER,
    column_count           INTEGER,
    column_names           TEXT[],
    duplicate_rows_removed INTEGER,
    created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_jobs_user_id    ON jobs(user_id);
CREATE INDEX idx_jobs_status     ON jobs(status);
CREATE INDEX idx_jobs_created_at ON jobs(created_at DESC);
```

user_id is the Cognito sub claim. user_email is stored at upload time for admin display
without requiring a Cognito Admin API call per row.

### 6.2 State machine

The database has five states. The user sees four. `pending_upload` is internal only.

```
Internal database lifecycle:

  [pending_upload]
        |
        |  POST /uploads/{job_id}/complete
        |  guard: caller owns job AND S3 object confirmed via HeadObject
        v
    [queued]  <-- first user-visible state
        |
        |  Worker: conditional UPDATE
        |  WHERE status IN ('queued', 'processing')
        v
   [processing]
        |
        +-- S3 write + DB UPDATE succeed  --> [completed]
        |
        `-- permanent validation failure  --> [failed]

User-visible processing lifecycle (pending_upload never shown):

    queued --> processing --> completed
                         `-> failed
```

State transition rules:

| Transition               | Performed by            | Guard                                                             |
|--------------------------|-------------------------|-------------------------------------------------------------------|
| pending_upload -> queued | Completion API endpoint | Caller user_id matches job record; S3 HeadObject confirms object  |
| queued -> processing     | Worker Lambda           | Conditional UPDATE: status IN ('queued','processing'), rows_affected = 1 |
| processing -> completed  | Worker Lambda           | S3 write succeeded; DB UPDATE succeeded                           |
| processing -> failed     | Worker Lambda           | Permanent failure (invalid CSV, unrecoverable error)              |

No backward transitions. `completed` and `failed` are terminal.
The completion endpoint is the only code path that may transition `pending_upload -> queued`.
The worker is the only code path that may transition `queued -> processing -> completed/failed`.

**API behaviour for pending_upload jobs:**
User-facing endpoints (`GET /jobs`, `GET /jobs/{id}`) do not surface `pending_upload` rows.
A job only becomes visible to the user after the completion call transitions it to `queued`.

---

## 7. Failure and Retry Behavior

### 7.1 Permanent vs transient failures

The core of correct SQS consumer design: control whether a failure is retried by
controlling whether the Lambda raises an exception.

| Failure type          | Example                          | Retryable? | Worker action                                       |
|-----------------------|----------------------------------|------------|-----------------------------------------------------|
| Invalid CSV           | Binary file, malformed CSV       | No         | UPDATE status=failed, then return normally          |
| Transient S3 error    | S3 throttling                    | Yes        | Raise exception -> SQS retries                      |
| Transient DB error    | RDS connection refused           | Yes        | Raise exception -> SQS retries                      |
| Unhandled bug         | Worker code throws unexpectedly  | Yes (until fixed) | Exception propagates -> retry -> DLQ         |

**Permanent failure:** The worker must NOT raise an exception. It updates the job to
failed with a human-readable reason [R-VAL-02], then returns normally. Raising here
would trigger up to 3 retries of a permanently broken file [R-VAL-03].

**Transient failure:** The worker MUST raise an exception. SQS retries after the
visibility timeout expires, giving the transient condition time to resolve.

### 7.2 The crash window: S3 write succeeds, PostgreSQL update fails

**Scenario:** Worker writes processed/{user_id}/{job_id}/result.csv to S3 successfully.
The subsequent database UPDATE raises a connection error. The Lambda raises an exception.
SQS retries the message.

**On retry:**
1. Idempotency check (step 3): status is still 'processing'. Worker proceeds.
2. Conditional UPDATE (step 4): status is 'processing', rows_affected = 1. Worker proceeds.
3. Worker downloads original CSV from S3 again.
4. Worker re-processes and writes to processed/{user_id}/{job_id}/result.csv.
   Because the output key is deterministic (fixed to {user_id}/{job_id}), this write
   replaces the same S3 object that was created in the failed attempt. No additional
   orphaned objects are created; the retry lands in exactly the same place.
5. Worker attempts the database UPDATE again. If it succeeds, job is completed.

**Why write S3 before the database?**
S3 is not a transactional participant. You cannot roll back an S3 write if a DB write
fails. Writing S3 first means: if the DB write fails, the retry replaces the same S3
object — harmless. If you wrote to the DB first and then S3 failed, you would have a
`completed` record in the database with no corresponding output file, which is a worse
and harder-to-recover inconsistency.

**Why the deterministic key makes this safe:**
A non-deterministic key (e.g. appending a timestamp or random suffix) would create a
distinct second S3 object on every retry while the previous attempt's object remained
orphaned. Using the fixed key `processed/{user_id}/{job_id}/result.csv` means every
attempt — original or retry — writes to the same object location. The final successful
state is the same regardless of how many attempts it took to get there.

### 7.3 DLQ behavior [R-REL-04, R-REL-05]

SQS configuration:

| Parameter            | Value        | Reason                                                                                                                                                  |
|----------------------|--------------|---------------------------------------------------------------------------------------------------------------------------------------------------------|
| Lambda Timeout       | 300s (5 min) | Starting value. Processing a 50 MB CSV is I/O and CPU bound. Adjust based on measurement. |
| Visibility timeout   | **1800s (30 min)** | Must be ≥ 6× Lambda Timeout per AWS recommendation: 6 × 300s = 1800s. If Lambda execution regularly approaches 300s, both values must be increased together. See AD-17. |
| Message retention    | 4 days       | How long a message survives if not processed.                                               |
| Max receive count    | **5**        | AWS recommends ≥ 5 before routing to DLQ. Revised from original 3. See AD-17.              |
| DLQ                  | csv-jobs-dlq | Separate SQS queue.                                                                         |

**Why timeout and visibility timeout must not be equal:**
If the Lambda Timeout and SQS VisibilityTimeout are the same value, the following race
can occur: Lambda begins processing, runs for exactly the timeout duration, is forcibly
terminated by Lambda, and the visibility timeout expires at the same instant — causing
SQS to re-deliver the message while the previous Lambda invocation is still technically
in its termination sequence. Setting visibility timeout strictly greater than Lambda
Timeout eliminates this race. The CloudFormation configuration for both values is kept
in the same template section (`worker.yaml`) so they are reviewed together.

A CloudWatch Alarm monitors ApproximateNumberOfMessagesVisible on the DLQ.
When it exceeds 0, the alarm fires [R-OBS-03].

For the MVP this is visible in the CloudWatch console.
Out of MVP scope: SNS notification to email or webhook.

**What a DLQ message means:** Something failed that retries could not fix. Likely a bug
in the worker, an IAM permission error, or an unrecoverable infrastructure issue.
The operator inspects the DLQ, fixes the root cause, and replays the messages
(re-drives from DLQ to main queue) once resolved.

### 7.4 Idempotency summary [R-REL-03]

The worker is idempotent because:
1. Status check before processing: completed or failed jobs are skipped.
2. Conditional UPDATE: only one worker can claim a job; a second exits cleanly.
3. Deterministic S3 output key: re-writing is safe and produces identical content.
4. Database UPDATE is the final step: failure there triggers a safe retry.

---

## 8. Authentication and Authorization

### 8.1 Authentication — Cognito User Pool

**Login flow:**

```
1. User enters email + password in the React login form.
2. Frontend calls Cognito SDK: initiateAuth(email, password).
3. Cognito validates and returns:
   - Access Token (used to call the API; contains cognito:groups claim)
   - ID Token (contains user attributes: sub, email)
   - Refresh Token (long-lived; for silent re-auth)
4. Frontend holds tokens in React component state (in-memory).
5. Every API request: Authorization: Bearer <access_token>
6. API Gateway JWT Authoriser validates the token signature against Cognito JWKS.
   Invalid or expired -> 401 before FastAPI is invoked.
7. FastAPI reads user_id from the decoded JWT claims passed by API Gateway.
```

**Token storage:** In-memory (React state). Tokens are lost on page refresh; the user
must log in again. This is the most secure option (no XSS risk from localStorage).
Out of MVP scope: silent token refresh via Refresh Token in an HttpOnly cookie.

### 8.2 Authorization — FastAPI enforces resource ownership

API Gateway establishes identity. FastAPI enforces data-level rules:

| Rule                                    | Enforcement                                                               |
|-----------------------------------------|---------------------------------------------------------------------------|
| User can only view their own jobs       | SELECT ... WHERE id = ? AND user_id = ? -> 404 if not found              |
| User can only download their own files  | Same ownership check before generating presigned URL                      |
| Only admins can list all jobs           | FastAPI checks cognito:groups claim for csvforge-admins group             |
| Non-admins on admin endpoints           | 403 Forbidden                                                             |

**Admin group:** Created in the Cognito User Pool. Membership assigned manually in the
Cognito console for the MVP.

### 8.3 Security requirements map

| Requirement | Implementation                                                                     |
|-------------|------------------------------------------------------------------------------------|
| R-AUTH-01   | API Gateway JWT authoriser on all protected routes                                 |
| R-AUTH-02   | JWT authoriser returns 401 before FastAPI runs                                     |
| R-SEC-01    | Worker and API never log CSV content; only job_id and status                       |
| R-SEC-02    | .env files in .gitignore; secrets in environment variables                         |
| R-SEC-03    | DB password in Secrets Manager; Lambda fetches at runtime                          |
| R-SEC-04    | S3 bucket: PublicAccessBlockConfiguration all true                                 |
| R-UPL-06    | No public S3 bucket policy; files accessed only via presigned URLs                 |
| R-DWN-03    | Presigned URL generated only after ownership check                                 |

---

## 9. Local Development Approach

### 9.1 Goals

Local development should let a developer run the FastAPI API and PostgreSQL, upload a
file, trigger processing, and see results. It should not require fighting with emulators.

### 9.2 Core local stack

docker-compose.yml starts:

| Service    | Port | Purpose                                    |
|------------|------|--------------------------------------------|
| api        | 8000 | FastAPI with hot-reload (uvicorn --reload) |
| postgres   | 5432 | PostgreSQL (same schema as RDS)            |

Environment variables are loaded from a .env file (listed in .gitignore):

```
DATABASE_URL=postgresql://csvforge:password@postgres:5432/csvforge
ENVIRONMENT=local
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
AWS_DEFAULT_REGION=us-east-1
```

### 9.3 S3 and SQS in local development

Neither option below is a prerequisite for the two-day MVP. Both are implementation
choices for the developer's local workflow. The test suite and CI pipeline do not
depend on a specific local S3/SQS setup.

**Option A: LocalStack**

LocalStack emulates S3 and SQS locally. When `AWS_ENDPOINT_URL=http://localstack:4566`
is set, boto3 routes all calls to LocalStack without any code changes. LocalStack can
be added as an optional service in `docker-compose.yml` and started only when needed.

Choose this if: you want to run the full upload-to-download flow locally without
an AWS account or without incurring AWS costs.

**Option B: Real AWS dev account**

Use a real AWS dev account with dedicated dev S3 bucket and SQS queue. Environment
variables point to real AWS resources. No emulator to configure or maintain.

Choose this if: you already have an AWS dev account and prefer to work against the
real services for accurate behaviour without emulator edge cases.

Both options work. The application code is identical for either; only environment
variables differ.

### 9.4 Running the worker locally

The worker Lambda can be run as a plain Python script:

```
python worker/handler.py --job-id <uuid>
```

This reads from PostgreSQL and S3 (LocalStack or real AWS), processes, and writes
results back. No Lambda infrastructure required for development and unit testing.

### 9.5 Authentication bypass for local development

When ENVIRONMENT=local, FastAPI accepts a X-Dev-User-Id header instead of validating
a JWT. This header is only accepted in the local environment. The Cognito authentication
flow is tested against real Cognito in CI.

### 9.6 Database migrations

Managed by Alembic (SQLAlchemy's migration tool). Migrations run automatically on
startup in local development. In production, migrations run as a CodeBuild step before
the Lambda deployment is updated.

---

## 10. Testing Strategy

### 10.1 Layers

```
Unit tests (fast, no external services)
|
+-- Backend (pytest)
|   +-- CSV parsing: valid, invalid, edge cases (empty, header-only, all duplicates,
|   |   Unicode, mixed line endings)
|   +-- Cleaning logic: trim, dedup, ordering, duplicate_rows_removed count
|   +-- Pydantic validation: file size limit, extension check
|   `-- Worker idempotency (mock S3 + DB with moto + pytest fixtures):
|       +-- completed job -> skip
|       +-- failed job -> skip
|       +-- concurrent claim -> conditional UPDATE returns 0 rows -> skip
|       `-- crash window -> S3 write succeeds, DB fails -> retry is safe
|
`-- Frontend (vitest)
    +-- File picker: validates extension and size before calling intent endpoint
    +-- Status badge: correct label per status string
    `-- Download button: only rendered when status = 'completed'

Integration tests (slower, require PostgreSQL + S3)
|
+-- Upload flow: POST intent -> PUT to S3 -> POST complete -> job is queued
+-- Worker: invoke handler with real S3 file -> job is completed -> output exists
+-- Download: GET /download returns presigned URL for completed job
+-- Auth: unauthenticated request -> 401; wrong user -> 404
`-- DLQ: force worker to fail 3 times -> message appears in DLQ
```

### 10.2 What we test vs what we trust

We test our code: CSV logic, worker state machine, API routing, auth enforcement.
We do not test AWS services themselves. We mock them with moto in unit tests and use
them directly (LocalStack or real AWS) in integration tests.

### 10.3 CI test execution order

1. pytest tests/unit/   (no Docker required)
2. docker compose up -d  (start LocalStack + PostgreSQL)
3. pytest tests/integration/
4. docker compose down
5. npm run test --run   (vitest, single pass)

Build fails if any test fails. Deployment is blocked on build failure.

### 10.4 Coverage goals for MVP

- CSV processing logic: 100% line coverage (pure Python, no external dependencies).
- API endpoints: happy path + primary error paths (400, 401, 403, 404, 422).
- Worker: happy path + permanent failure + idempotency paths.

---

## 11. Infrastructure as Code

### 11.1 CloudFormation template structure

A small set of flat templates rather than a large nested-stack hierarchy.
Nested stacks are used only where cross-stack output references require separation.

```
infrastructure/
├── network.yaml    # VPC, subnets, security groups
├── data.yaml       # RDS PostgreSQL + S3 bucket
├── queues.yaml     # SQS main queue + DLQ + CloudWatch alarm
├── auth.yaml       # Cognito User Pool + App Client
├── api.yaml        # API Lambda + API Gateway + IAM role
├── worker.yaml     # Worker Lambda (container) + ECR repo + event source mapping
└── deploy.sh       # Deploys stacks in dependency order, passing outputs as params
```

Each template is independently deployable given the right parameters. deploy.sh
is a thin shell script, not a framework. This keeps the infrastructure understandable
and allows targeted deploys (change the worker without touching the database).

### 11.2 Key resource configuration

**S3 bucket (data.yaml)**
```yaml
PublicAccessBlockConfiguration:
  BlockPublicAcls: true
  BlockPublicPolicy: true
  IgnorePublicAcls: true
  RestrictPublicBuckets: true
```


**S3 CORS configuration (data.yaml)**
```yaml
CorsConfiguration:
  CorsRules:
    - AllowedOrigins:
        - "http://localhost:5173"      # Vite dev server
        - !Sub "http://${FrontendBucket}.s3-website-${AWS::Region}.amazonaws.com"
      AllowedMethods: ["PUT"]
      AllowedHeaders: ["Content-Type", "*"]
      MaxAgeSeconds: 3000
      ExposedHeaders: []
```
Required because the browser performs a direct PUT to S3 from a different origin.
Without this, the browser's preflight OPTIONS request is rejected by S3 with a CORS error.

**API Gateway CORS (api.yaml)**
API Gateway HTTP API has built-in CORS support. Configure:
- AllowOrigins: `http://localhost:5173` (dev), production frontend URL
- AllowHeaders: `Authorization`, `Content-Type`
- AllowMethods: `GET`, `POST`, `OPTIONS`
- ExposeHeaders: `Content-Range` (required by React Admin ra-data-simple-rest)

**FastAPI CORSMiddleware (backend/main.py)**
Required for local development when Vite (port 5173) calls FastAPI (port 8000):
```python
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range"],
)
```
In production, CORS is handled at API Gateway level; the FastAPI middleware is only active locally.

**SQS (queues.yaml)**
```yaml
VisibilityTimeout: 1800         # 30 min — 6× Lambda Timeout (300s); AWS recommendation; see §7.3 and AD-17
MessageRetentionPeriod: 345600  # 4 days
RedrivePolicy:
  deadLetterTargetArn: !GetAtt DLQ.Arn
  maxReceiveCount: 5            # AWS recommends ≥5; revised from original 3; see AD-17
```

**Worker Lambda (worker.yaml)**
```yaml
PackageType: Image
ImageUri: !Sub "${AWS::AccountId}.dkr.ecr.${AWS::Region}.amazonaws.com/csvforge-worker:${ImageTag}"
MemorySize: 512     # starting point; adjust based on measurement
Timeout: 300        # 5 minutes — starting value; see §7.3 for timeout/visibility relationship
```

**API Lambda (api.yaml)**
```yaml
PackageType: Zip
Runtime: python3.14
MemorySize: 256
Timeout: 30
```

### 11.3 Secrets

- RDS password: AWS::SecretsManager::Secret. Lambda reads the ARN from an environment
  variable and fetches the value at runtime.
- Cognito User Pool ID and Client ID: public configuration, passed as Lambda environment
  variables. Not secrets.
- S3 bucket name, SQS queue URL: passed as Lambda environment variables via
  CloudFormation cross-stack outputs.

---

## 12. CI/CD Flow

### 12.1 Pipeline stages

```
Developer pushes to main branch
|
v
CodePipeline Source Stage
  Source: GitHub via CodeConnection (or CodeCommit)
  Artifact: source zip
|
v
CodePipeline Build Stage (CodeBuild)
  buildspec.yml:

  install:
    - pip install -r backend/requirements.txt
    - npm ci

  pre_build:
    - docker compose up -d postgres localstack
    - Wait for PostgreSQL healthy (pg_isready)

  build:
    - pytest tests/unit/
    - pytest tests/integration/
    - npm run test --run
    - docker build -t csvforge-worker ./worker/
    - aws ecr get-login-password | docker login ...
    - docker tag csvforge-worker <ECR_URI>:$CODEBUILD_BUILD_ID
    - docker push <ECR_URI>:$CODEBUILD_BUILD_ID
    - npm run build   (Vite production build -> dist/)
    - aws s3 sync dist/ s3://csvforge-frontend-<env>/

  post_build:
    - docker compose down

  on_failure: ABORT
|
v
CodePipeline Deploy Stage
  aws cloudformation deploy \
    --stack-name csvforge-worker \
    --template-file infrastructure/worker.yaml \
    --parameter-overrides ImageTag=$CODEBUILD_BUILD_ID \
    --capabilities CAPABILITY_IAM

  aws cloudformation deploy \
    --stack-name csvforge-api \
    --template-file infrastructure/api.yaml \
    --capabilities CAPABILITY_IAM
```

### 12.2 On failure

- Test failure: CodeBuild exits non-zero. Pipeline stops. Current deployed version
  remains in service.
- CloudFormation failure: CloudFormation rolls back to the previous stack state automatically.

### 12.3 Frontend deployment

Vite builds to `dist/`. CodeBuild syncs `dist/` to an S3 bucket configured for
static website hosting.

**S3 static website hosting is the MVP and demo deployment choice.** It is simple to
configure, has no additional infrastructure, and is sufficient for a non-public
development or demo environment. It is not the production frontend architecture:

- S3 static website endpoints are HTTP-only (no TLS).
- They do not support custom domains with managed certificates without CloudFront.
- They do not provide CDN edge caching or DDoS mitigation.

**Future hardening (out of MVP scope):** Replace S3 static website hosting with a
CloudFront distribution using a private S3 origin (Origin Access Control). This adds
HTTPS, CDN caching, a custom domain, and removes direct public S3 access. It is a
well-understood pattern and does not require changes to the application code — only
to the CloudFormation infrastructure and the CI/CD sync target.

---

## 13. Architectural Decisions

These decisions are the canonical record for this design. Copy confirmed decisions
to docs/DECISIONS.md.

| #     | Decision                                               | Rationale                                                                                   | Trade-off                                                        |
|-------|--------------------------------------------------------|---------------------------------------------------------------------------------------------|------------------------------------------------------------------|
| AD-01 | Lambda is the CSV worker                               | Intentionally learning S3->SQS->Lambda; 50 MB fits within Lambda constraints for MVP       | 15-minute ceiling; 50 MB cap is an explicit MVP constraint       |
| AD-02 | Direct browser -> S3 upload via presigned URL          | Avoids routing file bytes through Lambda; avoids API Gateway payload limits                 | Two-step upload flow; must verify S3 object on completion call   |
| AD-03 | React Admin for admin surface only                     | Practical React Admin experience; value on list/filter screens                              | Learning curve; different conventions from plain React           |
| AD-04 | PostgreSQL (RDS) for job state                         | SQL queries, ACID transactions, admin filters without GSI overhead                          | VPC setup; higher minimum cost than DynamoDB                     |
| AD-05 | Worker Lambda deployed as Docker container via ECR     | Demonstrates Lambda container deployment; consistent runtime                                | Larger cold starts; ECR setup required                           |
| AD-06 | API Lambda deployed as zip package                     | No large binary dependencies; simpler build                                                 | None for this use case                                           |
| AD-07 | SQS batch size = 1                                     | Clean per-message retry semantics; no partial batch failure complexity                      | More Lambda invocations at scale (irrelevant for MVP)            |
| AD-08 | Worker idempotency via status check + conditional SQL  | Handles SQS at-least-once delivery safely; PostgreSQL-idiomatic                             | Slightly more complex worker logic; worth the correctness        |
| AD-09 | Raw CloudFormation (not CDK)                           | Educational transparency; direct AWS resource model                                         | Verbose YAML                                                     |
| AD-10 | In-memory token storage                                | No XSS risk from localStorage                                                               | Tokens lost on page refresh                                      |
| AD-11 | No CloudFront in MVP                                   | Not needed for a 2-day MVP                                                                  | No HTTPS on frontend unless separately configured                |
| AD-12 | LocalStack is optional                                 | Keeps local dev simple; real AWS dev account is equally valid                               | Emulator divergence risk if LocalStack is used                   |
| AD-13 | pending_upload as an internal-only database state      | Cleanly models the intent-to-queued window; filtered from all user-facing API responses     | Fifth database state; stale records if browser never calls complete — cleanup is out of MVP scope |
| AD-14 | Write S3 before PostgreSQL in worker                   | Crash after S3 write is recoverable via retry; crash after DB write with no S3 is not      | Deterministic key required for safe overwrites                   |

---

## 14. Unresolved Questions

**Resolved decisions:**

| #     | Question                                          | Resolution                                                                                      |
|-------|---------------------------------------------------|-------------------------------------------------------------------------------------------------|
| UQ-01 | pending_upload: five database states or four?     | **Resolved.** Five states. `pending_upload` is internal and never user-visible. See §4.3, §6.2. |

**Open questions:**

| #     | Question                                                                         | Default in this design            | Decision needed?                           |
|-------|----------------------------------------------------------------------------------|-----------------------------------|--------------------------------------------|
| UQ-02 | File retention: how long should uploads and results be kept in S3?               | No lifecycle policy for MVP       | Low priority; flag for production          |
| UQ-03 | CI/CD source: GitHub via CodeConnection or CodeCommit?                           | GitHub via CodeConnection         | Yes — depends on where the repo lives      |
| UQ-04 | Frontend HTTPS: S3 static website is HTTP only. Acceptable for dev/staging?     | HTTP only for MVP                 | Acceptable for a private dev/staging env   |
| UQ-05 | Status updates: polling vs WebSocket/SSE                                         | Polling every 2-3 seconds for MVP | Polling is correct for MVP; note for later |
| UQ-06 | Admin bootstrap: first admin assigned manually in Cognito console                | Manual for MVP                    | Yes — document the manual step             |

---

## 15. Risks

| Risk                                                          | Likelihood   | Impact | Mitigation                                                                      |
|---------------------------------------------------------------|--------------|--------|---------------------------------------------------------------------------------|
| Processing time for 50 MB CSV approaches visibility timeout   | Low-Medium   | Medium | Measure during verification; increase Timeout and VisibilityTimeout if needed   |
| RDS connection pool exhaustion under Lambda concurrency       | Low for MVP  | High   | Out of MVP scope: RDS Proxy. Lambda concurrency is low at MVP scale.            |
| LocalStack diverges from real AWS behaviour                   | Medium       | Medium | Run one integration pass against a real AWS dev account before demo             |
| Worker crashes after S3 write, before DB update               | Low          | Low    | Covered by deterministic S3 key + safe retry design (section 7.2)              |
| pending_upload records accumulate if browser never calls complete | Low       | Low    | No data corruption; stale rows only. Cleanup job out of MVP scope.              |
| Cognito User Pool accidentally deleted                        | Very low     | High   | Enable DeletionProtection in CloudFormation template                            |
| DB password visible in Lambda console if stored as env var    | Medium       | High   | Use Secrets Manager; Lambda fetches at runtime via SDK                          |
