# Day 2 Learning Guide — CSVForge AWS Architecture

This guide explains the entire CSVForge distributed system from first principles.
After reading it once, you should be able to explain every component, every design decision,
and every failure mode — without looking at the code.

---

## 1. The Factory — A Mental Model for CSVForge

Every component in CSVForge maps to something in a factory. When you understand what the factory
does, you understand what the software does.

| Component | Factory Equivalent | Role |
|---|---|---|
| **Browser** | Customer | Brings raw materials; picks up finished goods |
| **FastAPI** | Reception Desk | Takes requests, issues permits, schedules work — never processes goods |
| **S3 Input** (`uploads/`) | Warehouse | Stores raw materials (the original CSV) |
| **SQS** | Job Ticket Conveyor Belt | Carries work orders (tickets), not the goods themselves |
| **Lambda** | Temporary Worker | Appears only when there is work; paid per job; vanishes when done |
| **S3 Output** (`processed/`) | Processed Goods Storage | Stores finished products (the cleaned CSV) |
| **PostgreSQL** | Factory Ledger | Records what happened — job status, timestamps — not the goods themselves |
| **IAM** | Security Guards | Enforces who has the key to which door |
| **CloudWatch** | CCTV / Factory Monitoring | Records everything; alerts when something goes wrong |
| **DLQ** | Failed Jobs Room | Jobs that couldn't be processed after multiple attempts wait here for inspection |
| **Alembic** | Blueprint Version History | Tracks schema changes the way Git tracks code changes |

The factory analogy clarifies the most important architectural truth:
**the Reception Desk (FastAPI) never touches the goods (CSV bytes).**
It only creates the job ticket and hands it to the conveyor belt. The Temporary Worker
(Lambda) does the actual processing.

---

## 2. Full Architecture Diagram

```
                          CONTROL PLANE
          ┌──────────────────────────────────────────────────┐
          │  IAM roles, SQS config, Lambda ESM, queue setup  │
          └──────────────────────────────────────────────────┘

                          DATA PLANE — UPLOAD FLOW
                                                              ┌──────────────┐
  ┌─────────┐  POST /uploads/intent  ┌──────────┐            │  PostgreSQL   │
  │ Browser │ ────────────────────►  │  FastAPI │ ──────────► │  (job record) │
  │         │  ◄──────────────────   │          │   INSERT     │  status=      │
  │         │  presigned PUT URL     └──────────┘             │  pending_     │
  │         │                                                  │  upload       │
  │         │  PUT CSV bytes (direct to S3, no FastAPI)        └──────────────┘
  │         │ ─────────────────────────────────────────────►  ┌──────────────┐
  │         │                                                  │  S3 uploads/ │
  │         │  POST /uploads/{job_id}/complete                 │  (raw CSV)   │
  │         │ ────────────────────►  ┌──────────┐             └──────────────┘
  │         │  ◄────────────────────  │  FastAPI │ ──────────► PostgreSQL
  │         │  { status: "queued" }   │          │   UPDATE     status=queued
  └─────────┘                        │          │
                                     │          │ ──────────► ┌──────────────┐
                                     └──────────┘   SendMsg   │     SQS      │
                                                              │  (job ticket) │
                          DATA PLANE — PROCESSING FLOW        └──────┬───────┘
                                                                     │ ESM polls
                                                              ┌──────▼───────┐
                                                              │    Lambda    │
                                                              │  (worker)    │
                                                              └──────┬───────┘
                                                      reads          │
                                               ┌──────────────────   │
                                               │ S3 uploads/  ◄──────┘  GetObject
                                               └──────────────────
                                                                     │ processes CSV
                                               ┌──────────────────   │
                                               │ S3 processed/ ◄─────┘  PutObject
                                               └──────────────────
                                                                     │ marks done
                                               ┌──────────────────   │
                                               │  PostgreSQL   ◄─────┘  UPDATE
                                               │  status=              status=
                                               │  completed            completed
                                               └──────────────────

                          RESULT RETRIEVAL FLOW

  ┌─────────┐  GET /jobs/{job_id}    ┌──────────┐            ┌──────────────┐
  │ Browser │ ────────────────────►  │  FastAPI │ ──────────► │  PostgreSQL  │
  │         │  ◄──────────────────   │          │   SELECT     │  (job state) │
  │         │  { status, metadata }  └──────────┘             └──────────────┘
  │         │
  │         │  GET /jobs/{job_id}/download
  │         │ ────────────────────►  ┌──────────┐
  │         │  ◄──────────────────   │  FastAPI │  generates presigned GET URL
  │         │  presigned GET URL     └──────────┘
  │         │
  │         │  GET {presigned-url} (direct to S3, no FastAPI)
  │         │ ─────────────────────────────────────────────►  ┌──────────────┐
  │         │  ◄─────────────────────────────────────────────  │ S3 processed/│
  │         │  cleaned CSV bytes                               │              │
  └─────────┘                                                  └──────────────┘
```

---

## 3. Two Different Flows: Control Plane vs Data Plane

### Control Plane (configuration, setup, permissions)

The control plane is set up once before any data flows through the system.
Changes to the control plane are rare (deploying new infrastructure, changing IAM policies).

| Action | Who does it | Tool |
|---|---|---|
| Create S3 bucket, set CORS | Ops/developer | `create_dev_resources.sh` / CloudFormation |
| Create SQS queues, set redrive policy | Ops/developer | `create_dev_resources.sh` / CloudFormation |
| Create IAM roles and policies | Ops/developer | `create_dev_resources.sh` / CloudFormation |
| Create Lambda function | Ops/developer | `deploy_lambda.sh` / CloudFormation |
| Configure event source mapping | Ops/developer | `deploy_lambda.sh` / CloudFormation |
| Run Alembic migrations | Developer | `alembic upgrade head` |

### Data Plane (actual work — CSV bytes flow here)

The data plane runs on every user request. This is the live system.

**Upload Flow:**
```
1. Browser → FastAPI: POST /uploads/intent (declares file name and size)
2. FastAPI → PostgreSQL: INSERT job (status=pending_upload)
3. FastAPI → Browser: presigned PUT URL (expires in 15 minutes)
4. Browser → S3: PUT raw CSV bytes (direct, no FastAPI involved)
5. Browser → FastAPI: POST /uploads/{job_id}/complete
6. FastAPI → S3: HeadObject (verify upload succeeded, check file size)
7. FastAPI → PostgreSQL: UPDATE job status=queued
8. FastAPI → SQS: SendMessage {job_id, s3_key} (job ticket)
9. FastAPI → Browser: { status: "queued" }
```

**Processing Flow:**
```
10. Lambda → SQS: ReceiveMessage (event source mapping, not Lambda code)
11. Lambda → PostgreSQL: UPDATE job status=processing (claim job)
12. Lambda → S3: GetObject uploads/{key} (download raw CSV)
13. Lambda: process CSV (parse, validate, clean, deduplicate)
14. Lambda → S3: PutObject processed/{key} (upload cleaned CSV)
15. Lambda → PostgreSQL: UPDATE job status=completed (save metadata)
16. Lambda → SQS: DeleteMessage (implicit on successful return)
```

