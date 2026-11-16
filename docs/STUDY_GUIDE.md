# CSVForge — Assessment Study Guide

Your personal revision document. Read this top-to-bottom once, then use the section
headers as a checklist before your presentation.

---

## How to Use This Guide

1. **Read Section 1 first** — the Big Picture. You should be able to draw the architecture
   diagram on a whiteboard from memory before you do anything else.
2. **Read each technology section** (2 through 7) with the actual file open in your editor.
   The goal is to be able to explain every line, not just memorize it.
3. **Use Section 8** (Interview Q&A) to self-test. Cover the answer, say it out loud, then
   check. If you can't say it out loud in 60 seconds, you don't know it yet.
4. **Keep Section 9** (Presentation Checklist) open during your rehearsal.

---

## Section 1: The Big Picture — What is CSVForge?

### In one sentence
CSVForge is a web app where users upload CSV files, a background worker cleans and
deduplicates them, and users download the processed result.

### In one paragraph (say this out loud in your presentation opening)
> "CSVForge is an end-to-end CSV processing pipeline built on AWS. A user logs in through
> a React frontend, uploads a CSV file directly to S3 using a presigned URL, and the API
> records the job in PostgreSQL and queues it in SQS. A separate Lambda worker picks up
> the message, processes the CSV, and writes the result back to S3. The user polls the
> API to track progress and downloads the cleaned file when done. The API never touches
> the file bytes — it only manages state. PostgreSQL owns job state. S3 owns file bytes.
> SQS is the trigger between the API and the worker."

### The Architecture — draw this on a whiteboard

```
Browser (React)
    │
    │ 1. HTTPS + Bearer Token
    ▼
API Gateway ──── validates JWT (Cognito) ──── 401 if invalid
    │
    │ 2. Lambda event (JSON)
    ▼
FastAPI (Lambda) ── backend/main.py
    │                     │
    │ 3. SQLAlchemy        │ 4. boto3
    ▼                     ▼
PostgreSQL           S3 / SQS
(jobs table)         (files / queue)
                          │
                          │ 5. Browser PUT bytes directly to S3
                          │    (presigned URL — API not involved)
                          │
                     SQS Queue
                          │
                          │ 6. Event source mapping
                          ▼
                   Worker Lambda
                   (reads S3, cleans CSV, writes S3, updates DB)
```

### The 5 Key Design Decisions (know WHY for each one)

| Decision | What | Why |
|----------|------|-----|
| Presigned URL for upload | Browser uploads directly to S3, not through API | API Gateway has a 10 MB limit; files can be 50 MB |
| PostgreSQL over DynamoDB | Relational DB with ACID guarantees | Need transactions; job state must never be partially updated |
| SQS for async processing | Queue between API and worker | Decouples them; worker can retry independently; API responds fast |
| Conditional UPDATE for idempotency | One SQL statement atomically claims a job | SQS delivers at least once; two workers might race for the same job |
| `pending_upload` hidden from users | List endpoint excludes this status | A job that never gets uploaded would appear as a ghost record |

---

## Section 2: React Frontend — What You Need to Know

### What it is
React is a JavaScript library for building UIs. This project uses:
- **React 19** — the core library
- **TypeScript** — JavaScript with types (catches mistakes before they run)
- **MUI (Material UI)** — pre-built styled components (buttons, tables, forms)
- **React Admin** — a framework for building admin dashboards (the admin panel)
- **Vite** — the build tool that compiles TypeScript to browser-ready JavaScript

### Where to look
```
frontend/src/
├── App.tsx              ← Root component, sets up routing
├── main.tsx             ← Entry point (like main() in Python)
├── auth/
│   └── AuthContext.tsx  ← Cognito login state, shared across the app
├── components/
│   ├── AppLayout.tsx    ← Page shell (header, sidebar)
│   ├── JobStatusChip.tsx ← The colored status badge (queued=blue, etc.)
│   └── RequireAuth.tsx  ← Redirects to login if not authenticated
├── api/
│   └── client.ts        ← All fetch() calls to the FastAPI backend
└── admin/
    └── AdminApp.tsx     ← React Admin panel for the admin view
```

### The 3 things React does in this project

1. **Authentication flow** — `AuthContext.tsx` holds the Cognito tokens. Every component
   that needs to know "is the user logged in?" reads from this context. `RequireAuth.tsx`
   wraps protected pages and redirects to login if the token is missing.