**Result Retrieval Flow:**
```
17. Browser → FastAPI: GET /jobs/{job_id} (poll for completion)
18. FastAPI → PostgreSQL: SELECT job (return status and metadata)
19. Browser → FastAPI: GET /jobs/{job_id}/download (when completed)
20. FastAPI → S3: generate_presigned_url('get_object') for processed/{key}
21. FastAPI → Browser: presigned GET URL (expires in 60 minutes)
22. Browser → S3: GET {presigned-url} (download cleaned CSV directly)
```

**What moves where:**
- CSV bytes: Browser → S3 (direct), S3 → Lambda (via GetObject), Lambda → S3 (via PutObject), S3 → Browser (via presigned URL). FastAPI never touches the bytes.
- Job tickets: FastAPI → SQS → Lambda. The ticket contains job_id only, not the CSV.
- Job state: FastAPI writes it, Lambda updates it, Browser reads it (via FastAPI).

---

## 4. S3 Mental Model

### The warehouse analogy

- **Bucket** = the warehouse building itself (one physical location)
- **Object** = a physical box (a specific file: `uploads/user123/job456/data.csv`)
- **Key** = the shelf/location label (the full path string, e.g., `uploads/user123/job456/data.csv`)
- **Prefix** = the aisle label (a common key prefix, e.g., `uploads/`)
- **Metadata** = the label on the box (content-type, custom headers, tags)

S3 looks like a filesystem but it is NOT a POSIX filesystem. The differences matter:

| POSIX filesystem | S3 object storage |
|---|---|
| `mv file.csv new_location/` (fast) | No rename: must copy to new key, then delete old key (two operations) |
| File locking (only one writer) | No locking: last writer wins |
| Atomic append | No append: must read full object, modify, rewrite entire object |
| Consistent directory listing | Eventual consistency on list operations (listing may miss recent writes) |
| Partial write (truncated file) | No partial writes: object is only visible after the full PUT completes |

### Why object storage scales

Each S3 `GET` or `PUT` request is an independent HTTP request. There is no coordination
between requests. S3 can serve millions of simultaneous requests because:
- No shared state between requests
- Objects are sharded across many physical nodes
- Reads are served from the nearest edge
- The only bottleneck is prefix-level request rates (which can be mitigated with key sharding)

This is fundamentally different from a filesystem, where every write acquires locks and
modifies shared metadata.

---

## 5. Presigned URL Mental Model

### The temporary visitor pass

Imagine a factory with a security desk. Normally, only staff with keycards can enter.
But you need a delivery driver to bring materials directly to the warehouse floor.

A presigned URL is like the security desk issuing a **single-visit visitor pass**:
- The pass says exactly which door the visitor can enter
- The pass is valid for 15 minutes only
- The visitor presents the pass directly at the door — they don't go through the security desk again
- The security desk's authorization (the IAM role's signature) is embedded in the pass

### What the URL contains

A presigned S3 PUT URL encodes all of these in its query parameters:
```
https://csvforge-files-dev.s3.ap-south-1.amazonaws.com/uploads/user123/job456/data.csv
  ?X-Amz-Algorithm=AWS4-HMAC-SHA256
  &X-Amz-Credential=AKID/20260101/ap-south-1/s3/aws4_request
  &X-Amz-Date=20260101T120000Z
  &X-Amz-Expires=900
  &X-Amz-SignedHeaders=host
  &X-Amz-Signature=<HMAC-SHA256 of all the above>
```

The `X-Amz-Signature` is a cryptographic hash that proves the URL was generated by someone
who had the IAM role's private key. S3 recomputes the signature when the browser presents
the URL and rejects requests where the signature does not match.

### Why the browser doesn't need AWS credentials

The signature IS the authorization. The browser holds a signed permission slip, not the keys.
S3 verifies:
1. Is the signature valid? (computed from IAM role's key — only the AWS account can compute this)
2. Has the URL expired? (`X-Amz-Expires`)
3. Does the request match what was signed? (method, bucket, key, headers)

The browser never sees the IAM role's access key. The presigned URL can be shared, stored,
and used by anyone with the URL — which is why it expires quickly (15 minutes for uploads).

### After expiry

If the browser tries to PUT after the URL expires, S3 returns `403 Forbidden`:
```xml
<Error>
  <Code>RequestExpired</Code>
  <Message>Request has expired</Message>
</Error>
```

The user must start over (call `POST /uploads/intent` again) to get a fresh presigned URL.

### Permanent S3 URL (what NOT to do)

A permanent public S3 URL (`https://bucket.s3.amazonaws.com/key`) requires the bucket to be
**publicly accessible** — which disables all access controls. Any file in the bucket becomes
readable by anyone on the internet. Never do this for user-uploaded data.

---

## 6. SQS Mental Model

### The conveyor belt analogy

Imagine a factory conveyor belt that carries job tickets. Each ticket says "process this job"
but does not carry the goods themselves (the goods are already in the warehouse / S3).

Workers stand at the end of the belt. When a ticket arrives:
1. A worker picks up the ticket (the ticket becomes invisible to other workers)
2. The worker processes the job
3. The worker tears up the ticket (DeleteMessage)

If the worker drops the ticket or walks away:
4. After N seconds (visibility timeout), the ticket reappears on the belt
5. Another worker picks it up

### Message = ticket, not goods

The SQS message body for CSVForge is tiny:
```json
{"job_id": "abc-123", "s3_key": "uploads/user123/abc-123/data.csv"}
```

The CSV bytes live in S3 — the queue only carries the address. This is intentional:
- SQS messages have a 256 KB size limit
- A 50 MB CSV would far exceed this limit
- Storing URLs (job tickets) in queues is the standard pattern

### Visibility timeout

When Lambda receives a message, SQS hides it for `VisibilityTimeout` seconds (1800s = 30 min).
During this window, the message is invisible to all other consumers.

Why 1800s? AWS recommends 6× Lambda timeout. Lambda timeout is 300s, so 6 × 300 = 1800s.
This gives Lambda plenty of time to complete processing before the message reappears.

### Retry

If Lambda crashes (exits with exception), the message was never deleted.
After 1800 seconds, SQS makes it visible again. Lambda picks it up and retries.

### DLQ

After `MaxReceiveCount = 5` failures, SQS automatically moves the message to the
Dead Letter Queue (`csvforge-jobs-dlq-dev`). The DLQ holds the message for 14 days.

### At-least-once vs exactly-once

**At-least-once (SQS Standard):** Every message is delivered one or more times.
In rare cases (network issues, SQS internal events), a message may be delivered twice.
The guarantee is that no message is ever lost — but duplicates are possible.

**Exactly-once (SQS FIFO):** Each message is delivered exactly once, in order.
CSVForge uses Standard SQS (not FIFO) because:
- Jobs are independent — ordering doesn't matter
- Standard is 3× cheaper than FIFO
- Duplicates are handled by the worker's idempotency guard

### Why at-least-once requires idempotent consumers

If your worker is not idempotent and SQS delivers a message twice, the job is processed twice.
This could mean a user's CSV is "cleaned" twice, or the same invoice is generated twice.

CSVForge's worker is idempotent via the conditional SQL UPDATE in `claim_job_for_processing()`.
If the second delivery arrives after the first completes, the UPDATE finds the job is
`completed` and returns False. No re-processing happens.

---

## 7. Lambda Mental Model

### The temporary worker analogy

Lambda is a temporary worker that the factory calls in when there is work.
Unlike a permanent employee, the worker:
- Does not exist when there is no work
- Is spun up (cold start) when a job arrives
- Is paid only for the time they actively work (millisecond billing)
- Disappears after the job is done (execution environment may be reused, but is not guaranteed)

### Cold start

When Lambda invokes a function in a new execution environment:
1. AWS provisions a MicroVM (Firecracker)
2. Downloads the deployment package (zip or container)
3. Starts the Python interpreter
4. Imports all top-level modules (boto3, SQLAlchemy, etc.)
5. **Then** calls your `lambda_handler`

This initialization takes 100–500ms for a zip package, or 1–3 seconds for a container image.

### Warm environment

After the first invocation, Lambda may reuse the same execution environment for subsequent
invocations. The Python interpreter is already running, modules are already imported.
The `lambda_handler` function is called directly — no initialization cost.

This is why database connections at module level (not inside the handler) persist across
warm invocations. But you cannot rely on this — Lambda may always give you a cold start.

### Execution environment

Each Lambda execution environment has:
- Memory: allocated MB of RAM (256 MB for dev, 1024 MB for prod)
- CPU: proportional to memory allocation
- `/tmp`: 512 MB of ephemeral disk (cleared between environments, not between warm invocations)
- Environment variables: injected at function configuration time
- Timeout: after N seconds, Lambda kills the environment (invocation fails)

### Lambda timeout and SQS visibility timeout relationship

Lambda timeout: 300s (the function must complete in 5 minutes)
SQS visibility timeout: 1800s (the message stays invisible for 30 minutes)

If SQS visibility timeout < Lambda timeout:
- Lambda is processing the message
- The visibility timeout expires before Lambda finishes
- The message becomes visible again
- **Another Lambda invocation starts processing the same job simultaneously**
- Both workers race to complete the same job — a concurrency bug

Setting visibility timeout to 6× Lambda timeout prevents this entirely.

### IAM execution role

When Lambda starts, AWS injects temporary credentials for the execution role.
These credentials appear as environment variables inside the execution environment:
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`.

The worker never calls `boto3.Session(aws_access_key_id=...)` — it just calls
`boto3.client('s3')` and boto3 automatically picks up the injected credentials.
The IAM execution role determines what those credentials can do.

---

## 8. IAM Mental Model

### The key system analogy

IAM is the factory's key management system. Every role is a set of keys:
- The **trust policy** says who can pick up this set of keys
- The **permission policy** says which doors these keys open

### Least privilege

The principle of least privilege: every role gets exactly the keys it needs for its job.

The Lambda worker needs:
- The key to the uploads warehouse (S3 GetObject on `uploads/*`) — to pick up the raw CSV
- The key to the processed goods warehouse (S3 PutObject on `processed/*`) — to deliver output
- The key to the job ticket machine (SQS ReceiveMessage, DeleteMessage) — to take and close tickets
- The key to the CCTV room (CloudWatch logs) — to emit structured logs

The Lambda worker does NOT need:
- The key to publish new job tickets (no SQS SendMessage) — would enable processing loops
- The key to the uploads warehouse for writing (no S3 PutObject on `uploads/*`) — would enable overwriting original CSVs
- The master key (no AdministratorAccess) — would be catastrophic if the function were compromised

### Trust policy

The trust policy answers: "who is allowed to assume this role?"

```json
{
  "Principal": {"Service": "lambda.amazonaws.com"},
  "Action": "sts:AssumeRole"
}
```

This says: only the Lambda service can use these keys. A human or another AWS service
cannot accidentally pick up the Lambda worker's permissions.

### Permission policy

The permission policy answers: "what can this role do once it has assumed these keys?"

Permissions are scoped to the minimum required resource:

```json
{
  "Action": "s3:GetObject",
  "Resource": "arn:aws:s3:::csvforge-files-dev/uploads/*"
}
```

Not `arn:aws:s3:::*` (all buckets). Not `arn:aws:s3:::csvforge-files-dev/*` (entire bucket).
Just `uploads/*` within this specific bucket.

### Why Lambda role does not have AdministratorAccess

A worker who can access every room in the factory is a security risk:
- If the Lambda function has a code vulnerability (injection, SSRF), an attacker
  can escalate to full AWS account access
- The blast radius of a compromised Lambda is bounded by its IAM role
- Least privilege means an attacker who compromises the Lambda worker can only read CSVs
  and write processed output — they cannot create new IAM users, read other users' data,
  spin up EC2 instances, etc.

### ARN scope

Resource ARNs allow per-resource permission scoping:
```
arn:aws:s3:::csvforge-files-dev/uploads/*
     │    │         │               └── key prefix (aisle)
     │    │         └── bucket name (warehouse)
     │    └── service (s3)
     └── partition (aws)
```

The `*` wildcard at the end matches any key with the `uploads/` prefix. This is not
the same as a `*` on the resource ARN (which would match all buckets).

---

## 9. Event-Driven Architecture

### Why NOT synchronous (API processes CSV in request)

If FastAPI received the CSV upload and processed it synchronously in the same HTTP request:

1. **30-second API timeout**: API Gateway HTTP API times out after 30 seconds. A 50 MB CSV
   takes longer than 30 seconds to parse, validate, deduplicate, and clean. Every large upload fails.

2. **Client blocks waiting**: The browser sits on a loading spinner for the entire processing time.
   If the browser closes the connection (user navigates away), the processing stops.

3. **API server memory tied up**: Each concurrent upload occupies a Python thread and holds
   50 MB of CSV in memory. With 100 concurrent uploads, that's 5 GB of memory in the API process.

4. **No retry on failure**: If the API crashes mid-processing, the job is lost with no record.
   The user never knows what happened.

5. **No backpressure**: There is no limit to how many jobs the API can accept simultaneously.
   The API can be overwhelmed by too many concurrent requests.

### Why asynchronous

1. **API returns in milliseconds**: The API accepts the job (< 100ms), publishes a ticket to SQS,
   and returns `{ status: "queued" }`. The client does not wait for processing.

2. **Worker scales independently**: Lambda instances scale from 0 to thousands, independently
   of the API. Adding load on the queue spins up more workers automatically.

3. **Retry is built-in**: SQS + Lambda provide automatic retry with configurable backoff.
   No custom retry logic needed in application code.

4. **Backpressure**: When Lambda is at capacity, the queue grows. Queue depth is a visible
   CloudWatch metric — you can see when the system is overloaded and scale accordingly.

5. **Failure isolation**: A Lambda crash does not affect the API. The API is always responsive.
   Failed jobs go to the DLQ without impacting other jobs.

---

## 10. Distributed Systems Concepts in CSVForge

| Concept | Definition | Where it appears in CSVForge |
|---|---|---|
| **Asynchronous processing** | Work is accepted now, completed later | API returns "queued" instantly; Lambda processes in the background |
| **Decoupling** | Components communicate through intermediaries, not directly | FastAPI and Lambda never call each other; SQS is the intermediary |
| **Backpressure** | A slow consumer limits the rate of a fast producer | SQS queue depth grows when Lambda is slow; visible metric, not silent failure |
| **At-least-once delivery** | Every message is delivered one or more times; duplicates possible | SQS Standard — Lambda may receive the same job twice |
| **Idempotency** | Processing the same input multiple times produces the same result | `claim_job_for_processing()` conditional UPDATE prevents double-processing |
| **Visibility timeout** | Message hidden from other consumers while one worker processes it | 1800s on `csvforge-jobs-dev` — prevents two Lambdas processing same job |
| **Dead-letter queue** | Final destination for messages that repeatedly fail | `csvforge-jobs-dlq-dev` — catches permanent failures after 5 retries |
| **Eventual consistency** | System will reach a consistent state, but not immediately | S3 listing may not immediately reflect a recently uploaded object |
| **Failure isolation** | One component's failure does not cascade to others | Lambda crash does not affect the API; API down does not affect in-flight processing |
| **Retry** | Automatic re-attempt on transient failure | SQS re-delivers after visibility timeout; 5 attempts before DLQ |
| **Durability** | Data survives hardware failures | S3: 11 nines durability (multiple AZs). PostgreSQL: RDS automated backups. SQS: messages replicated across AZs. |

---

## 11. Request vs Job

### HTTP request

An HTTP request is:
- **Stateless**: the server forgets about it after sending the response
- **Short-lived**: must complete before the client or gateway times out (30 seconds)
- **Synchronous**: the client blocks waiting for the response
- **Single-threaded**: one request consumes one thread/connection for its entire duration

### Background job

A background job is:
- **Stateful**: has a lifecycle (pending_upload → queued → processing → completed/failed)
- **Long-lived**: can run for minutes; outlives the HTTP request that created it
- **Asynchronous**: the client gets an acknowledgment immediately; completion is polled later
- **Independent lifecycle**: the job continues even if the client disconnects

### How they relate

A request creates a job. The job outlives the request.

```
Browser sends POST /uploads/intent
  → Server creates job record (job_id="abc-123", status="pending_upload")
  → Server returns job_id and presigned URL to browser
  → HTTP request is COMPLETE (server forgets this request)

Browser uploads CSV to S3
Browser sends POST /uploads/abc-123/complete
  → Server transitions job to "queued"
  → Server publishes job ticket to SQS
  → HTTP request is COMPLETE

[Lambda processes job independently — no HTTP request involved]
[Job transitions: queued → processing → completed]

Browser polls GET /jobs/abc-123
  → Server queries PostgreSQL for current job state
  → Returns { status: "completed", row_count: 5000, ... }
```

The job (`abc-123`) was created by the first request, updated by the second request,
processed with no HTTP request at all, and read by the third request. The job's lifecycle
spans multiple requests and outlives each one.

### Status polling

The client learns about job progress by polling. Without polling, the client would have to
maintain a long-lived connection (WebSocket or long-poll) which adds complexity.

For CSVForge, polling every 3–5 seconds is sufficient — processing a 50 MB CSV takes
10–30 seconds, so a few extra seconds of polling latency is negligible.

---

## 12. Ten Trace-the-System Exercises

### Exercise 1: "Explain what happens when I click Upload."

**Short Answer:** Your browser requests a signed permission slip, uploads the file directly to S3 storage, then notifies the API that the upload is done. The API queues a work ticket. A temporary worker (Lambda) picks up the ticket, processes your CSV, and stores the result. You can poll for completion.

**Deep Answer:** The full 22-step flow described in Section 3 — from `POST /uploads/intent` through Lambda writing to S3 and updating PostgreSQL, ending with the browser downloading the processed file via presigned GET URL.

**Architecture Path:** Browser → FastAPI → PostgreSQL → S3 (direct) → FastAPI → SQS → Lambda → S3 → PostgreSQL → Browser

**Analogy:** You walk into a factory (FastAPI), describe your job, get a warehouse pass (presigned URL), drop off your raw materials at the warehouse loading dock (S3), hand in a receipt (complete endpoint), the factory issues a work ticket (SQS), a temporary worker picks up the ticket and processes your goods (Lambda), stores the finished product (S3 processed/), and updates the job log (PostgreSQL). You check the log to see when it's done.

**Interview connection:** This is the canonical "explain your architecture" question. Walk through the upload flow, processing flow, and result retrieval flow.

---

### Exercise 2: "Why doesn't the CSV travel through FastAPI?"

**Short Answer:** API Gateway has a 10 MB payload limit. FastAPI would need to hold 50 MB of CSV in memory per upload. Presigned URLs send the bytes directly to S3, which can handle any size.

**Deep Answer:** Three reasons: (1) API Gateway HTTP API has a hard 10 MB body limit — a 50 MB CSV would be rejected before FastAPI even sees it. (2) Routing bytes through Lambda wastes Lambda memory (50 MB per invocation × N concurrent uploads) and adds billing cost. (3) S3 is designed for large binary transfers — it handles multipart upload, resume, and parallel chunks natively. FastAPI has none of these.

**Architecture Path:** The presigned URL bypasses FastAPI entirely. The browser PUTs directly to `s3.amazonaws.com`. FastAPI only generates the URL and verifies the upload after the fact (HeadObject).

**Analogy:** The factory reception desk doesn't accept large packages — it issues a loading dock pass. You drive to the loading dock and drop off the package directly. The reception desk never carries boxes.

**Interview connection:** "Presigned URLs for direct browser-to-S3 uploads" is a standard pattern for handling large file uploads in serverless architectures.

---

### Exercise 3: "What exactly is stored in PostgreSQL?"

**Short Answer:** Job metadata only — job_id, user_id, status, timestamps, row counts, column counts. No CSV bytes.

**Deep Answer:** The `jobs` table stores: `job_id` (UUID), `user_id`, `user_email`, `original_filename`, `s3_key`, `file_size_bytes`, `status` (enum), `row_count`, `column_count`, `duplicate_rows_removed`, `blank_rows_removed`, `error_message`, `created_at`, `updated_at`. All of these are small values — no binary content. PostgreSQL is the factory ledger: it records what happened, when, and to whom. The goods themselves (CSV bytes) are in S3.

**Architecture Path:** FastAPI writes to PostgreSQL on intent (INSERT) and complete (UPDATE). Lambda writes to PostgreSQL when claiming a job (UPDATE) and completing it (UPDATE). Browser reads PostgreSQL via FastAPI (SELECT).

**Analogy:** The factory ledger records "Job #456, customer Alice, status: completed, 5000 items processed." It does not store the physical goods.

**Interview connection:** This is the "separation of concerns" question — why SQL for metadata and object storage for blobs.

---

### Exercise 4: "What exactly is stored in S3?"

**Short Answer:** Two things: (1) the original raw CSV in `uploads/`, (2) the cleaned processed CSV in `processed/`.

**Deep Answer:** `uploads/{user_id}/{job_id}/{filename}` — the original bytes as uploaded by the browser, unchanged. `processed/{user_id}/{job_id}/cleaned_{filename}` — the output of the Lambda worker: trimmed, deduplicated, blank rows removed. S3 stores the actual file bytes as binary objects with metadata headers (Content-Type, Content-Length). No job state, no metadata about what the file contains — all of that is in PostgreSQL.

**Architecture Path:** Browser → S3 uploads/. Lambda reads from S3 uploads/, writes to S3 processed/. Browser downloads from S3 processed/ via presigned GET URL.

**Analogy:** S3 is the warehouse. The `uploads/` aisle holds raw materials. The `processed/` aisle holds finished products. The factory ledger (PostgreSQL) says what is in each aisle; the warehouse actually holds the goods.

**Interview connection:** "Why object storage instead of storing files in PostgreSQL?" — PostgreSQL can store BLOBs but at high cost. Object storage is cheap ($0.023/GB/month), infinitely scalable, and purpose-built for large binary data.

---

### Exercise 5: "What exactly is stored in SQS?"

**Short Answer:** Job tickets — tiny JSON messages containing only job_id and s3_key. No CSV bytes.

**Deep Answer:** Each SQS message body is a JSON string like `{"job_id":"abc-123","s3_key":"uploads/user/abc-123/data.csv"}`. This is ~80 bytes. SQS has a 256 KB message size limit — the CSV bytes (up to 50 MB) would never fit. The queue stores work orders, not goods. Lambda uses the job_id to look up the full job details from PostgreSQL and uses the s3_key to retrieve the CSV from S3.

**Architecture Path:** FastAPI → SQS (SendMessage). Lambda ← SQS (ReceiveMessage via event source mapping). Lambda → SQS (DeleteMessage on success, implicit on exception for retry).

**Analogy:** The conveyor belt carries tickets that say "go to shelf B7 in warehouse and process the box there." The belt does not carry the box.

**Interview connection:** "Why not put the CSV bytes in SQS?" — SQS has a 256 KB limit. The standard pattern is to store large payloads in S3 and pass the S3 reference in the queue message. This is called the "claim check" pattern.

---

### Exercise 6: "How does Lambda know that there is work?"

**Short Answer:** An event source mapping (ESM) connects SQS to Lambda. Lambda doesn't poll manually — the ESM polls SQS on Lambda's behalf and invokes Lambda when messages arrive.

**Deep Answer:** The event source mapping is an AWS-managed component that: (1) polls SQS for messages, (2) batches them (BatchSize=1 for CSVForge), (3) invokes Lambda with the messages as the event payload. The Lambda function code never calls `sqs.receive_message()` — it simply receives the event. The ESM handles the polling, retry, and message deletion (on success). The ESM is configured once via `create-event-source-mapping`.

**Architecture Path:** SQS ← ESM polls → Lambda. When Lambda's `lambda_handler` returns without exception, the ESM calls `sqs:DeleteMessage`. When Lambda raises, the ESM does nothing — message retries after visibility timeout.

**Analogy:** The factory's conveyor belt has a sensor. When a ticket appears, the sensor rings a bell and the temporary worker walks over. The worker doesn't check the belt manually — the sensor handles notification.

**Interview connection:** "What is an event source mapping?" — know that it is not Lambda code, it is AWS infrastructure that wires SQS to Lambda invocations.

---

### Exercise 7: "What happens if Lambda crashes halfway through processing?"

**Short Answer:** The SQS message becomes visible again after 1800 seconds and Lambda retries. If it fails 5 times, the message goes to the DLQ.

**Deep Answer:** See FAILURE_EXPERIMENTS.md Failure 1. The key insight is that the SQS message visibility timeout is the retry mechanism — not Lambda retries, not application code. Lambda simply exits with an exception; SQS handles the rest. The `claim_job_for_processing()` idempotency guard ensures that a retry can always reclaim a `processing` job and re-process it safely.

**Architecture Path:** Lambda exception → SQS hides message for 1800s → ESM polls again → Lambda invoked again → `claim_job_for_processing` reclaims job → processing resumes.

**Analogy:** The temporary worker drops a ticket and goes home sick. After 30 minutes, the ticket reappears on the conveyor belt and a new worker picks it up.

**Interview connection:** "How do you handle Lambda failures?" — SQS visibility timeout + DLQ + idempotent worker.

---

### Exercise 8: "What happens if the same job arrives twice?"

**Short Answer:** The second invocation calls `claim_job_for_processing()`, which returns False because the job is already `completed` or `processing`. The second invocation exits immediately — no duplicate work.

**Deep Answer:** See FAILURE_EXPERIMENTS.md Failure 2. The conditional SQL UPDATE `WHERE status IN ('queued','processing')` is the key. Once a job reaches `completed`, no UPDATE will match — the WHERE clause rejects it. This is the idempotency guarantee. SQS at-least-once delivery is safe because the worker is idempotent.

**Architecture Path:** SQS delivers duplicate message → Lambda invoked → `claim_job_for_processing` runs conditional UPDATE → UPDATE returns rowcount=0 (no match) → handler returns immediately → ESM deletes duplicate message.

**Analogy:** The factory worker picks up a ticket, sees it's for job #456, checks the ledger, sees job #456 is already "completed" — tears up the duplicate ticket without doing any work.

**Interview connection:** "How do you handle duplicate messages?" — idempotent consumers with conditional state transitions.

---

### Exercise 9: "Why can't Lambda have AdministratorAccess?"

**Short Answer:** Least privilege: the worker only needs to read from S3 uploads/, write to S3 processed/, and interact with SQS. AdministratorAccess would give it access to every AWS resource in the account.

**Deep Answer:** If Lambda had AdministratorAccess and the function were compromised (through a code vulnerability, dependency injection, or environment variable leak), an attacker could: create new IAM users with admin access, read every S3 bucket in the account, access production databases, spin up EC2 instances for cryptocurrency mining, delete all resources. With the least-privilege policy, a compromised Lambda can only: read from `csvforge-files-dev/uploads/*`, write to `csvforge-files-dev/processed/*`, and interact with `csvforge-jobs-dev`. The blast radius is bounded.

**Architecture Path:** IAM role → permission policy with specific S3 ARNs and SQS ARN → Lambda execution environment credentials are scoped to those exact permissions.

**Analogy:** A temporary worker is given the key to the raw materials warehouse and the finished goods storage room. They are NOT given the master key to every room in the building.

**Interview connection:** "Explain least privilege" and "what are the security implications of Lambda IAM roles?"

---

### Exercise 10: "Why do we need both PostgreSQL and S3?"

**Short Answer:** PostgreSQL stores structured metadata (status, counts, timestamps) that you query. S3 stores unstructured binary data (CSV bytes) that you transfer. Each is the right tool for its job.

**Deep Answer:** Consider what would break if you used only one:

Only S3 (no PostgreSQL): To find all jobs for user Alice, you'd have to list all objects in S3 with prefix `uploads/alice/` — an expensive operation with eventual consistency. To find all `failed` jobs, you'd need to store status in S3 object metadata and scan every object. To run admin queries like "show jobs created between 9am and 10am with >5000 rows," you'd need a full S3 scan. SQL makes these queries trivial; S3 does not.

Only PostgreSQL (no S3): PostgreSQL can store BLOBs (`BYTEA` column) but at massive cost. A 50 MB CSV in PostgreSQL requires: PostgreSQL TOAST storage overhead, WAL logging of every byte (replication cost), backup storage for each version, and memory pressure on the database server. S3 stores blobs at $0.023/GB/month, serves them via HTTP with no database connections, and handles parallel downloads natively.

The separation is the correct architecture: SQL for structured metadata queries, object storage for binary file transfers.

**Analogy:** The ledger and the warehouse serve different purposes. The ledger (PostgreSQL) tells you what is in the warehouse and its status. The warehouse (S3) holds the actual goods. A ledger entry that says "box 456, 50kg, status: processed" is useful because you can query it. The 50kg box itself belongs in the warehouse, not written into the ledger.

**Interview connection:** "Explain the PostgreSQL + S3 separation and why you chose it over storing files in the database."

---

## 13. Twenty-Three Interview Questions

### Q1 — What is object storage?

**30-second answer:** Object storage is a system for storing files as key-value pairs. The key is a string (like a URL path), the value is the file bytes. Unlike a filesystem, there are no directories — only keys. Objects are immutable after writing: you can replace an entire object but not append or edit part of it.

**Deep explanation:** Object storage emerged as a response to the scaling limits of POSIX filesystems. Traditional filesystems require coordination between nodes (locking, inodes, directory metadata) — this creates bottlenecks at scale. Object storage eliminates coordination: each PUT/GET is an independent HTTP request to a specific key. There is no shared metadata server. This allows unlimited parallel read/write at global scale.

**Where it appears in CSVForge:** S3 stores uploaded CSVs (`uploads/`) and processed output (`processed/`).

**Analogy:** A public locker system at a train station. Each locker has a unique number (key). You can put something in (PUT), take it out (GET), or check if it exists (HEAD). You cannot append to a locker's contents — you can only replace them entirely.

---

### Q2 — Why S3 instead of PostgreSQL for files?

**30-second answer:** PostgreSQL can store binary data but is expensive and slow at large-file transfers. S3 is purpose-built for storing and serving large files: cheap ($0.023/GB/month), unlimited scale, direct HTTP access without connections.

**Deep explanation:** PostgreSQL BYTEA storage: consumes WAL (write-ahead log) for every byte written — replication, backup, and point-in-time recovery all process the raw file bytes. A 50 MB upload creates 50 MB of WAL entries. S3 stores blobs at ~$0.023/GB/month with 11 nines durability across multiple AZs, served via HTTPS with no connection pool. S3 GET operations bypass the database entirely.

**Where it appears in CSVForge:** S3 handles all file transfers; PostgreSQL handles job metadata only.

**Analogy:** The ledger records "50 items received, status: processed" — it does not physically hold the items. The warehouse holds the items.

---

### Q3 — What is a presigned URL?

**30-second answer:** A presigned URL is a time-limited, pre-authorized HTTP URL. Whoever holds the URL can perform exactly one action (GET or PUT) on one specific S3 object within the expiry window, without needing AWS credentials.

**Deep explanation:** The URL contains all request parameters plus a cryptographic signature (HMAC-SHA256) computed using the IAM role's secret key. S3 recomputes the signature when receiving the request and rejects any mismatch. The signature binds: the IAM principal, HTTP method, bucket, key, expiry time, and optionally headers and query parameters.

**Where it appears in CSVForge:** Upload intent generates a presigned PUT URL (15 minutes). Download endpoint generates a presigned GET URL (60 minutes). The browser uses both to interact directly with S3.

**Analogy:** A visitor pass issued by the security desk. The pass specifies which door you can use, for how long. You present the pass at the door — you don't go back to the security desk.

---

### Q4 — How does a presigned URL avoid exposing AWS credentials?

**30-second answer:** The URL contains a signature, not credentials. The signature proves the URL was generated by someone who had the credentials — but the credentials themselves are not in the URL.

**Deep explanation:** The `X-Amz-Signature` query parameter is an HMAC-SHA256 hash of the canonical request, signed with the IAM role's secret access key. The browser presents this hash to S3. S3 knows the IAM role's secret key (it manages IAM). S3 recomputes the HMAC and compares — if they match, the request is authorized. The browser never sees the secret key.

**Where it appears in CSVForge:** FastAPI calls `s3.generate_presigned_url()`. The secret key never leaves AWS. The browser receives a URL with a signature.

**Analogy:** A wax seal on an envelope. The seal proves the letter was authorized by the signatory. The recipient can verify the seal is genuine — but they cannot forge a new seal without the signet ring.

---

### Q5 — What is SQS?

**30-second answer:** SQS (Simple Queue Service) is a managed message queue. Producers send messages; consumers receive and process them independently. Messages are durable (stored until deleted), and delivery is at-least-once.

**Deep explanation:** SQS is a distributed queue backed by redundant storage across multiple AZs. Messages are stored until deleted by the consumer (or until they expire). The queue acts as a buffer between producers (API) and consumers (Lambda), allowing them to operate at different rates without coupling.

**Where it appears in CSVForge:** FastAPI publishes job tickets; Lambda workers consume them.

**Analogy:** A physical in-tray. Documents go in when submitted; a worker picks them up when ready. The tray holds documents independently of who submitted them or who will process them.

---

### Q6 — Why use a queue?

**30-second answer:** A queue decouples the producer from the consumer. The API can publish jobs instantly without knowing if Lambda is available. Lambda can process at its own pace. Neither waits for the other.

**Deep explanation:** Without a queue, the API would directly invoke Lambda. Problems: (1) if Lambda is at capacity, the API call fails; (2) the API waits for Lambda to finish before responding; (3) if Lambda is down for deployment, uploads fail. With a queue: the API always succeeds (publishing to SQS is fast and reliable), Lambda picks up work at its own rate, backpressure is visible (queue depth metric), and Lambda outages don't cause API failures.

**Where it appears in CSVForge:** SQS between FastAPI and Lambda. FastAPI publishes, Lambda consumes, independently.

**Analogy:** An email inbox. The sender sends the email immediately without waiting for the recipient to read it. The recipient reads emails at their own pace. Neither blocks the other.

---

### Q7 — What is at-least-once delivery?

**30-second answer:** At-least-once delivery means every message will be delivered one or more times. No message is ever lost, but duplicates are possible. The consumer must be idempotent.

**Deep explanation:** SQS Standard queues store messages across multiple servers for redundancy. When a message is deleted, the deletion must propagate to all copies. In rare cases, a consumer deletes a message, but a copy on another server is not yet aware — that copy delivers the message again. This is a fundamental property of distributed systems: guaranteeing delivery requires replication, and replication can produce duplicates.

**Where it appears in CSVForge:** SQS Standard queue delivers job tickets to Lambda. The worker's idempotency guard (conditional UPDATE) handles duplicates.

**Analogy:** A postal system that guarantees your letter arrives but occasionally delivers an extra copy. The recipient needs to recognize and discard duplicates.

---

### Q8 — What is a visibility timeout?

**30-second answer:** When a consumer receives an SQS message, the message becomes invisible to all other consumers for the visibility timeout period. If the consumer finishes and deletes the message, it's gone. If the consumer crashes, the message reappears after the timeout.

**Deep explanation:** The visibility timeout is the mechanism that allows SQS to provide at-least-once delivery with retry. Without it, two workers would pick up the same message simultaneously. The timeout is set to 6× Lambda timeout (1800s) to ensure Lambda completes before the message reappears.

**Where it appears in CSVForge:** `csvforge-jobs-dev` has VisibilityTimeout=1800. While Lambda processes a job, no other Lambda can pick up the same ticket.

**Analogy:** Checking out a library book. While you have the book, no one else can borrow it. If you don't return it within the loan period, it becomes "available" again for others to borrow.

---

### Q9 — What is a DLQ?

**30-second answer:** A Dead Letter Queue receives messages that failed processing after a maximum number of retries. It is an auditable record of work that couldn't be completed.

**Deep explanation:** The `RedrivePolicy` on the main queue specifies `MaxReceiveCount` and `deadLetterTargetArn`. After a message is delivered `MaxReceiveCount` times without being deleted, SQS automatically moves it to the DLQ. The message body is preserved. Ops team can inspect DLQ messages, identify root causes, fix them, and replay messages to the main queue.

**Where it appears in CSVForge:** `csvforge-jobs-dlq-dev` receives jobs that failed 5 times. CloudWatch alarm on DLQ depth alerts the team.

**Analogy:** A failed jobs room in the factory. Work orders that couldn't be processed after 5 attempts are placed in the room for the supervisor to inspect. They don't disappear — they wait for a human to investigate.

---

### Q10 — Why is idempotency important?

**30-second answer:** Idempotency means processing the same input multiple times produces the same result. It makes systems safe with at-least-once delivery — a duplicate message doesn't cause duplicate work.

**Deep explanation:** Without idempotency, every duplicate SQS message would re-process the CSV — charging the user twice, storing two output files, incrementing counts twice. The `claim_job_for_processing()` conditional UPDATE is the idempotency key: once a job reaches `completed`, no second processing can happen.

**Where it appears in CSVForge:** The conditional SQL UPDATE `WHERE status IN ('queued','processing')` in `claim_job_for_processing()`.

**Analogy:** A bank transaction that charges a card. The payment processor may retry if it doesn't receive confirmation. Idempotency means retrying the same transaction doesn't charge twice.

---

### Q11 — How does SQS trigger Lambda?

**30-second answer:** An event source mapping (ESM) connects SQS to Lambda. The ESM polls SQS, batches messages, and invokes Lambda. Lambda code never polls SQS directly.

**Deep explanation:** The ESM is an AWS-managed poller that runs continuously, calls `sqs:ReceiveMessage` with the configured `BatchSize`, and invokes the Lambda function with the received messages as the event payload. On successful Lambda invocation (no exception), the ESM calls `sqs:DeleteMessage`. On Lambda failure, the ESM does nothing — the message retries after visibility timeout.

**Where it appears in CSVForge:** The ESM is created by `deploy_lambda.sh` using `aws lambda create-event-source-mapping`.

**Analogy:** The sensor on the conveyor belt that rings a bell and summons a worker when a ticket appears.

---

### Q12 — What is an event source mapping?

**30-second answer:** An event source mapping is the AWS-managed connector between an event source (SQS, DynamoDB Streams, Kinesis) and a Lambda function. It handles polling, batching, and invocation.

**Deep explanation:** The ESM is configured with: function name, event source ARN (SQS queue ARN), batch size, and optionally a filter. AWS runs the ESM infrastructure — you don't manage polling logic. The ESM also handles scaling: when the queue is deep, AWS increases the number of concurrent Lambda invocations up to the concurrency limit.

**Where it appears in CSVForge:** `aws lambda create-event-source-mapping --event-source-arn {QUEUE_ARN} --batch-size 1`.

**Analogy:** The staffing agency contract: "when the factory receives an order (SQS message), call one worker (Lambda invocation) immediately." The agency manages the logistics.

---

### Q13 — What is a Lambda execution role?

**30-second answer:** An IAM role that Lambda assumes when executing. It defines what AWS services the function can access. Credentials are injected automatically — the function code doesn't manage credentials.

**Deep explanation:** When Lambda starts an execution environment, AWS calls `sts:AssumeRole` on the execution role and injects temporary credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`) as environment variables. These credentials expire after ~15 minutes but are refreshed by AWS automatically. All boto3 calls inside the function use these credentials.

**Where it appears in CSVForge:** `csvforge-worker-role-dev` is the execution role. `lambda-execution-policy.json` defines what it can do.

**Analogy:** The set of keys given to the temporary worker when they start their shift. The keys are returned when the shift ends. The worker doesn't bring their own keys.

---

### Q14 — What is least privilege?

**30-second answer:** Least privilege means every role gets exactly the permissions it needs to do its job — no more. This limits the blast radius if credentials are compromised.

**Deep explanation:** Instead of giving Lambda an admin role, you grant exactly: S3 GetObject on `uploads/*`, S3 PutObject on `processed/*`, SQS ReceiveMessage/DeleteMessage/GetQueueAttributes on the specific queue ARN, CloudWatch logs on the Lambda log group. If this role is compromised, the attacker's reach is bounded to those exact resources.

**Where it appears in CSVForge:** `lambda-execution-policy.json` and `api-service-policy.json` — each has exactly the permissions required.

**Analogy:** The shipping clerk only has the key to the shipping dock. The accountant only has the key to the accounts office. Nobody has the master key.

---

### Q15 — What happens if Lambda fails?

**30-second answer:** If Lambda exits with an exception, the SQS message is not deleted. After the visibility timeout, the message reappears and Lambda retries. After 5 failures, the message goes to the DLQ.

**Deep explanation:** See Failure 1 in FAILURE_EXPERIMENTS.md. Lambda failure is transparent to SQS — SQS doesn't know why Lambda failed, only that the message was not deleted. The retry mechanism is entirely driven by the visibility timeout clock running out.

**Where it appears in CSVForge:** Any unhandled exception in `handler.lambda_handler` triggers this flow.

**Analogy:** The temporary worker walks out without finishing the job. The manager doesn't know why — they just notice the work order is back on the conveyor belt after 30 minutes and assigns a new worker.

---

### Q16 — Why shouldn't FastAPI process large CSV files synchronously?

**30-second answer:** API Gateway times out after 30 seconds. Processing a 50 MB CSV takes longer. The client would block waiting. A crash loses the work with no retry. Memory usage is unbounded with concurrent uploads.

**Deep explanation:** See Section 9 — Event-Driven Architecture.

**Where it appears in CSVForge:** The entire async architecture (SQS + Lambda) exists specifically to solve this problem.

**Analogy:** The reception desk can't process every package. It issues a work order and passes the package to the warehouse team. The desk focuses on receiving and scheduling; the workers focus on processing.

---

### Q17 — How does this architecture scale?

**30-second answer:** FastAPI scales horizontally (multiple instances). SQS scales automatically (no limits on Standard queue). Lambda scales automatically (concurrent invocations per queue depth). PostgreSQL and S3 are managed and scale independently.

**Deep explanation:** The queue is the decoupling point that enables independent scaling. When uploads spike: FastAPI publishes more messages to SQS; queue depth grows; AWS increases Lambda concurrency automatically (up to the account limit). When Lambda is at capacity: queue depth grows further — visible as a CloudWatch metric, triggering alerts or auto-scaling. S3 scales automatically: each request is independent, S3 handles millions of requests per second.

**Where it appears in CSVForge:** The architecture was designed with scale in mind (AD-01, AD-02).

**Analogy:** The factory can hire more temporary workers (Lambda) as more job tickets arrive on the conveyor belt. The belt itself handles any volume of tickets.

---

### Q18 — Where can backpressure occur?

**30-second answer:** The SQS queue is the backpressure mechanism. When Lambda can't keep up, the queue grows. Queue depth is a visible metric, not silent failure.

**Deep explanation:** Backpressure means a slow consumer signals a fast producer to slow down. In CSVForge: if Lambda is processing at 10 jobs/minute and uploads arrive at 100 jobs/minute, the queue depth grows by 90 messages per minute. This is visible in CloudWatch (`ApproximateNumberOfMessages`). You can set an alarm at depth > 100 and alert the team or trigger auto-scaling.

**Where it appears in CSVForge:** SQS queue depth is the pressure gauge. CloudWatch alarm on `ApproximateNumberOfMessages` is the alert.

**Analogy:** The loading dock has a limited number of parking spaces. When all spaces are full, trucks (uploads) queue outside. The queue length is visible — operations knows when to call more workers.

---

### Q19 — What happens if S3 succeeds but SQS publishing fails?

**30-second answer:** The job is stuck in `queued` status in PostgreSQL. The CSV is in S3 but no Lambda will process it until the SQS message is re-published.

**Deep explanation:** This is the DB-before-SQS failure window documented in AD-29. The API updates the job to `queued`, then calls SQS SendMessage. If SendMessage fails (transient network error, SQS outage), the job is `queued` with no corresponding SQS message. Lambda never processes it. Recovery requires manually re-publishing the message or implementing a reaper job that finds jobs stuck in `queued` for > N minutes.

**Where it appears in CSVForge:** AD-29 documents this as a known limitation. A CloudWatch alarm on jobs stuck in `queued` > 10 minutes would surface this.

**Analogy:** The factory ledger says a job is queued, but the work order never made it to the conveyor belt. The supervisor needs to manually re-submit the work order.

---

### Q20 — What happens if SQS succeeds but Lambda fails?

**30-second answer:** SQS retries automatically. The message remains in the queue and Lambda picks it up again after the visibility timeout.

**Deep explanation:** This is the happy path of the retry mechanism. SQS + ESM handle this case entirely: the message was published, Lambda received it, Lambda failed, the message became visible again, Lambda retried. No manual intervention needed unless failures exceed MaxReceiveCount=5.

**Where it appears in CSVForge:** The core retry mechanism — see Failure 1 in FAILURE_EXPERIMENTS.md.

**Analogy:** The temporary worker couldn't complete the job. After 30 minutes, the work order reappears on the belt and a new worker picks it up.

---

### Q21 — Why do we need PostgreSQL if S3 already stores the file?

**30-second answer:** PostgreSQL stores job state and metadata (status, timestamps, counts) that you need to query. S3 stores file bytes that you need to transfer. They serve completely different purposes.

**Deep explanation:** See Exercise 10 in Section 12. The key insight: you cannot efficiently query S3 for "all jobs for user Alice with status=failed." S3 has no query engine. You'd have to list all objects and filter — expensive, slow, eventually consistent. PostgreSQL answers this query in milliseconds with an index.

**Where it appears in CSVForge:** PostgreSQL tracks job lifecycle. S3 stores the actual CSV data.

**Analogy:** You need both a ledger (PostgreSQL) and a warehouse (S3). The ledger tells you what's in the warehouse and its status. Without the ledger, finding anything would require searching every shelf. Without the warehouse, there'd be nowhere to put the actual goods.

---

### Q22 — How do you prevent duplicate processing?

**30-second answer:** Idempotent worker with conditional SQL UPDATE. `claim_job_for_processing()` only succeeds if the job is in `queued` or `processing` state. A `completed` job cannot be re-claimed.

**Deep explanation:** The SQL `UPDATE jobs SET status='processing' WHERE job_id=:id AND status IN ('queued','processing') RETURNING job_id` is atomic in PostgreSQL. If two Lambda invocations race to claim the same job, exactly one UPDATE will match — the other gets rowcount=0 and exits immediately. PostgreSQL row-level locking ensures this is safe under concurrency.

**Where it appears in CSVForge:** `claim_job_for_processing()` in `backend/db/job_repository.py`.

**Analogy:** Two workers reach for the same ticket on the conveyor belt simultaneously. The ledger system only lets one of them check it out — the other sees "already claimed" and moves on.

---

### Q23 — How do you monitor this architecture?

**30-second answer:** CloudWatch metrics on SQS queue depth (ApproximateNumberOfMessages), DLQ depth (alert on > 0), Lambda errors and duration, and custom structured logs with job_id at each lifecycle step.

**Deep explanation:** Four layers of observability: (1) Lambda metrics: invocation count, error rate, duration, throttles. (2) SQS metrics: messages sent, received, deleted; queue depth; DLQ depth. (3) PostgreSQL: query performance, connection count, job status distribution (count of jobs by status). (4) Structured logs: every Lambda invocation logs `job_id`, `status`, `duration_ms`, `error` (if any). This allows tracing the complete lifecycle of any job from the logs.

**Key alerts:**
- DLQ `ApproximateNumberOfMessages > 0`: permanent failures detected
- Main queue `ApproximateNumberOfMessages > 100`: backpressure — Lambda can't keep up
- Lambda `ErrorRate > 5%`: high failure rate
- Lambda `Duration > 250000ms`: approaching 5-minute timeout

**Where it appears in CSVForge:** CloudWatch Logs for Lambda structured output; SQS metrics for queue health; DLQ alarm from `create_dev_resources.sh`.

**Analogy:** The factory CCTV (CloudWatch Logs), the production manager's dashboard (metrics), and the alarm system (metric alarms). You can watch every step, measure throughput, and get paged when something breaks.