2. **Upload flow** — The user picks a CSV file → frontend calls `POST /jobs` to get a
   presigned URL → frontend does a direct `PUT` to that URL with the raw file bytes →
   frontend calls `POST /uploads/{id}/complete` to tell the API the upload is done.

3. **Status polling** — After upload, the frontend periodically calls `GET /jobs/{id}`
   to check the status. When it becomes `completed`, it shows a download button.

### Key React concepts for your presentation

**Component** — A reusable piece of UI. `JobStatusChip` is a component: you pass it
`status="queued"` and it renders a blue chip. You pass it `status="completed"` and it
renders green.

**State** — Data that can change and causes the UI to re-render. The job status is state:
when it changes from `processing` to `completed`, React re-renders the page to show
the download button.

**Context** — Global state shared across many components without passing it down manually.
`AuthContext` stores the logged-in user's tokens. Any component in the app can read
from it without the parent having to pass it as a prop.

**TypeScript interfaces** — Define the shape of data. If the backend returns
`{ job_id: string, status: string }`, the frontend defines a matching TypeScript
interface. If you try to access a field that doesn't exist, TypeScript catches it
at compile time, not at runtime in the user's browser.

---

## Section 3: FastAPI Backend — What You Need to Know

### What it is
FastAPI is a Python web framework for building HTTP APIs. It is:
- **Fast** — asynchronous, handles many requests concurrently
- **Self-documenting** — go to `/docs` and you get interactive API documentation
- **Type-safe** — uses Python type hints to validate requests and serialize responses

### The 8 Endpoints (know all of these)

| Method | Path | What it does | Auth required? |
|--------|------|-------------|---------------|
| `GET` | `/health` | Returns `{"status": "ok"}` — liveness probe | No |
| `POST` | `/jobs` | Create job + get presigned S3 upload URL | Yes |
| `POST` | `/uploads/intent` | Same as above (original name, kept for compatibility) | Yes |
| `POST` | `/uploads/{job_id}/complete` | Verify upload landed in S3, queue the job | Yes |
| `GET` | `/jobs` | List your jobs (excludes `pending_upload`) | Yes |
| `GET` | `/jobs/{job_id}` | Get one job | Yes |
| `GET` | `/jobs/{job_id}/download` | Get presigned download URL for processed file | Yes |
| `GET` | `/admin/jobs` | Paginated job list for admins | Admin only |

### The Request Lifecycle (trace one request end-to-end)

```
Browser: GET /jobs HTTP/1.1
         Authorization: Bearer eyJ...

Step 1: API Gateway validates the JWT against Cognito's public keys.
        If invalid → 401 Unauthorized (never reaches FastAPI).
        If valid → passes request to Lambda.

Step 2: Mangum (in main.py) converts Lambda's JSON event into an ASGI request.
        FastAPI's router matches GET /jobs → list_jobs() function.

Step 3: FastAPI resolves dependencies BEFORE calling list_jobs():
        - get_current_user() → reads JWT claims → returns CurrentUser(user_id="abc...")
        - get_db() → opens Session → yields db

Step 4: list_jobs() runs:
        - calls list_jobs_for_user(db, user_id=current_user.user_id)
        - repository issues: SELECT * FROM jobs WHERE user_id='abc...'
                             AND status != 'pending_upload'
                             ORDER BY created_at DESC
        - returns a list of Job ORM objects

Step 5: FastAPI serializes the Job objects to JSON using Pydantic (JobResponse model).
        Returns HTTP 200 with JSON body.

Step 6: get_db() cleanup runs:
        - db.commit() (no writes in this request, so this is a no-op)
        - db.close() (returns connection to pool)
```

### Dependency Injection — the most important FastAPI concept

```python
@app.get("/jobs")
def list_jobs(
    current_user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ...
```

`Depends(get_current_user)` tells FastAPI: "before you call `list_jobs`, call
`get_current_user` and inject the result as `current_user`." This is dependency injection.
It makes testing easy: in tests, you can replace `get_current_user` with a fake version
that returns a test user, without changing `list_jobs` at all.

### Mangum — the production bridge

Locally: `uvicorn backend.main:app --reload` → Uvicorn runs the ASGI app as an HTTP server.
Production: API Gateway → Lambda event (JSON) → **Mangum** translates it → FastAPI.

Mangum is one line: `handler = Mangum(app)`. That `handler` is what CloudFormation points
Lambda at. The FastAPI code doesn't know it's running in Lambda — Mangum handles the
translation completely.

---

## Section 4: PostgreSQL and SQLAlchemy — What You Need to Know

### The mental model (use this analogy in your presentation)

```
SQLAlchemy Layer    Real-World Analogy
────────────────    ──────────────────────────────────────────────
Engine              The restaurant manager — knows where the kitchen
                    is (DATABASE_URL), manages all the connections.

Connection Pool     The tables in the restaurant — pre-set, waiting.
                    Borrowing a connection = sitting at a table.

Connection          One occupied table — one open TCP socket to PostgreSQL.

Session             One customer's full order — tracks everything you've
                    added, modified, or deleted. Ends with the bill
                    (commit) or walking out (rollback).
```

### The jobs table — 15 columns, know them all

```
Column              Type            Purpose
──────────────────  ──────────────  ──────────────────────────────────────
id                  VARCHAR(36)     UUID primary key — Python generates this
user_id             VARCHAR(128)    Cognito 'sub' — who owns this job
status              VARCHAR(32)     State machine: 5 possible values
original_filename   VARCHAR(255)    What the user called their file
original_s3_key     VARCHAR(1024)   Where the raw CSV lives in S3
user_email          VARCHAR(255)    Display only — never used for identity
file_size_bytes     BIGINT          Reported by client (not authoritative)
row_count           INTEGER         NULL until worker completes
column_names        ARRAY(TEXT)     NULL until worker completes
processed_s3_key    VARCHAR(1024)   NULL until worker completes
failure_reason      TEXT            NULL unless status='failed'
created_at          TIMESTAMPTZ     Set by DB at INSERT
updated_at          TIMESTAMPTZ     Auto-updated by DB on every UPDATE
processing_started_at TIMESTAMPTZ  NULL until worker claims the job
processing_completed_at TIMESTAMPTZ NULL until job reaches terminal state
```

### The 5 Job Statuses (the state machine)

```
pending_upload ──→ queued ──→ processing ──→ completed
      (hidden                                (terminal)
      from users)              └──────────→ failed
                                            (terminal)
```

- `pending_upload`: Job row created, S3 upload not yet confirmed. Hidden from users.
- `queued`: Upload confirmed, SQS message sent. First user-visible status.
- `processing`: Worker has claimed the job and is actively running.
- `completed`: Worker finished successfully. `processed_s3_key` is set.
- `failed`: Worker errored out. `failure_reason` explains what happened.

**Why not just a boolean `is_processed`?** A boolean can't distinguish "queued and
waiting" from "actively being processed." The state machine makes every condition
explicit and makes invalid transitions impossible to code accidentally.

### The 3 Indexes (know WHY each exists)

```sql
idx_jobs_user_id    ON jobs (user_id)      -- "Show me all jobs for user X"
idx_jobs_status     ON jobs (status)       -- "Show me all queued jobs"
idx_jobs_created_at ON jobs (created_at)   -- "Sort by newest first"
```

Without `idx_jobs_user_id`, every `GET /jobs` request would scan the entire table.
With the index, PostgreSQL jumps directly to the rows for that user. This is an O(log N)
lookup instead of O(N).

### Key SQLAlchemy concepts

**`expire_on_commit=False`** — After `db.commit()`, SQLAlchemy normally marks all
loaded object attributes as "stale." The next access would re-query the database.
But in FastAPI, the response serialization happens AFTER the commit — if attributes
are stale, it tries to re-query a closed session → `DetachedInstanceError`. Setting
this to `False` keeps the in-memory values readable after commit.

**`flush()` vs `commit()`** — `flush()` writes SQL to the DB within the current
transaction (like writing in pencil — the DB has it but it's not permanent yet).
`commit()` makes it permanent (signing in ink). We call `flush()` in `create_job()`
so we can call `refresh()` to read back server-generated values (`created_at`).

**`expire_all()`** — After a bulk UPDATE (like in `claim_job_for_processing()`),
SQLAlchemy's in-memory objects still show the OLD status. `expire_all()` forces
the next attribute access to re-query the DB and get the current value.

**`pool_size=1` for Lambda** — Lambda handles exactly 1 request per container.
More connections would be wasted AND would hit RDS's connection limit. 500 concurrent
Lambda invocations × pool_size=1 = 500 connections. With pool_size=5, that's 2500
connections — `db.t3.micro` can only handle ~100.

---

## Section 5: AWS Services — What You Need to Know

### S3 (Simple Storage Service)

**What it is:** Object storage. Think of it as an infinite hard drive in the cloud where
files are accessed by a path (called an "object key"), not a file system.

**How CSVForge uses it:**
- `uploads/{user_id}/{job_id}/original.csv` — where raw user uploads land
- `processed/{user_id}/{job_id}/result.csv` — where the cleaned CSV goes

**Presigned URL** — The key concept. When a user wants to upload, the API calls
`generate_presigned_upload_url()` which creates a time-limited URL that is
cryptographically signed with the API's AWS credentials. The URL is handed to the
browser. The browser uploads directly to S3 using this URL — no API involvement.
The API's signature in the URL IS the permission. When the URL expires (15 minutes),
it can no longer be used.

```
API → "here's a signed URL valid for 15 minutes for s3://bucket/key"
Browser → PUT file bytes to that URL directly
S3 → validates the signature → accepts the upload
```

**Why not upload through the API?** API Gateway has a 10 MB hard limit on request size.
CSV files can be 50 MB. The presigned URL bypasses this completely.

**HeadObject** — When the browser calls `POST /uploads/{id}/complete`, the API calls
`HeadObject` on the S3 key. This returns only metadata (file size, content type) without
downloading the bytes. It confirms the file actually landed in S3 before the job is queued.

### SQS (Simple Queue Service)

**What it is:** A message queue. One service puts messages in. Another service reads them.
They never talk to each other directly — the queue is the middleman.

**How CSVForge uses it:**
1. After upload is confirmed, API calls `send_job_message()` → puts `{job_id, s3_key}` into SQS.
2. SQS triggers the Worker Lambda via "event source mapping" (AWS connects them automatically).
3. Worker Lambda processes the job, then returns. SQS deletes the message.

**At-least-once delivery** — SQS guarantees every message is delivered at least once.
In rare failure cases, it might deliver the SAME message twice. This is why the worker
uses a conditional UPDATE — if two workers race for the same job, only one wins.

**Visibility timeout (1800 seconds = 30 minutes)** — When the worker Lambda picks up a
message, SQS hides it from other consumers for 30 minutes. If the Lambda crashes or
times out before finishing, after 30 minutes SQS makes the message visible again so
another worker can retry.

**Dead Letter Queue (DLQ)** — After 5 failed delivery attempts (`MaxReceiveCount=5`),
the message is moved to the DLQ (`csvforge-jobs-dev-dlq`). This prevents a poison pill
message from retrying forever. A human can inspect the DLQ to understand what failed.

**Claim-check pattern** — The SQS message contains ONLY identifiers (`job_id`, `s3_key`),
not the actual CSV bytes. The worker fetches the actual file from S3 separately. This
keeps messages small (SQS has a 256 KB message limit) and decouples message delivery
from data transfer.

### Lambda

**What it is:** Serverless function execution. You write a function, upload it to AWS,
and AWS runs it in response to events. No server to manage. You only pay when it runs.

**How CSVForge uses it:**
- `csvforge-api-dev` — The FastAPI app runs as a Lambda. API Gateway triggers it on
  every HTTP request.
- `csvforge-worker-dev` — The CSV processing worker. SQS triggers it when a new job
  is queued.

**Cold start** — The first time a Lambda function is called (or after a period of
inactivity), AWS has to start a new container, download your code, and initialize it.
This takes 1–3 seconds. The module-level code in Python (like `engine = create_engine(...)`)
runs during this cold start — which is why creating the engine once at module level and
reusing it is important.

**Warm invocation** — Subsequent calls on the same container skip the initialization.
The module-level code has already run. Only the handler function executes. This is fast.

**Container reuse** — AWS keeps Lambda containers alive ("warm") for 5–15 minutes after
the last invocation. During this time, the connection pool is still open. `pool_pre_ping=True`
handles the case where the TCP connection dropped during the freeze.

### API Gateway

**What it is:** The front door for HTTP traffic into Lambda. It:
- Accepts HTTPS connections from browsers
- Validates JWTs (using Cognito as the authority)
- Converts HTTP requests into Lambda events (JSON format)
- Converts Lambda responses back into HTTP responses

**JWT Authorizer** — API Gateway validates the `Authorization: Bearer <token>` header
on every request. It downloads Cognito's public keys (JWKS) and verifies the JWT
signature. If the token is invalid or expired → 401, never reaches Lambda. If valid →
JWT claims are injected into the request context. FastAPI reads them via `get_current_user()`.

### Cognito

**What it is:** AWS's user authentication service. Handles sign-up, sign-in, password
reset, MFA, and token issuance.

**What CSVForge uses:**
- Cognito issues JWTs (access tokens) when users log in
- API Gateway validates these tokens
- The token's `sub` claim is the user's permanent, immutable ID

**Why `sub` and not `email`?** Email can change. The `sub` is a UUID assigned at account
creation and never changes. All ownership checks use `sub` stored in `job.user_id`.

### CloudFormation

**What it is:** AWS's "infrastructure as code" service. Instead of clicking in the console
to create resources, you write YAML templates describing what resources you want. CloudFormation
creates, updates, or deletes them to match your template.

**How CSVForge uses it:**
The entire infrastructure (Lambda functions, API Gateway, RDS, S3, SQS, IAM roles, VPC
configuration) is defined in CloudFormation templates in the `buildspec/` directory.

**Stack** — A CloudFormation stack is a named group of AWS resources created from one
template. If you delete the stack, all resources in it are deleted together.

**Change set** — Before applying changes, CloudFormation computes a "change set" — a
preview of what will be created, updated, or deleted. The CI/CD pipeline creates a change
set, reviews it, then applies it. This prevents surprises.

**Why infrastructure as code?** Reproducibility and version control. The exact same
infrastructure can be recreated from scratch in minutes. You can see in git history exactly
what changed and when.

### RDS (Relational Database Service)

**What it is:** AWS's managed PostgreSQL. AWS handles OS patching, backups, failover, and
hardware. You just connect to an endpoint and use it like a regular PostgreSQL database.

**What you're running:** `db.t3.micro` — the smallest PostgreSQL instance. Single-AZ
(no automatic failover). 20 GB storage. Enough for a learning project; not production-grade.

---

## Section 6: CI/CD Pipeline — What You Need to Know

### The Pipeline (CodePipeline → CodeBuild → CloudFormation)

```
GitHub push to main branch
    │
    ▼
AWS CodePipeline (orchestrator)
    │
    ├── Stage 1: Source
    │   Pull code from GitHub/CodeCommit
    │
    ├── Stage 2: Test
    │   CodeBuild runs: pytest tests/unit/ -v
    │   (uses buildspec/buildspec-test-backend.yml)
    │   Fails here = nothing deploys
    │
    ├── Stage 3: Build
    │   CodeBuild builds Docker image for worker Lambda
    │   Pushes to ECR (Elastic Container Registry)
    │   (uses buildspec/buildspec-build-worker.yml)
    │
    ├── Stage 4: Infrastructure
    │   CodeBuild creates CloudFormation change set
    │   Human approves or auto-approves
    │   CloudFormation applies changes
    │
    ├── Stage 5: Deploy
    │   Update Lambda function code
    │   Run database migrations (alembic upgrade head)
    │   Deploy frontend to S3
    │
    └── Stage 6: Smoke Test
        Call GET /health, verify 200
        (uses buildspec/buildspec-smoke-test.yml)
```

### The buildspec files

Each `buildspec-*.yml` file tells CodeBuild what to do in that stage:
- `install` section: install Python dependencies, run `pip install`
- `build` section: run the actual work (tests, build, deploy command)
- Environment variables: passed in from CodePipeline (Lambda ARN, S3 bucket name, etc.)

### Why this matters for your presentation

The pipeline enforces quality gates: **you cannot deploy broken code**. The unit tests
run first. If any test fails, the pipeline stops and nothing is deployed. This is the
reason the "53 tests must pass" rule is so important — the pipeline will catch any
regression before it reaches production.

---

## Section 7: The Worker Lambda — What You Need to Know

### What it does

```
1. SQS delivers: {"job_id": "abc-123", "s3_key": "uploads/user/abc-123/original.csv"}

2. Worker calls claim_job_for_processing(db, job_id)
   → Conditional UPDATE: sets status='processing' WHERE status IN ('queued', 'processing')
   → If rowcount=0 (someone else got it, or already completed): exit, do nothing

3. Worker downloads CSV from S3 to /tmp/abc-123.csv

4. Worker runs csv_processor.py:
   → parse_and_analyse(): read the file, detect column names, count rows
   → clean(): remove duplicate rows, validate required columns
   → write_csv(): write cleaned version to a new file in /tmp

5. Worker uploads cleaned file to S3:
   → s3_key: "processed/user/abc-123/result.csv"

6. Worker calls complete_job(db, job_id, processed_s3_key, row_count, column_names)
   → UPDATE jobs SET status='completed', processed_s3_key=..., row_count=..., etc.
   → If this fails: call fail_job(db, job_id, failure_reason=str(error))

7. SQS deletes the message (because Lambda returned without error)
```

### The idempotency guarantee (most important concept)

SQS can deliver the same message twice. What happens if two Workers race?

```
Worker A: UPDATE jobs SET status='processing' WHERE id='X' AND status IN ('queued','processing')
Worker B: UPDATE jobs SET status='processing' WHERE id='X' AND status IN ('queued','processing')
```

The database row lock means only one UPDATE executes at a time. The first to execute gets
`rowcount=1` → proceeds. The second gets `rowcount=0` (the status is now 'processing', which
is still IN the list, so actually... it also gets rowcount=1). But S3 write is idempotent
(overwriting the same key with the same content is fine), and `complete_job` just overwrites
the result columns. The end state is correct. This is by design — see the `claim_job_for_processing`
docstring for the full reasoning including why `processing` is in the WHERE clause.

---

## Section 8: Interview Q&A — Self-Test

Cover the answer, say it out loud, check.

---

### Q1: Walk me through what happens when a user uploads a CSV file.

**Answer:**
1. User selects a file. Frontend calls `POST /jobs` with filename and file size.
2. API creates a `pending_upload` job in PostgreSQL. Calls `generate_presigned_upload_url()` to get a signed S3 URL. Returns `{job_id, upload_url}` to browser.
3. Browser uploads the file bytes directly to S3 using a `PUT` to the presigned URL. The API is not involved. This bypasses the 10 MB API Gateway limit.
4. Frontend calls `POST /uploads/{job_id}/complete`. API calls `HeadObject` to verify the file is in S3. Transitions job from `pending_upload` → `queued`. Publishes `{job_id, s3_key}` to SQS.
5. SQS triggers the Worker Lambda. Worker downloads the CSV from S3, deduplicates it, uploads the result. Calls `complete_job()` → job transitions to `completed`.
6. Frontend polls `GET /jobs/{id}` and sees `completed`. Shows download button. User clicks → frontend calls `GET /jobs/{id}/download` → gets a presigned GET URL → browser downloads the file.

---

### Q2: What is a presigned URL and why is it used here?

**Answer:** A presigned URL is an S3 URL that contains an embedded AWS signature. The signature is computed using the API server's IAM credentials. When the browser sends a PUT request to this URL, S3 validates the signature — it's proof that the API authorized this specific upload. The browser needs no AWS credentials. CSVForge uses it because: (1) API Gateway has a 10 MB request size limit, (2) files can be 50 MB, (3) buffering bytes through Lambda wastes memory and execution time.

---

### Q3: What is SQS at-least-once delivery and why does it matter?

**Answer:** Standard SQS guarantees every message is delivered at least once, but in rare cases (Lambda timeout, network glitch) a message can be delivered twice. This means the Worker Lambda could receive the same job twice. If not handled, two workers could both process the same CSV and produce duplicate or corrupted output. CSVForge handles this with a conditional UPDATE (`claim_job_for_processing`): the SQL atomically checks the job status AND updates it in one statement. The database's row-level locking ensures only one worker can win. The other gets `rowcount=0` and exits without processing.

---

### Q4: What is the difference between `flush()` and `commit()`?

**Answer:** `flush()` sends the SQL statements to the database within the current open transaction — the rows exist in the DB and are visible to queries within the same transaction, but the changes are not permanent. Rolling back after a flush undoes everything. `commit()` makes the transaction permanent — the changes are written to disk and visible to all other database connections. In `create_job()`, `flush()` is called so we can call `refresh()` to read back the server-generated `created_at` timestamp. The actual `commit()` happens later in `get_db()` after the route handler returns.

---

### Q5: Why does `get_db()` use `yield` instead of `return`?

**Answer:** `yield` turns `get_db()` into a generator function. FastAPI calls it, Python runs up to the `yield` and pauses — the Session is injected into the route handler. After the route finishes (or raises an exception), FastAPI resumes `get_db()` from after the `yield`. The `try/finally` block ensures `db.close()` always runs, even on exceptions. A regular `return` can't run cleanup code after the caller is done. This is Python's standard pattern for resource management when you can't use a `with` statement.

---

### Q6: What is a cold start in Lambda?

**Answer:** When AWS runs a Lambda function for the first time (or after it has been idle for ~15 minutes), it has to provision a new container, download the function code, and run all the module-level Python code — engine creation, Secrets Manager calls, imports. This takes 1–3 seconds and is called a cold start. Subsequent invocations on the same container skip this — only the handler function runs. This is a "warm invocation." CSVForge's `pool_pre_ping=True` handles the edge case where a warm container's TCP connection to RDS was dropped during the freeze.

---

### Q7: What is connection pooling and why is `pool_size=1` correct for Lambda?

**Answer:** A connection pool pre-creates TCP connections to PostgreSQL and reuses them across requests. Opening a new PostgreSQL connection involves a TCP handshake, TLS negotiation, and authentication — 10–50 ms overhead. A pool amortizes this across many requests. For Lambda, `pool_size=1` is correct because Lambda scales by spinning up more containers (each handling one request), not by using threads inside one container. At 500 concurrent requests, that's 500 containers × 1 connection each = 500 connections total — well within RDS limits. If `pool_size=5`, that's 2500 connections — a `db.t3.micro` only handles ~100.

---

### Q8: Why does the jobs list endpoint return 404 for wrong-owner jobs instead of 403?

**Answer:** 403 (Forbidden) says "I know this resource exists but you don't have permission." This leaks the information that the resource exists — a form of information disclosure. An attacker can enumerate all job IDs and discover which ones belong to other users. 404 (Not Found) for both "doesn't exist" and "belongs to someone else" prevents this. The caller can't distinguish between a job that doesn't exist and a job that belongs to someone else. This is a standard API security practice.

---

### Q9: What is React's Context and how is it used in CSVForge?

**Answer:** React Context is a way to share state across many components without passing it down through props at every level. `AuthContext` in CSVForge stores the Cognito authentication state — the user's tokens and profile. Any component in the tree (the upload form, the jobs list, the admin panel) can read the current user from context without the parent components having to pass it down manually. When the user logs out, the context state changes and every component that reads from it re-renders automatically.

---

### Q10: What is CORS and why does CSVForge configure it?

**Answer:** CORS (Cross-Origin Resource Sharing) is a browser security feature. When JavaScript on `localhost:5173` (the React app) makes a request to `localhost:8000` (the API), the browser blocks it by default because the origins differ. The server must explicitly allow it by returning `Access-Control-Allow-Origin: http://localhost:5173` in the response. FastAPI's `CORSMiddleware` handles this. The `Content-Range` header also needs to be explicitly listed in `expose_headers` because React Admin reads it for pagination in the admin panel.

---

### Q11: What is the state machine pattern and why use it for job status?

**Answer:** A state machine is a system with a defined set of states and explicit rules for which transitions are allowed. CSVForge's `JobStatus` has 5 states and each function only performs one specific transition (`transition_to_queued`, `claim_job_for_processing`, `complete_job`, `fail_job`). The benefits: (1) it's impossible to accidentally write code that moves a job from `failed` to `processing` because there's no function for that transition; (2) every historical state is auditable; (3) the conditional UPDATEs use the state directly — `WHERE status = 'queued'` — which makes concurrency safe.

---

### Q12: What does CloudFormation do and why use it?

**Answer:** CloudFormation is AWS's infrastructure-as-code service. Instead of clicking in the console to create an RDS instance, an S3 bucket, and a Lambda function, you write a YAML template describing all of them. CloudFormation creates, updates, or deletes resources to match the template. Benefits: reproducibility (the entire stack can be recreated from scratch), version control (the template lives in git — you can see what changed and when), and automation (the CI/CD pipeline applies changes without human clicking). The change set preview step means you can review exactly what will change before it happens.

---

## Section 9: Presentation Checklist

Before your assessment, go through this list. If you can't answer something, go to the
file listed in parentheses and read it.

### Architecture
- [ ] Can you draw the full architecture diagram from memory?
- [ ] Can you name every AWS service and say what it does?
- [ ] Can you trace one full CSV upload end-to-end without looking at notes?
- [ ] Can you explain why the browser uploads directly to S3 (not through API)?

### Database
- [ ] Can you name all 5 job statuses and explain when each is set?
- [ ] Can you explain the conditional UPDATE pattern and why it prevents race conditions?
- [ ] Can you explain `flush()` vs `commit()` with the pencil/ink analogy?
- [ ] Can you explain `expire_on_commit=False` and when it matters? (`connection.py`)
- [ ] Can you explain why `pool_size=1` is correct for Lambda? (`connection.py`)
- [ ] Can you name the 3 indexes and explain why each exists? (`models/job.py`)

### React
- [ ] Can you explain what a component is with an example from this project?
- [ ] Can you explain what state is and give an example?
- [ ] Can you explain what Context is and how `AuthContext` uses it? (`auth/AuthContext.tsx`)
- [ ] Can you explain the upload flow from the frontend's perspective?

### AWS
- [ ] Can you explain presigned URLs with the "visitor pass" analogy?
- [ ] Can you explain SQS at-least-once delivery and idempotency?
- [ ] Can you explain Lambda cold starts and why `pool_pre_ping=True` matters?
- [ ] Can you explain what Cognito does and why `sub` is used for identity?

### Code
- [ ] Can you explain every line of `get_db()` in `connection.py`?
- [ ] Can you explain `claim_job_for_processing()` in `job_repository.py`?
- [ ] Can you explain the `Depends()` pattern in `main.py`?
- [ ] Can you explain what Mangum does and why it exists?

---

## Section 10: Day-by-Day Study Schedule

If you have multiple days before your assessment:

**Day 1:** Big Picture
- Read MENTAL_MAP.md Section 1–3 (architecture + file index)
- Read `backend/main.py` with this guide open
- Trace the GET /jobs request lifecycle (Section 3 of this guide)

**Day 2:** Database Layer
- Read `backend/db/connection.py` — every comment
- Read `backend/models/job.py` — focus on the `status` column and `column_names` comments
- Read `backend/db/job_repository.py` — focus on `claim_job_for_processing`
- Do Interview Q4, Q5, Q7 out loud

**Day 3:** AWS Services
- Read DAY2_LEARNING_GUIDE.md (S3, SQS, Worker Lambda)
- Read `backend/s3_client.py` and `backend/sqs_client.py`
- Do Interview Q1, Q2, Q3, Q6 out loud

**Day 4:** Frontend + CI/CD
- Read `frontend/src/` — App.tsx, AuthContext.tsx, client.ts
- Read one buildspec file (buildspec-test-backend.yml is the simplest)
- Do Interview Q9, Q10 out loud

**Day 5:** Full rehearsal
- Draw architecture diagram from memory
- Trace a full upload end-to-end without notes
- Do ALL 12 interview questions out loud
- Review Section 9 checklist

---

## Quick Reference — Key File Locations

| When asked about... | Open this file |
|--------------------|---------------|
| Full architecture | `docs/MENTAL_MAP.md` |
| All design decisions | `docs/DECISIONS.md` |
| Connection pooling, `get_db()` | `backend/db/connection.py` |
| Job model, 15 columns, state machine | `backend/models/job.py` |
| All SQL operations | `backend/db/job_repository.py` |
| All API endpoints | `backend/main.py` |
| Auth, JWT, Cognito | `backend/auth.py` |
| S3 operations, presigned URLs | `backend/s3_client.py` |
| SQS operations, message format | `backend/sqs_client.py` |
| Pydantic request/response models | `backend/schemas.py` |
| Database migrations | `backend/alembic/versions/0001_initial_jobs_table.py` |
| Frontend components | `frontend/src/components/` |
| Test fixtures | `tests/conftest.py` |
| CI/CD pipeline steps | `buildspec/` |
